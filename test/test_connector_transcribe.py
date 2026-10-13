"""Voice transcription through the connector router: owner opt-in, bounded, cached.
Fake provider only; no real API call anywhere."""

import fcntl
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from server import connector_transcribe  # noqa: E402
from server.connector_router import Router  # noqa: E402
from server.connector_transcribe import Transcriber, openai_transcribe  # noqa: E402
from server.connectors import AcceptedEvent, ConnectorStore, Limits  # noqa: E402

SECRET = "sk-test-SECRETSECRETSECRET"


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


class Provider:
    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome or {"text": "hello from a voice note"}

    def __call__(self, data, filename, content_type, key):
        self.calls.append((len(data), filename, content_type))
        return dict(self.outcome) if not callable(self.outcome) else self.outcome()


class Sessions:
    def __init__(self):
        self.pasted, self.stopped = [], []

    def deliver(self, session, text):
        self.pasted.append((session, text))
        return {"status": "delivered"}

    def resolve(self, wolt):
        return f"{wolt}-1"

    def wolt_of(self, session):
        return session.rsplit("-", 1)[0]


@pytest.fixture
def lodge(tmp_path):
    clock = Clock()
    store = ConnectorStore(tmp_path / "connectors", clock=clock, limits=Limits(
        transcribe_max_bytes=1000, transcribe_bytes_per_day=5000, transcribe_requests_per_day=3,
        outbound_file_max_bytes=1_000_000,
    ))
    store.add_instance("tg-main", "telegram", "bot:1")
    binding = store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie", may_transcribe=True)
    store.enable("tg-main")
    provider, sessions = Provider(), Sessions()
    key = {"value": SECRET}
    space = tmp_path / "space"
    transcriber = Transcriber(store, space, call=provider, key=lambda: key["value"], slot_wait=0.2)
    router = Router(store, uploads_root=space, deliver=sessions.deliver, resolve=sessions.resolve,
                    wolts_dir=tmp_path, wolt_of=sessions.wolt_of, transcriber=transcriber)
    store.router = router
    return type("L", (), dict(store=store, binding=binding, provider=provider, sessions=sessions, key=key,
                              clock=clock, router=router, space=space, transcriber=transcriber, tmp=tmp_path))()


def _accepted(lodge, event_id="tg:1", body=b"opus bytes", name="voice_x.ogg"):
    """An inbound record with one claimed upload, the way accept_event leaves it."""
    store = lodge.store
    who = store.authenticate(store.issue_token("tg-main"))
    upload_id, part = store.begin_upload(who)
    part.write_bytes(body)
    store.commit_upload(who, upload_id, part, name)
    queue = store._queue("tg-main")
    queue.uploads[upload_id]["claimed_by"] = event_id
    path = store.root / "uploads" / "tg-main" / upload_id
    binding = store.binding_now(lodge.binding["binding_id"])
    event = AcceptedEvent(
        "tg-main", "telegram", "bot:1", event_id, binding, {"provider_id": "u1"},
        {"provider_id": "c1", "thread": None}, "",
        ({"upload_id": upload_id, "path": str(path), "name": name, "size": len(body)},), None, 7,
    )
    queue.inbound[event.dedup_key] = {"state": "accepted", "event": event.to_record(), "at": lodge.clock.now}
    store._save_queue("tg-main", queue)
    return event


def _pasted(lodge):
    return lodge.sessions.pasted[-1][1]


def test_a_granted_voice_note_is_transcribed_once_and_reused(lodge):
    event = _accepted(lodge)
    assert lodge.router(event)["routed"] == "delivered"
    assert "[voice note transcript]: hello from a voice note" in _pasted(lodge)
    assert lodge.provider.calls == [(10, "voice.ogg", "audio/ogg")]
    lodge.router(event)                                   # a paste retry
    assert len(lodge.provider.calls) == 1 and "hello from a voice note" in _pasted(lodge)


