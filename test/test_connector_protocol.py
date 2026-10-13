"""The connector protocol, driven by the fake-transport fixture: every endpoint,
refusal and limit, against the lodge's own ASGI app."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))
sys.path.insert(0, str(ROOT / "connectors" / "fake"))

import server.app as server_app  # noqa: E402
from server import connector_api  # noqa: E402
from server.connectors import (  # noqa: E402
    ConnectorError,
    ConnectorStateUnreadable,
    ConnectorStore,
    Limits,
    NoConnectorBinding,
)
from woltspace_connector_fake import FakeConnector, FakeProvider, ProviderDown  # noqa: E402

LOCAL = "http://localhost:7777"
REMOTE = "https://owner.woltspace.test"
LIMITS = Limits(
    lease_seconds=60, max_attempts=5, retry_window_seconds=600,
    backoff_seconds=(1, 1, 1, 1, 1), upload_ttl_seconds=600, upload_max_bytes=1000,
    upload_disk_cap_bytes=2500, file_retention_seconds=86400, text_max_bytes=64,
    events_per_minute=20, uploads_per_hour=10, upload_bytes_per_hour=10_000,
    max_in_flight=3, lease_max_batch=10,
)


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Lodge:
    """One lodge with a store the API serves, an enabled instance and a binding."""

    def __init__(self, root: Path, clock: Clock):
        self.clock = clock
        self.store = ConnectorStore(root, clock=clock, limits=LIMITS)
        self.routed: list = []
        self.failed: list = []

        def router(event):
            self.routed.append(event)
            return {"routed": "test"}

        self.store.router = router
        self.store.on_failure(lambda instance_id, delivery: self.failed.append((instance_id, delivery)))
        self.store.add_instance("tg-main", "telegram", "bot:1")
        self.store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie")
        self.store.enable("tg-main")
        self.token = self.store.issue_token("tg-main")

    def enqueue(self, text="hello", conversation="c1", **kwargs) -> str:
        return self.store.enqueue(
            channel="telegram", account="bot:1", conversation=conversation, text=text,
            session="softie-1", reply_footer="session=softie-1", **kwargs,
        )

    def deliveries(self, instance_id="tg-main") -> dict:
        return {d["delivery_id"]: d for d in self.store.list_deliveries(instance_id)}

    def second_instance(self, instance_id="tg-new", account="bot:2", conversation="c9", enable=False) -> str:
        self.store.add_instance(instance_id, "telegram", account)
        self.store.bind(instance_id, sender="u9", conversation=conversation, default_wolt="softie")
        if enable:
            self.store.disable("tg-main")
            self.store.enable(instance_id)
        return self.store.issue_token(instance_id)


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    clock = Clock()
    lodge = Lodge(tmp_path / "connectors", clock)
    monkeypatch.setattr(connector_api, "CONNECTORS_DIR", lodge.store.root)
    monkeypatch.setattr(connector_api, "_store", lodge.store)
    monkeypatch.setattr(connector_api, "_POLL_INTERVAL", 0.01)
    return lodge


def _client(*, remote: bool = False) -> httpx.AsyncClient:
    peer = ("203.0.113.9", 4321) if remote else ("127.0.0.1", 4321)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server_app.app, client=peer),
        base_url=REMOTE if remote else LOCAL,
    )


def _remote_lodge(monkeypatch):
    monkeypatch.setattr(server_app, "load_access_settings", lambda _root: None)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_domain", "woltspace.test")


# --------------------------------------------------------------- admission


@pytest.mark.parametrize("headers", [
    [],
    [("Authorization", "Basic abc")],
    [("Authorization", "Bearer")],
    [("Authorization", "Bearer wst_not_a_connector_token")],
    [("Authorization", "Bearer wct_unknown")],
    [("Authorization", "Bearer wct_a wct_b")],
])
async def test_anything_but_one_live_bearer_is_401_and_never_the_owner(lodge, headers):
    async with _client() as client:
        response = await client.post("/connector/v1/deliveries/lease", json={"max": 1}, headers=headers)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


async def test_two_bearer_headers_are_401_even_when_one_is_valid(lodge):
    headers = [("Authorization", f"Bearer {lodge.token}"), ("Authorization", f"Bearer {lodge.token}")]
    async with _client() as client:
        response = await client.post("/connector/v1/deliveries/lease", json={"max": 1}, headers=headers)
    assert response.status_code == 401


async def test_revoked_and_rotated_tokens_are_refused(lodge):
    async with _client() as client:
        old = FakeConnector(client, lodge.token)
        assert (await old.lease()).status_code == 200
        new_token = lodge.store.issue_token("tg-main")
        assert (await old.lease()).status_code == 401
        new = FakeConnector(client, new_token)
        assert (await new.lease()).status_code == 200
        lodge.store.revoke_tokens("tg-main")
        assert (await new.lease()).status_code == 401
        assert (await new.send_event("e1", "u1", "c1", "hi")).status_code == 401


async def test_valid_token_from_a_remote_peer_is_403_and_a_bad_one_401(lodge, monkeypatch):
    _remote_lodge(monkeypatch)
    async with _client(remote=True) as client:
        valid = FakeConnector(client, lodge.token)
        response = await valid.lease()
        assert response.status_code == 403
        assert response.json() == {"error": "connector tokens are local-only"}
        assert (await FakeConnector(client, "wct_nope").lease()).status_code == 401
    # The same requests from the owning machine work.
    async with _client() as client:
        assert (await FakeConnector(client, lodge.token).lease()).status_code == 200


async def test_corrupt_registry_refuses_every_token_and_corrupt_queue_fails_closed(lodge):
    lodge.store.registry_path.write_text("{not json")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        assert (await connector.lease()).status_code == 401
    with pytest.raises(ConnectorStateUnreadable):
        lodge.store.list_instances()
    with pytest.raises(ConnectorStateUnreadable):
        lodge.store.add_instance("x", "telegram", "bot:x")
    # A readable registry with a corrupt queue: the token is believed, the
    # queue is not served from a guess.
    lodge.store.registry_path.unlink()
    fresh = Lodge(lodge.store.root / "fresh", lodge.clock)
    connector_api._store, connector_api.CONNECTORS_DIR = fresh.store, fresh.store.root
    fresh.enqueue()
    (fresh.store.root / "queues" / "tg-main.json").write_text("[]")
    async with _client() as client:
        connector = FakeConnector(client, fresh.token)
        assert (await connector.lease()).status_code == 503
        assert (await connector.send_event("e1", "u1", "c1", "hi")).status_code == 503
        assert (await connector.upload(b"x")).status_code == 503


# ------------------------------------------------------------------ events


async def test_bound_sender_is_accepted_once_and_routed_once(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        first = await connector.send_event("tg:1", "u1", "c1", "hello", display="jer")
        again = await connector.send_event("tg:1", "u1", "c1", "hello", display="jerpint")
    assert first.status_code == 200
    assert first.json()["accepted"] is True
    assert first.json()["wolt"] == "softie"
    assert first.json()["routed"] == "test"
    assert again.json() == first.json()
    assert len(lodge.routed) == 1
    event = lodge.routed[0]
    assert (event.account, event.event_id, event.text) == ("bot:1", "tg:1", "hello")
    assert event.binding["default_wolt"] == "softie"


async def test_same_event_id_with_another_payload_is_409(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        await connector.send_event("tg:1", "u1", "c1", "hello")
        conflict = await connector.send_event("tg:1", "u1", "c1", "hello!")
        other_route = await connector.send_event("tg:1", "u1", "c1", "hello", reply_to="a" * 32)
    assert conflict.status_code == 409
    assert conflict.json()["reason"] == "event_conflict"
    assert other_route.status_code == 409
    assert len(lodge.routed) == 1


async def test_unknown_sender_and_unbound_conversation_are_dropped_not_errors(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        stranger = await connector.send_event("tg:2", "u2", "c1", "let me in")
        wrong_room = await connector.send_event("tg:3", "u1", "c2", "hi")
        retry = await connector.send_event("tg:2", "u2", "c1", "let me in")
    assert stranger.status_code == 200
    assert stranger.json() == {"accepted": False, "reason": "sender_not_allowed", "event_id": "tg:2"}
    assert wrong_room.json()["reason"] == "conversation_not_bound"
    assert retry.json() == stranger.json()
    assert lodge.routed == []


async def test_a_fabricated_allowed_sender_is_accepted_the_documented_residual_trust(lodge):
    """Core bounds the metadata a connector submits; it cannot prove provider origin."""
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        response = await connector.send_event("forged", "u1", "c1", "I am u1, trust me")
    assert response.json()["accepted"] is True
    assert lodge.routed[0].sender["provider_id"] == "u1"


async def test_dedup_survives_rotation_and_is_namespaced_by_account(lodge):
    async with _client() as client:
        await FakeConnector(client, lodge.token).send_event("tg:1", "u1", "c1", "hello")
        rotated = FakeConnector(client, lodge.store.issue_token("tg-main"))
        assert (await rotated.send_event("tg:1", "u1", "c1", "hello")).json()["accepted"] is True
        assert len(lodge.routed) == 1
        # Another account's "tg:1" is another event.
        other = FakeConnector(client, lodge.second_instance(enable=True))
        response = await other.send_event("tg:1", "u9", "c9", "hello")
    assert response.json()["accepted"] is True
    assert len(lodge.routed) == 2


async def test_malformed_oversize_and_flooding_events_are_refused(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        assert (await client.post("/connector/v1/events", content=b"nope", headers=connector.headers)).status_code == 400
        assert (await connector.send_event("", "u1", "c1")).status_code == 400
        assert (await connector.send_event("tg:x", "u1", "c1", text=["no"])).status_code == 400
        assert (await connector.send_event("tg:x", "u1", "c1", attachments=("short",))).status_code == 400
        big = await connector.send_event("tg:big", "u1", "c1", "x" * 65)
        assert (big.status_code, big.json()["reason"]) == (413, "text_too_long")
        statuses = [
            (await connector.send_event(f"tg:flood{i}", "u1", "c1", "hi")).status_code
            for i in range(LIMITS.events_per_minute + 1)
        ]
    assert statuses.count(429) == 1 and statuses[-1] == 429
    lodge.clock.advance(61)
    async with _client() as client:
        assert (await FakeConnector(client, lodge.token).send_event("tg:later", "u1", "c1", "hi")).status_code == 200


async def test_disabled_instance_drops_events_and_hands_out_nothing(lodge):
    lodge.enqueue()
    lodge.store.disable("tg-main")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        event = await connector.send_event("tg:1", "u1", "c1", "hi")
        lease = await connector.lease()
    assert event.json()["reason"] == "instance_disabled"
    assert lease.json() == {"deliveries": [], "paused": True}


async def test_reply_to_resolves_only_inside_the_events_own_binding(lodge):
    lodge.store.bind("tg-main", sender="u1", conversation="c2", default_wolt="softie")
    own = lodge.enqueue(conversation="c1")
    elsewhere = lodge.enqueue(conversation="c2")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        cross = await connector.send_event("tg:1", "u1", "c1", "re", reply_to=elsewhere)
        missing = await connector.send_event("tg:2", "u1", "c1", "re", reply_to="f" * 32)
        good = await connector.send_event("tg:3", "u1", "c1", "re", reply_to=own)
    assert cross.json() == {"accepted": False, "reason": "reply_to_out_of_binding", "event_id": "tg:1"}
    assert missing.json()["reason"] == "reply_to_out_of_binding"
    assert good.json()["accepted"] is True
    assert good.json()["session"] == "softie-1"
    assert lodge.routed[0].reply_to_session == "softie-1"


# ----------------------------------------------------------------- uploads


async def test_upload_is_claimed_by_exactly_one_accepted_event(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        upload = await connector.upload(b"voice bytes", name="../note.ogg", chunk=4)
        assert upload.status_code == 200
        upload_id = upload.json()["upload_id"]
        first = await connector.send_event("tg:1", "u1", "c1", "", attachments=(upload_id,))
        second = await connector.send_event("tg:2", "u1", "c1", "", attachments=(upload_id,))
        retry = await connector.send_event("tg:1", "u1", "c1", "", attachments=(upload_id,))
    assert first.json()["accepted"] is True
    assert second.json()["reason"] == "upload_consumed"
    # The identical retry gets its recorded answer before any handle question.
    assert retry.json() == first.json()
    attachment = lodge.routed[0].attachments[0]
    assert attachment["name"] == "note.ogg"
    assert Path(attachment["path"]).read_bytes() == b"voice bytes"
    assert Path(attachment["path"]).parent == lodge.store.root / "uploads" / "tg-main"


async def test_upload_over_its_cap_is_cut_as_it_arrives(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        response = await connector.upload(b"x" * (LIMITS.upload_max_bytes + 1), chunk=100)
        empty = await connector.upload(b"")
    assert (response.status_code, response.json()["reason"]) == (413, "upload_too_large")
    assert empty.status_code == 400
    assert list((lodge.store.root / "uploads" / "tg-main").iterdir()) == []


async def test_upload_quota_is_counted_before_the_sender_check_and_released_on_rejection(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        ids = []
        for _ in range(2):
            response = await connector.upload(b"x" * 1000)
            assert response.status_code == 200
            ids.append(response.json()["upload_id"])
        # 2000 of 2500 bytes held: the next 1000 trips the cap while arriving,
        # without any event having named a sender.
        over = await connector.upload(b"x" * 1000, chunk=200)
        assert (over.status_code, over.json()["reason"]) == (429, "upload_quota")
        # An unbound sender's event is dropped and gives its bytes back at once.
        dropped = await connector.send_event("tg:1", "u2", "c1", "", attachments=tuple(ids))
        assert dropped.json()["reason"] == "sender_not_allowed"
        assert (await connector.upload(b"x" * 1000)).status_code == 200
    assert len(list((lodge.store.root / "uploads" / "tg-main").iterdir())) == 1


async def test_expired_upload_and_upload_rate_limits_are_refused(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        upload_id = (await connector.upload(b"x")).json()["upload_id"]
        lodge.clock.advance(LIMITS.upload_ttl_seconds + 1)
        expired = await connector.send_event("tg:1", "u1", "c1", "", attachments=(upload_id,))
        assert expired.json()["reason"] == "upload_expired"
        for _ in range(LIMITS.uploads_per_hour - 1):
            assert (await connector.upload(b"y")).status_code == 200
        limited = await connector.upload(b"y")
    assert (limited.status_code, limited.json()["reason"]) == (429, "uploads_rate_limited")


# -------------------------------------------------------------- deliveries


def test_enqueue_needs_an_instance_for_the_account_and_a_binding_for_the_conversation(lodge):
    with pytest.raises(NoConnectorBinding):
        lodge.store.enqueue(channel="telegram", account="bot:2", conversation="c1", text="x")
    with pytest.raises(NoConnectorBinding):
        lodge.store.enqueue(channel="telegram", account="bot:1", conversation="c2", text="x")
    with pytest.raises(NoConnectorBinding):
        lodge.store.enqueue(channel="slack", account="bot:1", conversation="c1", text="x")


async def test_lease_send_ack_delivers_with_its_attachment(lodge, tmp_path):
    note = tmp_path / "note.ogg"
    note.write_bytes(b"opus" * 10)
    delivery_id = lodge.enqueue("listen", attachment=note, attachment_kind="voice")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        results = await connector.run_once()
    assert results == [{"ok": True, "status": "delivered", "attempts": 0}]
    sent = connector.provider.sent[0]
    assert (sent["delivery_id"], sent["text"], sent["files"]) == (delivery_id, "listen", [b"opus" * 10])
    assert sent["conversation"] == {"provider_id": "c1", "thread": None}
    assert sent["reply_footer"] == "session=softie-1"
    record = lodge.deliveries()[delivery_id]
    assert (record["status"], record["provider_message_id"]) == ("delivered", "msg-1")
    assert list((lodge.store.root / "files").iterdir()) == []
    assert lodge.failed == []


async def test_lodge_restart_and_token_rotation_keep_the_queue(lodge):
    delivery_id = lodge.enqueue()
    # A lodge restart is a new store over the same files.
    restarted = ConnectorStore(lodge.store.root, clock=lodge.clock, limits=LIMITS)
    connector_api._store = restarted
    async with _client() as client:
        survivor = FakeConnector(client, lodge.token)
        leased = (await survivor.lease()).json()["deliveries"]
        assert [d["delivery_id"] for d in leased] == [delivery_id]
        # The runner is relaunched: a new generation. The old lease is released
        # for the new process; the old token can no longer ack it.
        new_token = restarted.issue_token("tg-main")
        stale = await survivor.ack(delivery_id, leased[0]["attempt_id"], provider_message_id="m")
        assert stale.status_code == 401
        relaunched = FakeConnector(client, new_token)
        again = (await relaunched.lease()).json()["deliveries"]
        assert [d["delivery_id"] for d in again] == [delivery_id]
        assert again[0]["attempt_id"] != leased[0]["attempt_id"]
        assert (await relaunched.ack(delivery_id, again[0]["attempt_id"], provider_message_id="m")).json()["status"] == "delivered"
    assert restarted.list_deliveries("tg-main")[0]["attempts"] == 0


async def test_disable_pauses_the_retry_clock_and_enable_resumes_it(lodge):
    delivery_id = lodge.enqueue()
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        connector.provider.failures.append(ProviderDown("timeout"))
        assert (await connector.run_once())[0]["status"] == "queued"
        lodge.store.disable("tg-main")
        lodge.clock.advance(LIMITS.retry_window_seconds * 3)
        assert (await connector.lease()).json()["paused"] is True
        lodge.store.enable("tg-main")
        lodge.clock.advance(2)
        results = await connector.run_once()
    assert results == [{"ok": True, "status": "delivered", "attempts": 1}]
    assert lodge.deliveries()[delivery_id]["status"] == "delivered"


async def test_remove_cancels_the_queue_revokes_and_detaches(lodge):
    delivery_id = lodge.enqueue()
    outcome = lodge.store.remove_instance("tg-main")
    assert outcome == {"cancelled": 1, "transferred": 0}
    assert lodge.store.list_instances() == [] and lodge.store.list_bindings() == []
    assert not (lodge.store.root / "queues" / "tg-main.json").exists()
    assert lodge.failed[0][1]["status"] == "cancelled"
    record = json.loads((lodge.store.root / "failures.jsonl").read_text())
    assert (record["delivery_id"], record["status"], record["delivered"]) == (delivery_id, "cancelled", False)
    async with _client() as client:
        assert (await FakeConnector(client, lodge.token).lease()).status_code == 401


async def test_remove_transfers_only_what_an_explicit_mapping_covers(lodge):
    lodge.store.bind("tg-main", sender="u1", conversation="c2", default_wolt="softie")
    moved = lodge.enqueue(conversation="c1")
    dropped = lodge.enqueue(conversation="c2")
    new_token = lodge.second_instance()
    same_ids = [{"from": {"account": "bot:1", "conversation": "c1"}, "to": {"account": "bot:1", "conversation": "c1"}}]
    with pytest.raises(ConnectorError, match="not a binding"):
        lodge.store.remove_instance("tg-main", transfer_to="tg-new", mapping=same_ids)
    mapping = [{"from": {"account": "bot:1", "conversation": "c1"}, "to": {"account": "bot:2", "conversation": "c9"}}]
    outcome = lodge.store.remove_instance("tg-main", transfer_to="tg-new", mapping=mapping)
    assert outcome == {"cancelled": 1, "transferred": 1}
    assert [failed["delivery_id"] for _instance, failed in lodge.failed] == [dropped]
    lodge.store.enable("tg-new")
    async with _client() as client:
        connector = FakeConnector(client, new_token)
        results = await connector.run_once()
    assert results[0]["status"] == "delivered"
    sent = connector.provider.sent[0]
    assert (sent["delivery_id"], sent["conversation"]["provider_id"]) == (moved, "c9")


async def test_lease_expiry_redelivers_and_the_stale_attempt_cannot_ack(lodge):
    delivery_id = lodge.enqueue()
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        first = (await connector.lease()).json()["deliveries"][0]
        assert (await connector.lease()).json()["deliveries"] == []
        lodge.clock.advance(LIMITS.lease_seconds + 1)
        second = (await connector.lease()).json()["deliveries"][0]
        assert (second["delivery_id"], second["attempt_id"] != first["attempt_id"]) == (delivery_id, True)
        stale = await connector.ack(delivery_id, first["attempt_id"], provider_message_id="m1")
        assert (stale.status_code, stale.json()["reason"]) == (409, "stale_attempt")
        good = await connector.ack(delivery_id, second["attempt_id"], provider_message_id="m2")
    assert good.json() == {"ok": True, "status": "delivered", "attempts": 1}


async def test_cross_instance_and_cross_generation_acks_are_refused(lodge):
    delivery_id = lodge.enqueue()
    other_token = lodge.second_instance()
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        leased = (await connector.lease()).json()["deliveries"][0]
        lodge.store.disable("tg-main")
        lodge.store.enable("tg-new")
        foreign = await FakeConnector(client, other_token).ack(delivery_id, leased["attempt_id"], provider_message_id="m")
        assert (foreign.status_code, foreign.json()["reason"]) == (409, "stale_attempt")
        lodge.store.disable("tg-new")
        lodge.store.enable("tg-main")
        next_generation = FakeConnector(client, lodge.store.issue_token("tg-main"))
        cross_generation = await next_generation.ack(delivery_id, leased["attempt_id"], provider_message_id="m")
        assert cross_generation.status_code == 409
        unknown = await next_generation.ack("0" * 32, leased["attempt_id"], provider_message_id="m")
        assert unknown.status_code == 409
        malformed = await next_generation.ack(delivery_id, "short", provider_message_id="m")
        assert malformed.status_code == 400
    assert lodge.deliveries()[delivery_id]["status"] == "queued"


async def test_repeat_ack_for_the_same_attempt_returns_the_first_answer(lodge):
    delivery_id = lodge.enqueue()
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        leased = (await connector.lease()).json()["deliveries"][0]
        first = await connector.ack(delivery_id, leased["attempt_id"], provider_message_id="m1")
        lodge.clock.advance(LIMITS.lease_seconds + 5)
        repeat = await connector.ack(delivery_id, leased["attempt_id"], provider_message_id="m1")
        changed = await connector.ack(delivery_id, leased["attempt_id"], ok=False, error="x", retry=False)
    assert first.status_code == repeat.status_code == changed.status_code == 200
    assert first.json() == repeat.json() == changed.json()
    assert lodge.deliveries()[delivery_id]["provider_message_id"] == "m1"


async def test_crash_after_provider_send_before_ack_duplicates_exactly_as_documented(lodge):
    delivery_id = lodge.enqueue("once, hopefully")
    async with _client() as client:
        provider = FakeProvider()
        crashed = FakeConnector(client, lodge.token, provider, crash_before_ack=True)
        assert (await crashed.run_once())[0]["crashed"] is True
        assert len(provider.sent) == 1
        lodge.clock.advance(LIMITS.lease_seconds + 1)
        restarted = FakeConnector(client, lodge.token, provider)
        results = await restarted.run_once()
    assert results == [{"ok": True, "status": "delivered", "attempts": 1}]
    assert [s["delivery_id"] for s in provider.sent] == [delivery_id, delivery_id]


async def test_retries_are_bounded_and_the_terminal_failure_is_reported(lodge):
    delivery_id = lodge.enqueue()
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        statuses = []
        for attempt in range(LIMITS.max_attempts):
            connector.provider.failures.append(ProviderDown(f"down {attempt}"))
            statuses.append((await connector.run_once())[0]["status"])
            lodge.clock.advance(2)
        assert (await connector.lease()).json()["deliveries"] == []
    assert statuses == ["queued"] * (LIMITS.max_attempts - 1) + ["failed"]
    assert connector.provider.sent == []
    record = lodge.deliveries()[delivery_id]
    assert (record["status"], record["attempts"]) == ("failed", LIMITS.max_attempts)
    assert "down 4" in record["error"]
    assert lodge.failed[0][1]["delivery_id"] == delivery_id
    failure = json.loads((lodge.store.root / "failures.jsonl").read_text())
    assert (failure["session"], failure["delivered"], failure["attempts"]) == ("softie-1", False, 5)


async def test_the_retry_window_ends_a_delivery_and_a_permanent_nack_ends_it_at_once(lodge):
    slow = lodge.enqueue("slow")
    refused = lodge.enqueue("refused")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        connector.provider.failures.extend([ProviderDown("busy"), ValueError("chat not found")])
        results = await connector.run_once()
        assert [r["status"] for r in results] == ["queued", "failed"]
        lodge.clock.advance(LIMITS.retry_window_seconds + 1)
        assert (await connector.lease()).json()["deliveries"] == []
    records = lodge.deliveries()
    assert records[refused]["error"] == "chat not found"
    assert (records[slow]["status"], records[slow]["attempts"]) == ("failed", 1)
    assert "retry window" in records[slow]["error"]


async def test_attachment_retention_running_out_while_disabled_fails_the_delivery(lodge, tmp_path):
    note = tmp_path / "late.ogg"
    note.write_bytes(b"late")
    delivery_id = lodge.enqueue(attachment=note)
    lodge.store.disable("tg-main")
    lodge.clock.advance(LIMITS.file_retention_seconds / 2)
    assert lodge.deliveries()[delivery_id]["status"] == "queued"
    assert len(list((lodge.store.root / "files").iterdir())) == 1
    lodge.clock.advance(LIMITS.file_retention_seconds)
    record = lodge.deliveries()[delivery_id]
    assert (record["status"], record["error"]) == ("failed", "attachment retention expired")
    assert list((lodge.store.root / "files").iterdir()) == []
    lodge.store.enable("tg-main")
    async with _client() as client:
        assert (await FakeConnector(client, lodge.token).lease()).json()["deliveries"] == []


async def test_in_flight_leases_are_capped_and_the_batch_is_bounded(lodge):
    for i in range(LIMITS.max_in_flight + 2):
        lodge.enqueue(f"m{i}")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        first = (await connector.lease(100)).json()["deliveries"]
        assert len(first) == LIMITS.max_in_flight
        capped = await connector.lease(1)
        assert (capped.status_code, capped.json()["reason"]) == (429, "deliveries_in_flight")
        assert (await connector.lease(0)).status_code == 400
        await connector.ack(first[0]["delivery_id"], first[0]["attempt_id"], provider_message_id="m")
        assert len((await connector.lease(1)).json()["deliveries"]) == 1


async def test_a_waiting_lease_returns_when_work_arrives_and_dies_with_its_token(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)

        async def enqueue_soon():
            await asyncio.sleep(0.05)
            lodge.enqueue("while you wait")

        response, _ = await asyncio.gather(connector.lease(1, wait=5), enqueue_soon())
        assert [d["text"] for d in response.json()["deliveries"]] == ["while you wait"]
        await connector.ack(
            response.json()["deliveries"][0]["delivery_id"],
            response.json()["deliveries"][0]["attempt_id"], provider_message_id="m",
        )

        async def revoke_soon():
            await asyncio.sleep(0.05)
            lodge.store.revoke_tokens("tg-main")

        response, _ = await asyncio.gather(connector.lease(1, wait=5), revoke_soon())
    assert response.status_code == 401


# ------------------------------------------------------------------- files


async def test_files_need_the_current_generations_active_lease_for_that_attempt(lodge, tmp_path):
    note = tmp_path / "n.ogg"
    note.write_bytes(b"audio")
    delivery_id = lodge.enqueue(attachment=note)
    other_token = lodge.second_instance()
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        leased = (await connector.lease()).json()["deliveries"][0]
        file_id, attempt_id = leased["attachments"][0]["file_id"], leased["attempt_id"]
        good = await connector.fetch_file(file_id, attempt_id)
        assert (good.status_code, good.content) == (200, b"audio")
        assert good.headers["x-woltspace-attachment-name"] == "n.ogg"
        assert good.headers["cache-control"] == "no-store"
        assert (await connector.fetch_file(file_id, None)).status_code == 404
        assert (await connector.fetch_file(file_id, "0" * 32)).status_code == 404
        # Another instance, even with the right attempt id.
        lodge.store.disable("tg-main")
        lodge.store.enable("tg-new")
        assert (await FakeConnector(client, other_token).fetch_file(file_id, attempt_id)).status_code == 404
        lodge.store.disable("tg-new")
        lodge.store.enable("tg-main")
        # A stale attempt after the lease expired, then a fresh one works again.
        lodge.clock.advance(LIMITS.lease_seconds + 1)
        assert (await connector.fetch_file(file_id, attempt_id)).status_code == 404
        again = (await connector.lease()).json()["deliveries"][0]
        assert (await connector.fetch_file(file_id, again["attempt_id"])).status_code == 200
        # A new generation cannot use the old generation's attempt.
        rotated = FakeConnector(client, lodge.store.issue_token("tg-main"))
        assert (await rotated.fetch_file(file_id, again["attempt_id"])).status_code == 404
        final = (await rotated.lease()).json()["deliveries"][0]
        assert (await rotated.fetch_file(file_id, final["attempt_id"])).status_code == 200
        # Acked: the file expired with the delivery.
        await rotated.ack(delivery_id, final["attempt_id"], provider_message_id="m")
        assert (await rotated.fetch_file(file_id, final["attempt_id"])).status_code == 404
    assert list((lodge.store.root / "files").iterdir()) == []


async def test_revoking_mid_stream_cuts_the_file_at_the_next_chunk(lodge, tmp_path, monkeypatch):
    big = tmp_path / "big.bin"
    big.write_bytes(b"z" * (connector_api._CHUNK * 3))
    lodge.enqueue(attachment=big)
    checks = []
    authenticate = lodge.store.authenticate

    def revoking_authenticate(token):
        checks.append(token)
        if len(checks) == 4:
            lodge.store.revoke_tokens("tg-main")
        return authenticate(token)

    monkeypatch.setattr(lodge.store, "authenticate", revoking_authenticate)
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        leased = (await connector.lease()).json()["deliveries"][0]
        received = b""
        try:
            async with client.stream(
                "GET", f"/connector/v1/files/{leased['attachments'][0]['file_id']}",
                params={"attempt_id": leased["attempt_id"]}, headers=connector.headers,
            ) as response:
                async for chunk in response.aiter_bytes():
                    received += chunk
        except httpx.HTTPError:
            pass
    assert 0 < len(received) < connector_api._CHUNK * 3
