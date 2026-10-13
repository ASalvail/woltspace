"""The Telegram connector against a fake Bot API (MockTransport) and the real
lodge connector API. No bot token, no api.telegram.org: every call is the fake's."""

import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))
sys.path.insert(0, str(ROOT / "connectors" / "telegram"))

import server.app as server_app  # noqa: E402
from server import connector_api  # noqa: E402
from server.connectors import AcceptedEvent, ConnectorStore, Limits  # noqa: E402
from woltspace_telegram import LodgeRefused, TelegramConnector, split_text  # noqa: E402

BOT_TOKEN = "123456:FAKE-not-a-real-token"
# The connector addresses the Bot API by its real host, so the fake transport
# must answer for that host. It is the only place the name appears: every
# client here is built on httpx.MockTransport, so no request can leave.
BOT_API = "https://" + "api.telegram.org"
LIMITS = Limits(outbound_file_max_bytes=1_000_000, upload_max_bytes=100_000, lease_seconds=60)


class FakeBotApi:
    """Enough of the Bot API for the connector: updates in, sends out, files served."""

    def __init__(self):
        self.updates: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.sent: list[dict] = []
        self.files: dict[str, bytes] = {}
        self.fail: dict[str, dict] = {}     # method -> error body for the next call
        self.next_message_id = 100
        self.polled_offsets: list[int | None] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.url.host == BOT_API.removeprefix("https://")
        if path.startswith(f"/file/bot{BOT_TOKEN}/"):
            name = path.split("/")[-1]
            if name in self.files:
                return httpx.Response(200, content=self.files[name])
            return httpx.Response(404, json={"ok": False})
        assert path.startswith(f"/bot{BOT_TOKEN}/"), path
        method = path.rsplit("/", 1)[-1]
        if request.headers.get("content-type", "").startswith("application/json"):
            params = json.loads(request.content)
        else:
            params = {"_multipart": request.headers.get("content-type", ""), "_size": len(request.content)}
            raw = request.content
            for field in ("chat_id", "message_thread_id"):
                marker = f'name="{field}"\r\n\r\n'.encode()
                if marker in raw:
                    params[field] = raw.split(marker, 1)[1].split(b"\r\n", 1)[0].decode()
            for field in ("voice", "audio", "document"):
                if f'name="{field}"'.encode() in raw:
                    params["_field"] = field
        self.calls.append((method, params))
        if method in self.fail:
            body = self.fail.pop(method)
            return httpx.Response(200, json={"ok": False, **body})
        if method == "getUpdates":
            self.polled_offsets.append(params.get("offset"))
            offset = params.get("offset")
            result = [u for u in self.updates if offset is None or u["update_id"] >= offset]
            return httpx.Response(200, json={"ok": True, "result": result})
        if method == "getFile":
            file_id = params["file_id"]
            return httpx.Response(200, json={"ok": True, "result": {"file_id": file_id, "file_path": f"files/{file_id}"}})
        if method in ("sendMessage", "sendDocument", "sendVoice", "sendAudio"):
            self.next_message_id += 1
            record = {"method": method, "message_id": self.next_message_id, **params}
            self.sent.append(record)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_message_id}})
        return httpx.Response(200, json={"ok": False, "description": f"unknown method {method}"})


def _update(update_id, text=None, *, chat=101, user=101, reply_to=None, **message):
    body = {"message_id": update_id * 10, "from": {"id": user, "username": "jer"}, "chat": {"id": chat}, **message}
    if text is not None:
        body["text"] = text
    if reply_to is not None:
        body["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": body}