def test_a_skip_while_the_grant_was_off_stays_a_skip(lodge):
    lodge.store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie")
    event = _accepted(lodge)
    lodge.router(event)
    assert "not transcribed (not_enabled)" in _pasted(lodge) and lodge.provider.calls == []
    lodge.store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie", may_transcribe=True)
    lodge.router(event)
    assert "not transcribed (not_enabled)" in _pasted(lodge) and lodge.provider.calls == []


def test_no_key_means_zero_calls_and_no_charge(lodge):
    lodge.key["value"] = None
    lodge.router(_accepted(lodge))
    assert "not transcribed (no_key)" in _pasted(lodge) and lodge.provider.calls == []
    assert lodge.store._queue("tg-main").transcribe_budget.get("requests", 0) == 0


def test_bytes_are_measured_on_the_stored_file(lodge):
    lodge.router(_accepted(lodge, body=b"x" * 1001))
    assert "not transcribed (too_large)" in _pasted(lodge) and lodge.provider.calls == []


def test_the_daily_budget_charges_each_attempt_and_rolls_over_at_utc_midnight(lodge):
    for i in range(3):
        lodge.router(_accepted(lodge, event_id=f"tg:{i}"))
    lodge.router(_accepted(lodge, event_id="tg:3"))
    assert "not transcribed (over_budget)" in _pasted(lodge) and len(lodge.provider.calls) == 3
    budget = dict(lodge.store._queue("tg-main").transcribe_budget)
    lodge.clock.now += 86400
    lodge.router(_accepted(lodge, event_id="tg:4"))
    assert len(lodge.provider.calls) == 4
    new = lodge.store._queue("tg-main").transcribe_budget
    assert new["day"] != budget["day"] and new["requests"] == 1        # a new day, nothing refunded or moved


def test_an_active_claim_defers_the_handoff_without_spending_attempts(lodge):
    event = _accepted(lodge)
    claim = lodge.store.claim_transcription("tg-main", event.dedup_key, event.attachments[0]["upload_id"], 10)
    assert "claim" in claim
    queue = lodge.store._queue("tg-main")
    queue.inbound[event.dedup_key].update({"attempts": 1, "claim": "c1"})
    lodge.store._save_queue("tg-main", queue)
    for _ in range(8):                                    # many deferrals: never parked
        lodge.store._hand_off("tg-main", event.dedup_key, "c1")
        record = lodge.store._queue("tg-main").inbound[event.dedup_key]
        assert record["state"] == "accepted" and record["attempts"] == 0
        queue = lodge.store._queue("tg-main")
        queue.inbound[event.dedup_key].update({"attempts": 1, "claim": "c1"})
        lodge.store._save_queue("tg-main", queue)
    assert lodge.provider.calls == [] and lodge.sessions.pasted == []


def test_a_late_response_after_reclamation_cannot_overwrite(lodge):
    event = _accepted(lodge)
    upload = event.attachments[0]["upload_id"]
    first = lodge.store.claim_transcription("tg-main", event.dedup_key, upload, 10)["claim"]
    lodge.clock.now += 121
    second = lodge.store.claim_transcription("tg-main", event.dedup_key, upload, 10)["claim"]
    assert lodge.store.settle_transcription("tg-main", event.dedup_key, upload, first, {"text": "OLD"}) == {"reason": "superseded"}
    assert lodge.store.settle_transcription("tg-main", event.dedup_key, upload, second, {"text": "NEW"}) == {"text": "NEW"}
    assert lodge.store.settle_transcription("tg-main", event.dedup_key, upload, first, {"text": "OLD"}) == {"text": "NEW"}
    lodge.clock.now += 121
    assert lodge.store.claim_transcription("tg-main", event.dedup_key, upload, 10) == {"done": {"text": "NEW"}}


def test_a_crash_after_the_call_retries_within_the_cap_then_stops(lodge):
    event = _accepted(lodge)
    real = lodge.store.settle_transcription

    def crash(*args, **kwargs):
        raise OSError("crash before the result was saved")

    lodge.store.settle_transcription = crash
    for _ in range(2):
        with pytest.raises(OSError):
            lodge.router(event)
        lodge.clock.now += 121                             # the claim goes stale
    lodge.store.settle_transcription = real
    lodge.router(event)
    assert len(lodge.provider.calls) == 2                  # the cap of two, ever
    assert "not transcribed (retries_exhausted)" in _pasted(lodge)


