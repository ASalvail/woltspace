"""Text-only connector for the official WhatsApp Agent Platform API.

This API is creator-only and not end-to-end encrypted. It is separate from
WhatsApp Business Cloud API and linked-device automation.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from bot.core import message_session, start_claude_session
from wolts import list_wolts
from lib.paths import space_dir
from lib.notify_prompt import notify_reply_instruction
from woltspace.whatsapp_rate_limit import pace_request, request_slot

log = logging.getLogger("whatsapp")
API = "https://api.whatsapp.com/agent/v1"
TOKEN = os.environ["WHATSAPP_AGENT_TOKEN"]
WOLTS_DIR = Path(os.environ.get("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))
WOLT_NAME = os.environ.get("WOLTSPACE_WOLT_NAME", "wolt")
STATE = space_dir(WOLTS_DIR) / "whatsapp"
STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
ACCOUNT = hashlib.sha256(TOKEN.encode()).hexdigest()[:20]
CURSOR = STATE / f"cursor-{ACCOUNT}.json"
ROUTES = STATE / f"routes-{ACCOUNT}.json"
LOCK = STATE / f"poll-{ACCOUNT}.lock"
RODENT_TYPES = {"raccoon", "beaver", "otter", "rodent"}


class WhatsAppConfigurationError(RuntimeError):
    """An account or poller conflict needs operator action, not retries."""


class WhatsAppRateLimit(RuntimeError):
    def __init__(self, retry_after: int = 5):
        self.retry_after = max(1, min(60, retry_after))
        super().__init__("WhatsApp Agent API rate limit reached")


def eligible_wolts() -> dict[str, dict]:
    return {
        item.get("name", ""): item for item in list_wolts()
        if item.get("name") and item.get("type") in RODENT_TYPES
    }


def api(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    if method == "POST" and path == "/messages":
        for attempt in range(3):
            try:
                with request_slot(STATE, TOKEN, 12, "send"):
                    return _api_request(method, path, payload)
            except WhatsAppRateLimit as exc:
                if attempt == 2:
                    raise
                time.sleep(exc.retry_after)
    elif method == "GET" and path.startswith("/updates?"):
        pace_request(STATE, TOKEN, 15, "poll")
    return _api_request(method, path, payload)


def _api_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        # Do not surface response bodies: they may echo submitted content.
        if exc.code == 204:
            return 204, {}
        if exc.code in (401, 403):
            raise WhatsAppConfigurationError(
                f"WhatsApp Agent API authentication failed (HTTP {exc.code}); check the Agent API key"
            ) from None
        if exc.code == 409:
            raise WhatsAppConfigurationError(
                "WhatsApp Agent API rejected this poller (HTTP 409); verify there is only one active poller"
            ) from None
        if exc.code == 429:
            try:
                retry_after = int(exc.headers.get("Retry-After", "5")) if exc.headers else 5
            except (TypeError, ValueError):
                retry_after = 5
            raise WhatsAppRateLimit(retry_after) from None
        raise RuntimeError(f"WhatsApp Agent API request failed (HTTP {exc.code})") from None


def atomic_json(path: Path, value: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".wa-", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def messages(body: dict):
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for item in value.get("messages", []):
                yield item


def handle(item: dict, routes: dict, activation: int) -> None:
    mid, sender = item.get("id"), item.get("from")
    if not isinstance(mid, str) or not isinstance(sender, str) or not sender.startswith("user:"):
        return
    try:
        timestamp = int(item.get("timestamp", "0"))
    except (TypeError, ValueError):
        return
    if timestamp < activation:
        return
    if item.get("type") != "text":
        api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
             "type": "text", "text": {"body": "I can handle text messages here; attachments are not supported yet."}})
        return
    text = (item.get("text") or {}).get("body", "").strip()
    if text.startswith("wolt "):
        name = text[5:].strip()
        if name in eligible_wolts():
            routes[sender] = {"wolt": name}
            atomic_json(ROUTES, routes)
            reply = f"Selected {name}. Send a message to start its session."
        else:
            reply = "That wolt is unavailable. Send `wolt NAME` with an installed wolt name."
        api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
             "type": "text", "text": {"body": reply}})
        return
    route = routes.get(sender)
    if not route or not route.get("wolt"):
        names = ", ".join(eligible_wolts())
        reply = "Choose a wolt first by sending `wolt NAME`. Available: " + (names or "none")
        api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
             "type": "text", "text": {"body": reply}})
        return
    # Persisted route is bound to this account's hashed key; selection cannot
    # carry over when credentials change. A first message creates a session;
    # later messages go only to the exact recorded session.
    if route["wolt"] not in eligible_wolts():
        routes.pop(sender, None)
        atomic_json(ROUTES, routes)
        api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
             "type": "text", "text": {"body": "That wolt is no longer available. Choose again with `wolt NAME`."}})
        return
    route_instruction = notify_reply_instruction("--whatsapp", sender)
    prompt = f"[WhatsApp message from {sender}]: {text}\n{route_instruction}"
    routing = {"adapter": "whatsapp", "chat_id": sender, "whatsapp_account": ACCOUNT}
    try:
        if route.get("session"):
            result = message_session(route["session"], prompt)
            if not result.get("ok"):
                raise RuntimeError("session did not accept message")
        else:
            session = start_claude_session(prompt, wolt=route["wolt"], routing=routing)
            route["session"] = session.get("name")
            routes[sender] = route
            atomic_json(ROUTES, routes)
            link = session.get("url", "")
            if link:
                api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
                     "type": "text", "text": {"body": f"Session started: {link}"}})
    except Exception:
        log.error("Could not start or resume the WhatsApp session (%s)", type(sys.exc_info()[1]).__name__)
        api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
             "type": "text", "text": {"body": "I could not start a session. Please try again."}})


def poll_once(state: dict, routes: dict, activation: int, *, cursor_path: Path = CURSOR,
              request=api, dispatch=handle) -> dict:
    """Fetch and checkpoint one batch. A 204 keeps the current cursor."""
    query = urllib.parse.urlencode({"offset": int(state.get("offset", 0)), "limit": 50, "timeout": 20})
    status, body = request("GET", "/updates?" + query)
    if status == 204:
        return state
    if not isinstance(body, dict):
        raise ValueError("WhatsApp updates response is malformed")
    next_offset = body.get("next_offset")
    batch = list(messages(body))
    if not isinstance(next_offset, int) or next_offset < int(state.get("offset", 0)):
        raise ValueError("WhatsApp updates response has no valid next_offset")
    state["offset"] = next_offset
    atomic_json(cursor_path, state)
    processed = list(state.get("processed", []))
    known = set(processed)
    for item in batch:
        mid = item.get("id")
        if not isinstance(mid, str) or mid in known:
            continue
        try:
            too_old = int(item.get("timestamp", "0")) < activation
        except (TypeError, ValueError):
            too_old = True
        processed.append(mid)
        known.add(mid)
        state["processed"] = processed[-10000:]
        atomic_json(cursor_path, state)
        if not too_old:
            dispatch(item, routes, activation)
    return state


def main() -> None:
    with LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("WhatsApp API key already has an active poller")
        state = load(CURSOR)
        activation = state.get("activation")
        if not isinstance(activation, int):
            activation = int(time.time())
            state = {"offset": 0, "activation": activation, "processed": []}
            atomic_json(CURSOR, state)
        routes = load(ROUTES)
        failures = 0
        while True:
            try:
                state = poll_once(state, routes, activation)
                failures = 0
            except WhatsAppConfigurationError as exc:
                log.error("%s", exc)
                raise SystemExit(1) from None
            except Exception as exc:
                failures += 1
                if failures >= 5:
                    raise RuntimeError("WhatsApp polling failed five consecutive times") from None
                delay = min(60, 5 * (2 ** (failures - 1)))
                log.error("WhatsApp poll failed (%s); retry %d/5 in %ds", type(exc).__name__, failures, delay)
                time.sleep(delay)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