class Lodge:
    def __init__(self, tmp_path):
        self.store = ConnectorStore(tmp_path / "connectors", limits=LIMITS)
        self.routed: list[AcceptedEvent] = []
        self.store.router = lambda event: self.routed.append(event) or {"routed": "test"}
        self.store.add_instance("tg", "telegram", "bot:123456")
        self.binding = self.store.bind("tg", sender="101", conversation="101", default_wolt="softie")
        self.store.enable("tg")
        self.token = self.store.issue_token("tg")

    def enqueue(self, text="hello", **kwargs) -> str:
        return self.store.enqueue(
            channel="telegram", account=None, conversation="101", text=text, session="softie-1",
            reply_footer="\n\n---\n↩️ reply to this message\nhttps://lodge/tui?session=softie-1", **kwargs,
        )

    def deliveries(self):
        return {d["delivery_id"]: d for d in self.store.list_deliveries("tg")}


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    lodge = Lodge(tmp_path)
    monkeypatch.setattr(connector_api, "CONNECTORS_DIR", lodge.store.root)
    monkeypatch.setattr(connector_api, "_store", lodge.store)
    monkeypatch.setattr(connector_api, "_POLL_INTERVAL", 0.01)
    return lodge


@pytest.fixture
def api():
    return FakeBotApi()


def _connector(lodge, api, token=None) -> TelegramConnector:
    lodge_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server_app.app, client=("127.0.0.1", 1)),
        base_url="http://localhost:7777",
    )
    bot = httpx.AsyncClient(transport=api.transport(), base_url=BOT_API)
    return TelegramConnector(lodge_client, bot, bot_token=BOT_TOKEN, connector_token=token or lodge.token)


# ---------------------------------------------------------------- inbound


async def test_text_updates_become_accepted_events_and_the_cursor_follows(lodge, api):
    api.updates = [_update(41, "hello"), _update(42, "/start"), _update(43, "from a stranger", user=202)]
    connector = _connector(lodge, api)
    state = await connector.start()
    assert state["cursor"] is None
    report = await connector.poll_once(timeout=0)
    assert api.polled_offsets == [None]
    assert [e["body"].get("accepted") for e in report.events] == [True, True, False]
    assert report.events[2]["body"]["reason"] == "sender_not_allowed"
    assert report.cursor == 44
    # Commands are passed through as text in this slice.
    assert [e.text for e in lodge.routed] == ["hello", "/start"]
    assert lodge.routed[0].event_id == "tg:41" and lodge.routed[0].cursor == 41
    assert lodge.routed[0].sender == {"provider_id": "101", "display": "jer"}
    # The next poll asks from the cursor, and a restart takes it from core.
    await connector.poll_once(timeout=0)
    assert api.polled_offsets[-1] == 44
    fresh = _connector(lodge, api)
    assert (await fresh.start())["cursor"] == 44
    assert len(lodge.routed) == 2


async def test_the_cursor_never_moves_past_an_update_the_lodge_did_not_answer(lodge, api, monkeypatch):
    api.updates = [_update(41, "one"), _update(42, "two"), _update(43, "three")]
    connector = _connector(lodge, api)
    original = lodge.store.accept_event
    calls = []

    def flaky(who, payload):
        calls.append(payload["event_id"])
        if payload["event_id"] == "tg:42" and len(calls) < 3:
            from server.connectors import ConnectorStateUnreadable
            raise ConnectorStateUnreadable("disk hiccup")
        return original(who, payload)

    monkeypatch.setattr(lodge.store, "accept_event", flaky)
    report = await connector.poll_once(timeout=0)
    assert [e["status"] for e in report.events] == [200, 503]
    assert report.cursor == 42
    assert [e.event_id for e in lodge.routed] == ["tg:41"]
    # The next poll re-fetches from 42; the lodge answers it and 43, and 41 is
    # not asked for again.
    report = await connector.poll_once(timeout=0)
    assert api.polled_offsets[-1] == 42
    assert [e["status"] for e in report.events] == [200, 200]
    assert report.cursor == 44
    assert [e.event_id for e in lodge.routed] == ["tg:41", "tg:42", "tg:43"]
    # A re-poll of an already answered update is answered from dedup, once more, no new handoff.
    api.updates.append(_update(41, "one"))
    await connector.poll_once(timeout=0)
    assert len(lodge.routed) == 3