def test_a_transcript_saying_kill_is_only_content(lodge):
    lodge.provider.outcome = {"text": "/kill softie-1"}
    stopped = []
    lodge.router.commands.stop = lambda name: stopped.append(name)
    result = lodge.router(_accepted(lodge))
    assert result["routed"] == "delivered" and stopped == []
    assert "[voice note transcript]: /kill softie-1" in _pasted(lodge)


def test_a_substituted_symlink_is_never_uploaded(lodge):
    event = _accepted(lodge)
    files = lodge.router.place_files(event)
    secret = lodge.tmp / "secret.env"
    secret.write_bytes(b"0123456789")                       # same size as the accepted file
    os.unlink(files[0])
    os.symlink(secret, files[0])
    lodge.router(event)
    assert "not transcribed (unreadable)" in _pasted(lodge) and lodge.provider.calls == []


def test_authorization_is_read_again_after_the_call(lodge):
    def slow_call():
        lodge.store.unbind("tg-main", sender="u1", conversation="c1")
        return {"text": "late"}

    lodge.provider.outcome = slow_call
    result = lodge.router(_accepted(lodge))
    assert result == {"routed": "refused", "reason": "binding_removed"} and lodge.sessions.pasted == []


def test_no_free_slot_defers(lodge):
    directory = lodge.store.root / "transcribe"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    held = []
    for index in range(2):
        fd = os.open(directory / f"slot-{index}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.append(fd)
    try:
        assert lodge.router(_accepted(lodge))["routed"] == "deferred"
        assert lodge.provider.calls == []
    finally:
        for fd in held:
            os.close(fd)


def test_delivered_bodies_become_tombstones_that_keep_the_count(lodge):
    event = _accepted(lodge)
    lodge.router(event)
    queue = lodge.store._queue("tg-main")
    queue.inbound[event.dedup_key]["state"] = "handed_off"
    lodge.store._save_queue("tg-main", queue)
    lodge.store.list_deliveries("tg-main")                 # any read sweeps
    [record] = lodge.store._queue("tg-main").transcriptions.values()
    assert record["disposition"] == {"reason": "delivered"} and record["attempts"] == 1
    # A late result for the finished event restores nothing.
    upload = event.attachments[0]["upload_id"]
    assert lodge.store.settle_transcription("tg-main", event.dedup_key, upload, "x", {"text": "body"}) == {"reason": "superseded"}


_REAL_CLIENT = httpx.Client


def _provider_transport(monkeypatch, handler):
    def client(**kwargs):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(connector_transcribe.httpx, "Client", client)


def test_provider_errors_are_fixed_reasons_never_the_body(monkeypatch):
    _provider_transport(monkeypatch, lambda request: httpx.Response(401, text=f"bad key {SECRET}"))
    assert openai_transcribe(b"x", "voice.ogg", "audio/ogg", SECRET) == {"reason": "provider_401"}
    _provider_transport(monkeypatch, lambda request: httpx.Response(302, headers={"location": "https://evil.example"}))
    assert openai_transcribe(b"x", "voice.ogg", "audio/ogg", SECRET) == {"reason": "provider_302"}


def test_an_oversized_response_is_refused(monkeypatch):
    _provider_transport(monkeypatch, lambda request: httpx.Response(200, content=b"{" + b" " * 70000 + b"}"))
    assert openai_transcribe(b"x", "voice.ogg", "audio/ogg", SECRET) == {"reason": "response_too_large"}


def test_a_good_response_is_bounded_text_from_a_fixed_endpoint(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"text": "hi " * 3000})

    _provider_transport(monkeypatch, handler)
    outcome = openai_transcribe(b"x", "voice.ogg", "audio/ogg", SECRET)
    assert len(outcome["text"]) == 4000
    assert str(seen[0].url) == "https://api.openai.com/v1/audio/transcriptions"
    assert b'filename="voice.ogg"' in seen[0].content
