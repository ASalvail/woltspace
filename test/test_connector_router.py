"""The real inbound router: accepted events become session messages, with the
session boundary mocked."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from server.connector_router import Router, session_text  # noqa: E402
from server.connectors import AcceptedEvent, ConnectorStore, Limits  # noqa: E402


class Sessions:
    """A fake session boundary: what is live, what was pasted, what was resumed."""

    def __init__(self, live=None, statuses=None):
        self.live = live or {}            # wolt -> live session name
        self.statuses = statuses or {}    # session -> deliver status
        self.pasted = []
        self.resumed = []

    def deliver(self, session, text):
        self.pasted.append((session, text))
        return {"status": self.statuses.get(session, "delivered"), "session": session}

    def resume(self, session, prompt):
        self.resumed.append((session, prompt))
        return {"status": "revived"}

    def resolve(self, wolt):
        return self.live.get(wolt)


@pytest.fixture
def lodge(tmp_path):
    store = ConnectorStore(tmp_path / "connectors", limits=Limits(outbound_file_max_bytes=1_000_000))
    store.add_instance("tg-main", "telegram", "bot:1")
    binding = store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie")
    sleepy = store.bind("tg-main", sender="u1", conversation="c2", default_wolt="softie", may_wake=True)
    store.enable("tg-main")
    return store, binding, sleepy


def _event(store, binding, text="hello", attachments=(), reply_to_session=None, conversation="c1"):
    return AcceptedEvent(
        "tg-main", "telegram", "bot:1", "tg:1", binding, {"provider_id": "u1", "display": "jer"},
        {"provider_id": conversation, "thread": None}, text, tuple(attachments), reply_to_session, 7,
    )


def _replies(store):
    return [d["text"] for d in store.list_deliveries("tg-main") if d["status"] == "queued"]


def test_delivers_to_the_default_wolts_live_session_and_remembers_it(lodge, tmp_path):
    store, binding, _ = lodge
    sessions = Sessions(live={"softie": "softie-7"})
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    result = router(_event(store, binding))
    assert result == {"routed": "delivered", "session": "softie-7"}
    session, text = sessions.pasted[0]
    assert session == "softie-7"
    assert text.startswith("[telegram message from jer, chat_id=c1, event=tg:1]: hello")
    assert "WOLTSPACE_NOTIFY" in text
    assert store.routing_state("tg-main", binding["binding_id"]) == {"active_session": "softie-7"}
    assert _replies(store) == ["🐾 sent to softie-7"]
    # Next time the remembered session wins over the wolt's live one.
    sessions.live["softie"] = "softie-8"
    router(_event(store, binding, "again"))
    assert sessions.pasted[-1][0] == "softie-7"


def test_a_reply_goes_to_the_delivery_s_session_first(lodge, tmp_path):
    store, binding, _ = lodge
    sessions = Sessions(live={"softie": "softie-7"})
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    store.remember_route("tg-main", binding["binding_id"], active_session="softie-7")
    assert router(_event(store, binding, reply_to_session="softie-3"))["session"] == "softie-3"
    assert sessions.pasted[0][0] == "softie-3"


def test_no_live_session_is_refused_not_spawned(lodge, tmp_path):
    store, binding, _ = lodge
    sessions = Sessions()
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    result = router(_event(store, binding))
    assert result == {"routed": "refused", "reason": "no_session", "wolt": "softie"}
    assert sessions.pasted == [] and sessions.resumed == []
    assert _replies(store) == ["🪵 softie has no live session; ask the owner to start one"]


def test_a_stopped_session_is_woken_only_when_the_binding_may_wake(lodge, tmp_path):
    store, binding, sleepy = lodge
    sessions = Sessions(live={"softie": "softie-7"}, statuses={"softie-7": "agent-gone"})
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    refused = router(_event(store, binding))
    assert refused == {"routed": "refused", "reason": "may_wake_off", "session": "softie-7"}
    assert sessions.resumed == []
    woken = router(_event(store, sleepy, conversation="c2"))
    assert woken == {"routed": "resumed", "session": "softie-7", "detail": "revived"}
    assert sessions.resumed[0][0] == "softie-7"
    assert store.routing_state("tg-main", sleepy["binding_id"]) == {"active_session": "softie-7"}
    texts = _replies(store)
    assert "🪵 softie-7 is asleep and this chat may not wake it" in texts
    assert "🪵 softie-7 was asleep; woke it and delivered" in texts


def test_a_gone_session_falls_back_to_the_wolts_live_one_once(lodge, tmp_path):
    store, binding, _ = lodge
    sessions = Sessions(live={"softie": "softie-9"}, statuses={"softie-old": "no-session"})
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    store.remember_route("tg-main", binding["binding_id"], active_session="softie-old")
    result = router(_event(store, binding))
    assert result == {"routed": "delivered", "session": "softie-9", "fallback_from": "softie-old"}
    assert [s for s, _ in sessions.pasted] == ["softie-old", "softie-9"]
    assert store.routing_state("tg-main", binding["binding_id"]) == {"active_session": "softie-9"}
    sessions.live = {}
    sessions.statuses["softie-9"] = "no-session"
    gone = router(_event(store, binding))
    assert gone["routed"] == "refused"


def test_uploads_move_to_the_adapters_folder_and_are_released(lodge, tmp_path):
    store, binding, _ = lodge
    who = store.authenticate(store.issue_token("tg-main"))
    upload_id, part = store.begin_upload(who)
    part.write_bytes(b"opus bytes")
    store.commit_upload(who, upload_id, part, "note.ogg")
    store._queue("tg-main").uploads[upload_id]["claimed_by"] = "tg:1"
    sessions = Sessions(live={"softie": "softie-7"})
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    attachment = {"upload_id": upload_id, "path": str(store.root / "uploads" / "tg-main" / upload_id), "name": "note.ogg", "size": 10}
    router(_event(store, binding, "listen", attachments=[attachment]))
    saved = list((tmp_path / "space" / "telegram" / "uploads").iterdir())
    assert len(saved) == 1 and saved[0].name.endswith("_note.ogg")
    assert saved[0].read_bytes() == b"opus bytes"
    assert f"[file saved: {saved[0]}]" in sessions.pasted[0][1]
    assert not (store.root / "uploads" / "tg-main" / upload_id).exists()
    assert upload_id not in store._queue("tg-main").uploads


def test_session_text_names_the_event_for_duplicate_detection(lodge):
    store, binding, _ = lodge
    text = session_text(_event(store, binding, "hi"), [])
    assert "event=tg:1" in text.splitlines()[0]


def _boundary(sessions):
    return {"deliver": sessions.deliver, "resume": sessions.resume, "resolve": sessions.resolve}