async def test_media_is_downloaded_uploaded_and_named_in_the_event(lodge, api):
    api.files["v1"] = b"OggS voice"
    api.files["d1"] = b"%PDF-1.4"
    api.updates = [
        _update(51, voice={"file_id": "v1", "file_unique_id": "uv1"}, caption="listen"),
        _update(52, document={"file_id": "d1", "file_unique_id": "ud1", "file_name": "../../report.pdf"}),
        _update(53, photo=[{"file_id": "p1", "file_unique_id": "up1", "file_size": 10}, {"file_id": "p2", "file_unique_id": "up2", "file_size": 99}]),
    ]
    api.files["p2"] = b"\xff\xd8 big photo"
    connector = _connector(lodge, api)
    report = await connector.poll_once(timeout=0)
    assert [e["body"]["accepted"] for e in report.events] == [True, True, True]
    voice, document, photo = lodge.routed
    assert voice.text == "listen" and voice.attachments[0]["name"] == "voice_uv1.ogg"
    assert Path(voice.attachments[0]["path"]).read_bytes() == b"OggS voice"
    assert document.attachments[0]["name"] == "report.pdf"
    assert photo.attachments[0]["name"] == "photo_up2.jpg"
    assert Path(photo.attachments[0]["path"]).read_bytes() == b"\xff\xd8 big photo"
    # The largest photo size was fetched, nothing else.
    assert [p["file_id"] for m, p in api.calls if m == "getFile"] == ["v1", "d1", "p2"]


async def test_a_file_the_api_cannot_serve_still_delivers_the_text(lodge, api):
    api.updates = [_update(61, document={"file_id": "missing", "file_unique_id": "um"}, caption="see attached")]
    connector = _connector(lodge, api)
    report = await connector.poll_once(timeout=0)
    assert report.events[0]["body"]["accepted"] is True
    assert lodge.routed[0].attachments == ()
    assert "could not be fetched" in lodge.routed[0].text


async def test_replies_to_the_bots_messages_route_through_the_message_map(lodge, api):
    delivery_id = lodge.enqueue("a long answer " * 400)
    connector = _connector(lodge, api)
    report = await connector.deliver_once()
    ack = report.acks[0]["body"]
    assert ack["status"] == "delivered"
    parts = [m for m in api.sent if m["method"] == "sendMessage"]
    assert len(parts) == 2 and all(len(m["text"]) <= 4096 for m in parts)
    assert parts[-1]["text"].endswith("https://lodge/tui?session=softie-1")
    ids = lodge.deliveries()[delivery_id]["provider_message_ids"]
    assert ids == [str(m["message_id"]) for m in parts]
    # A reply to the first part (no footer on it) still finds the session.
    api.updates = [_update(71, "re: part one", reply_to=parts[0]["message_id"]),
                   _update(72, "re: nothing", reply_to=424242)]
    report = await connector.poll_once(timeout=0)
    assert report.events[0]["body"]["session"] == "softie-1"
    assert report.events[1]["body"]["reason"] == "reply_to_out_of_binding"
    assert lodge.routed[0].reply_to_session == "softie-1"


# ---------------------------------------------------------------- outbound


def test_split_text_cuts_at_line_ends_within_the_limit():
    text = "\n".join(f"line {i:04d}" for i in range(1000))
    parts = split_text(text)
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")
    assert all(not p.startswith("\n") for p in parts)
    assert split_text("") == [""]
    assert split_text("x" * 5000) == ["x" * 4096, "x" * 904]


