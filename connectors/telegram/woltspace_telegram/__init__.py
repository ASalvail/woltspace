"""Telegram on the connector protocol.

One process, two loops: `poll_once` turns Bot API updates into lodge events
and advances the cursor core keeps; `deliver_once` leases deliveries and
sends them. The connector decides nothing about who may talk to whom or
where a message goes; it carries bytes between the two APIs.

Inbound, per update: text or caption becomes the event text; a photo (its
largest size), document, voice note, audio or video is downloaded through
`getFile` and uploaded to the lodge first, then named in the event. Slash
commands are passed through as text in this slice: the chat command set
(`/wolt`, `/new`, `/kill`, `/sessions`, inline buttons) is core-mediated and
comes with slice 2b. A reply to one of the bot's messages carries that
message's id, and core resolves it to the delivery and session inside the
exact binding; footer text is never routing.

The cursor: core hands it over at start (`GET state`); after every update
the lodge has answered (accepted, dropped, or 409 conflict) the connector
posts `update_id + 1`. Core refuses a cursor past what it answered, so a
crash before the post re-fetches the update and core answers from dedup. No
cursor at all (first start after the adapter) means asking Telegram for its
unconfirmed backlog, which is exactly what the adapter had not processed.

Outbound: text goes as `sendMessage` in parts of at most 4096 characters,
the reply footer on the last one; a `voice` attachment as `sendVoice`, an
.mp3/.m4a as `sendAudio`, anything else (and anything Telegram refuses as
voice or audio) as `sendDocument`. The ack lists every part's message id.
Telegram 429 and 5xx, and network trouble, are nacked as retryable; 400
and 403 are terminal.

The bot token lives in `TELEGRAM_BOT_TOKEN` and the file download URL that
embeds it is never logged. httpx only; the two clients take any transport,
which is how tests run it against a fake Bot API and the real lodge app.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

PREFIX = "/connector/v1"
TELEGRAM_MAX_TEXT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
VOICE_SUFFIXES = (".ogg", ".oga", ".opus")
AUDIO_SUFFIXES = (".mp3", ".m4a")
POLL_TIMEOUT = 25
LEASE_WAIT = 20
DOWNLOAD_CAP = 50 * 1024 * 1024

logger = logging.getLogger(__name__)


class LodgeRefused(RuntimeError):
    """The lodge answered 401 or 403: this process must stop."""


@dataclass
class Report:
    """What one turn of a loop did, for tests and the log."""

    events: list[dict] = field(default_factory=list)
    acks: list[dict] = field(default_factory=list)
    cursor: int | None = None
    paused: bool = False


def split_text(text: str, limit: int = TELEGRAM_MAX_TEXT) -> list[str]:
    """Telegram-sized parts, cut at line ends when there is one in reach."""
    if not text:
        return [""]
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


def _redact(error: Exception, token: str) -> str:
    text = str(error)
    return text.replace(token, "<token>") if token else text


class TelegramConnector:
    def __init__(
        self, lodge: httpx.AsyncClient, bot: httpx.AsyncClient, *, bot_token: str, connector_token: str,
    ):
        self.lodge = lodge
        self.bot = bot
        self._bot_token = bot_token
        self._connector_token = connector_token
        self.cursor: int | None = None
        self.started = False

    # ----------------------------------------------------------------- lodge

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._connector_token}"}

    async def _lodge(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        response = await self.lodge.request(method, f"{PREFIX}{path}", headers=headers, **kwargs)
        if response.status_code in (401, 403):
            raise LodgeRefused(f"lodge answered {response.status_code}")
        return response

    async def start(self) -> dict:
        """Take the cursor core kept; nothing is polled before this."""
        state = (await self._lodge("GET", "/state")).json()
        self.cursor = state.get("cursor")
        self.started = True
        return state

    # --------------------------------------------------------------- telegram

    async def _call(self, method: str, **params) -> httpx.Response:
        return await self.bot.post(f"/bot{self._bot_token}/{method}", json=params)

    async def _upload_call(self, method: str, field_name: str, data: bytes, name: str, params: dict) -> httpx.Response:
        return await self.bot.post(
            f"/bot{self._bot_token}/{method}", data=params, files={field_name: (name, data)},
        )

    async def _download(self, file_id: str) -> bytes | None:
        info = await self._call("getFile", file_id=file_id)
        body = info.json() if info.headers.get("content-type", "").startswith("application/json") else {}
        path = (body.get("result") or {}).get("file_path") if body.get("ok") else None
        if not path:
            return None
        response = await self.bot.get(f"/file/bot{self._bot_token}/{path}")
        if response.status_code != 200 or len(response.content) > DOWNLOAD_CAP:
            return None
        return response.content

    # ---------------------------------------------------------------- inbound

    @staticmethod
    def _attachment_of(message: dict) -> tuple[str, str, str] | None:
        """(file_id, name, kind) of the one file a message carries, or None."""
        if message.get("photo"):
            largest = max(message["photo"], key=lambda size: size.get("file_size", 0))
            return largest["file_id"], f"photo_{largest['file_unique_id']}.jpg", "file"
        if message.get("voice"):
            return message["voice"]["file_id"], f"voice_{message['voice']['file_unique_id']}.ogg", "voice"
        if message.get("audio"):
            audio = message["audio"]
            return audio["file_id"], audio.get("file_name") or f"audio_{audio['file_unique_id']}.mp3", "file"
        if message.get("video"):
            video = message["video"]
            return video["file_id"], video.get("file_name") or f"video_{video['file_unique_id']}.mp4", "file"
        if message.get("document"):
            document = message["document"]
            return document["file_id"], document.get("file_name") or f"document_{document['file_unique_id']}", "file"
        return None

    async def _event_for(self, update: dict) -> dict | None:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict) or not message.get("from") or not message.get("chat"):
            return None
        text = message.get("text") or message.get("caption") or ""
        attachments = []
        found = self._attachment_of(message)
        if found is not None:
            file_id, name, _kind = found
            data = await self._download(file_id)
            if data is None:
                text = f"{text}\n[a file came with this message and could not be fetched]".strip()
            else:
                upload = await self._lodge(
                    "POST", "/uploads", params={"name": name}, content=data,
                    headers={**self._headers, "Content-Type": "application/octet-stream"},
                )
                if upload.status_code == 200:
                    attachments.append({"upload_id": upload.json()["upload_id"]})
                else:
                    text = f"{text}\n[a file came with this message and the lodge refused it: {upload.json().get('reason', upload.status_code)}]".strip()
        event = {
            "event_id": f"tg:{update['update_id']}",
            "sender": {
                "provider_id": str(message["from"]["id"]),
                "display": (message["from"].get("username") or message["from"].get("first_name") or "")[:256],
            },
            "conversation": {
                "provider_id": str(message["chat"]["id"]),
                "thread": str(message["message_thread_id"]) if message.get("message_thread_id") else None,
            },
            "text": text[:16 * 1024],
            "attachments": attachments,
            "cursor": update["update_id"],
        }
        replied = message.get("reply_to_message")
        if isinstance(replied, dict) and replied.get("message_id") is not None:
            event["reply_to"] = {"provider_message_id": str(replied["message_id"])}
        return event

    async def poll_once(self, *, timeout: int = POLL_TIMEOUT) -> Report:
        """One getUpdates; every update is answered by the lodge before the cursor moves past it."""
        if not self.started:
            await self.start()
        report = Report(cursor=self.cursor)
        params = {"timeout": timeout, "allowed_updates": ["message", "edited_message"]}
        if self.cursor is not None:
            params["offset"] = self.cursor
        try:
            response = await self._call("getUpdates", **params)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("getUpdates failed: %s", _redact(exc, self._bot_token))
            return report
        if not body.get("ok"):
            logger.warning("getUpdates not ok: %s", body.get("description", "")[:200])
            return report
        for update in body.get("result", []):
            event = await self._event_for(update)
            answered = True
            if event is not None:
                response = await self._lodge("POST", "/events", json=event)
                report.events.append({"status": response.status_code, "body": response.json()})
                # 200 (accepted or dropped) and 409 (conflict) are final. 429
                # and 5xx are not: stop here, the next poll re-fetches it.
                answered = response.status_code in (200, 409)
            if not answered:
                break
            cursor = await self._lodge("POST", "/cursor", json={"cursor": update["update_id"] + 1})
            if cursor.status_code != 200:
                logger.warning("cursor refused: %s", cursor.json().get("reason"))
                break
            self.cursor = cursor.json()["cursor"]
            report.cursor = self.cursor
        return report

    # --------------------------------------------------------------- outbound

    def _classify(self, reason: str, status: int) -> bool:
        """Retryable? Telegram 429 and 5xx, yes; 400 and 403, no."""
        return status == 429 or status >= 500

    async def _send_delivery(self, delivery: dict) -> tuple[list[str], str | None, bool]:
        """Send one delivery; returns (message ids, error, retryable)."""
        chat_id = delivery["conversation"]["provider_id"]
        extra = {}
        if delivery["conversation"].get("thread"):
            extra["message_thread_id"] = int(delivery["conversation"]["thread"])
        message_ids: list[str] = []
        footer = delivery.get("reply_footer") or ""
        text = delivery["text"] or ""
        parts = split_text(text + footer) if footer else split_text(text)
        if not text and not footer:
            parts = []
        for part in parts:
            try:
                response = await self._call("sendMessage", chat_id=chat_id, text=part, **extra)
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                return message_ids, _redact(exc, self._bot_token), True
            if not body.get("ok"):
                return message_ids, body.get("description", "telegram error")[:500], self._classify(
                    body.get("description", ""), body.get("error_code", 500),
                )
            message_ids.append(str(body["result"]["message_id"]))
        for attachment in delivery.get("attachments", []):
            fetched = await self._lodge(
                "GET", f"/files/{attachment['file_id']}", params={"attempt_id": delivery["attempt_id"]},
            )
            if fetched.status_code != 200:
                return message_ids, f"lodge did not hand over {attachment['name']}", True
            data = fetched.content
            name = attachment["name"]
            lowered = name.lower()
            if attachment.get("kind") == "voice" or lowered.endswith(VOICE_SUFFIXES):
                order = (("sendVoice", "voice"), ("sendDocument", "document"))
            elif lowered.endswith(AUDIO_SUFFIXES):
                order = (("sendAudio", "audio"), ("sendDocument", "document"))
            else:
                order = (("sendDocument", "document"),)
            sent = False
            for method, field_name in order:
                try:
                    response = await self._upload_call(method, field_name, data, name, {"chat_id": chat_id, **extra})
                    body = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    return message_ids, _redact(exc, self._bot_token), True
                if body.get("ok"):
                    message_ids.append(str(body["result"]["message_id"]))
                    sent = True
                    break
                if method == order[-1][0]:
                    return message_ids, body.get("description", "telegram error")[:500], self._classify(
                        body.get("description", ""), body.get("error_code", 500),
                    )
            if not sent:
                return message_ids, "telegram refused the file", False
        return message_ids, None, False

    async def deliver_once(self, *, max_items: int = 10, wait: float = 0) -> Report:
        """Lease, send, ack: one turn. Partial sends are acked as failures and may repeat."""
        if not self.started:
            await self.start()
        report = Report(cursor=self.cursor)
        leased = (await self._lodge("POST", "/deliveries/lease", json={"max": max_items, "wait": wait})).json()
        report.paused = bool(leased.get("paused"))
        for delivery in leased.get("deliveries", []):
            message_ids, error, retryable = await self._send_delivery(delivery)
            if error is None:
                body = {"attempt_id": delivery["attempt_id"], "ok": True, "provider_message_ids": message_ids}
            else:
                body = {"attempt_id": delivery["attempt_id"], "ok": False, "error": error[:1000], "retry": retryable}
            ack = await self._lodge("POST", f"/deliveries/{delivery['delivery_id']}/ack", json=body)
            report.acks.append({"delivery_id": delivery["delivery_id"], "status": ack.status_code, "body": ack.json()})
        return report