async def test_attachments_go_as_voice_audio_or_document_with_document_fallback(lodge, api, tmp_path):
    for name, kind in (("note.ogg", "voice"), ("song.mp3", "file"), ("report.pdf", "file"), ("talk.ogg", "voice")):
        path = tmp_path / name
        path.write_bytes(b"bytes of " + name.encode())
        lodge.enqueue(f"here is {name}", attachment=path, attachment_kind=kind)
    api.fail["sendVoice"] = {"error_code": 400, "description": "Bad Request: VOICE_MESSAGES_FORBIDDEN"}
    connector = _connector(lodge, api)
    # The forbidden voice is the first delivery leased; it falls back to a document.
    report = await connector.deliver_once()
    assert [a["body"]["status"] for a in report.acks] == ["delivered"] * 4
    methods = [m["method"] for m in api.sent if m["method"] != "sendMessage"]
    assert methods == ["sendDocument", "sendAudio", "sendDocument", "sendVoice"]
    assert [m["_field"] for m in api.sent if m["method"] != "sendMessage"] == ["document", "audio", "document", "voice"]
    for delivery in lodge.deliveries().values():
        assert len(delivery["provider_message_ids"]) == 2


async def test_telegram_errors_are_nacked_retryable_or_terminal(lodge, api):
    busy = lodge.enqueue("busy")
    api.fail["sendMessage"] = {"error_code": 429, "description": "Too Many Requests: retry after 3"}
    connector = _connector(lodge, api)
    report = await connector.deliver_once()
    assert report.acks[0]["body"]["status"] == "queued"
    gone = lodge.enqueue("gone")
    api.fail["sendMessage"] = {"error_code": 403, "description": "Forbidden: bot was blocked by the user"}
    report = await connector.deliver_once()
    assert report.acks[0]["body"]["status"] == "failed"
    records = lodge.deliveries()
    assert records[busy]["failed_attempts"] == 1 and records[busy]["status"] == "queued"
    assert records[gone]["status"] == "failed" and "blocked" in records[gone]["error"]


async def test_a_partial_send_is_a_failed_attempt_and_may_repeat_the_sent_parts(lodge, api):
    delivery_id = lodge.enqueue("x" * 5000)
    connector = _connector(lodge, api)
    calls = []
    real = api.handle

    def second_part_times_out(request):
        if request.url.path.endswith("/sendMessage"):
            calls.append(1)
            if len(calls) == 2:
                raise httpx.ReadTimeout("timed out", request=request)
        return real(request)

    connector.bot = httpx.AsyncClient(transport=httpx.MockTransport(second_part_times_out), base_url=BOT_API)
    report = await connector.deliver_once()
    assert report.acks[0]["body"]["status"] == "queued"
    assert len([m for m in api.sent if m["method"] == "sendMessage"]) == 1
    # Documented duplicate: the retry resends part one.
    lodge.store.clock = lambda: __import__("time").time() + 10
    report = await connector.deliver_once()
    assert report.acks[0]["body"]["status"] == "delivered"
    assert len([m for m in api.sent if m["method"] == "sendMessage"]) == 3
    assert len(lodge.deliveries()[delivery_id]["provider_message_ids"]) == 2


async def test_a_paused_or_revoked_lodge_stops_the_connector(lodge, api):
    connector = _connector(lodge, api)
    lodge.store.disable("tg")
    assert (await connector.deliver_once()).paused is True
    lodge.store.revoke_tokens("tg")
    with pytest.raises(LodgeRefused):
        await connector.deliver_once()
    api.updates = [_update(91, "anyone there?")]
    with pytest.raises(LodgeRefused):
        await connector.poll_once(timeout=0)
    # Nothing was confirmed on the losing side: the update is still Telegram's to hand out.
    assert api.polled_offsets[-1] is None


async def test_the_bot_token_never_reaches_a_log_line(lodge, api, caplog):
    api.updates = [_update(81, "hi")]
    connector = _connector(lodge, api)

    def explode(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    connector.bot = httpx.AsyncClient(transport=httpx.MockTransport(explode), base_url=BOT_API)
    with caplog.at_level(logging.WARNING):
        await connector.poll_once(timeout=0)
    assert caplog.records and BOT_TOKEN not in caplog.text
    assert "<token>" in caplog.text
    lodge.enqueue("hello")
    with caplog.at_level(logging.WARNING):
        report = await connector.deliver_once()
    assert BOT_TOKEN not in json.dumps(report.acks)
    assert BOT_TOKEN not in json.dumps(lodge.deliveries())
