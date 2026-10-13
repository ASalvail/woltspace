"""Chat commands, core-mediated: fixed commands, owner grants, binding-owned resources.

The connector carries text and button presses; it names no authority. Core
derives the binding from the authenticated instance and the event's sender
and conversation (that happened at acceptance), parses only the fixed
commands below out of the text, and checks the binding's current grant and
the named resource at execution, after dedup and after the handoff claim.
Every answer goes back through a delivery in the same binding.

    /wolt [name]      pick the wolt this chat talks to. No name: a picker of
                      the binding's `selectable_wolts` as buttons. A name must
                      be in that set.
    /new [name]       a new session for a selectable wolt (the active one by
                      default). Needs `may_create`, which is not `may_wake`;
                      bounded per binding by `create_per_hour` and
                      `create_concurrent` live sessions it created.
    /sessions         the sessions this binding created or routed to, and no
                      others: a projection of names and states.
    /kill [name]      stop one session this binding created or routed to.
                      Needs `may_stop_own_sessions`; never another session,
                      never everything.
    /start, /help     what this chat may do.

Anything else that starts with `/` is answered "not supported"; text that
does not is a message for the session, as before.

Every command with an effect is a durable operation keyed by the event,
recorded in the instance's queue before any effect: `reserved` (for `/new`,
the reservation is made under the store lock against the current grant and
counts toward the hourly and concurrent limits at once), then
`effect_in_progress` written *before* the session call, `started` (the
session named), `associated`, `answered`; or `failed` (the reservation
released) or `uncertain` (parked). A retry resumes from the recorded state
and never repeats the effect. An operation found `effect_in_progress` after
a crash is never run again on its own: a `/new` is reconciled at the
session boundary by the nonce the start carried in its routing
(`operation`) and parked as `uncertain` if no session answers to it; a
`/kill` is done if the session no longer runs and parked otherwise. A
parked operation keeps its reservation until the owner resolves it
(`woltspace connector operations --done/--abandon`); an owner retry of the
event only replays the parked answer. Resumed work re-checks the current
grants like a first execution. The store lock is never held across the
session calls. A button press is bound to its operation: the button is
consumed by that event, so a retry of the same event resumes while another
event gets neither the result nor a repeat.

Residual trust, stated plainly to the owner who grants: a malicious
connector can forge an allowed sender and use any grant turned on for that
binding.

Session access goes through callables (resolve, start, stop, live, find)
that default to the lodge's own `sessions` module, so tests mock the
boundary.
"""

from __future__ import annotations

import re
from typing import Callable

from .connectors import AcceptedEvent, ConnectorError, ConnectorStore, NoConnectorBinding, valid_id

COMMANDS = ("wolt", "new", "sessions", "kill", "start", "help")
_COMMAND_RE = re.compile(r"^/([a-z]+)(?:@\w+)?(?:\s+(\S+))?\s*$")
MAX_REPLY_CHARS = 1000
LIVE_STATES = ("running", "resting")


def parse_command(text: str) -> tuple[str, str | None] | None:
    """(command, argument) for a fixed command, or None for ordinary text.

    One optional argument, a bare token; anything else in the line makes it
    ordinary text for the session, never a command with extra arguments.
    """
    match = _COMMAND_RE.match(text or "")
    if match is None:
        return None
    command, argument = match.group(1), match.group(2)
    if command not in COMMANDS:
        return ("unsupported", command)
    return command, argument


def _default_start(wolt: str, prompt: str, routing: dict) -> dict:
    from sessions import start_session

    return start_session(wolt=wolt, prompt=prompt, routing=routing)


def _default_stop(session: str) -> dict:
    from sessions import stop_session

    return stop_session(session)


def _default_live(session: str) -> str | None:
    """The session's state if it is still known: 'running', 'resting', 'stopped', ..."""
    from sessions import SessionRegistry

    record = SessionRegistry().get(session)
    return (record or {}).get("status")


def _default_find(operation: str) -> str | None:
    """The session whose routing carries this operation nonce, if a start registered one."""
    from sessions import SessionRegistry

    for record in SessionRegistry().list():
        if (record.get("routing") or {}).get("operation") == operation:
            return record.get("name")
    return None


def _default_resolve(wolt: str) -> str | None:
    from sessions import resolve_active_session

    return resolve_active_session(wolt)


class Commands:
    def __init__(
        self, store: ConnectorStore, *,
        start: Callable[[str, str, dict], dict] = _default_start,
        stop: Callable[[str], dict] = _default_stop,
        live: Callable[[str], str | None] = _default_live,
        resolve: Callable[[str], str | None] = _default_resolve,
        find: Callable[[str], str | None] = _default_find,
    ):
        self.store = store
        self.start = start
        self.stop = stop
        self.live = live
        self.resolve = resolve
        self.find = find

    # ------------------------------------------------------------- answers

    def _tell(self, event: AcceptedEvent, text: str, buttons: list[list[dict]] | None = None) -> None:
        try:
            self.store.enqueue(
                channel=event.channel, account=event.account,
                conversation=event.conversation["provider_id"], thread=event.conversation.get("thread"),
                binding_id=event.binding["binding_id"], text=text[:MAX_REPLY_CHARS], buttons=buttons,
            )
        except (NoConnectorBinding, ConnectorError):
            pass

    def _audit(self, event: AcceptedEvent, command: str, outcome: str, **detail) -> dict:
        record = {
            "binding_id": event.binding["binding_id"], "event_id": event.event_id,
            "command": command, "outcome": outcome, **detail,
        }
        try:
            self.store.record_command(event.instance_id, record)
        except OSError:
            pass
        return {"routed": "command", "command": command, "outcome": outcome, **detail}

    def _done(self, event: AcceptedEvent, command: str, outcome: str, **detail) -> dict:
        """The operation's last word: answered, and the result kept for a replay."""
        result = self._audit(event, command, outcome, **detail)
        try:
            self.store.settle_operation(event.instance_id, event.dedup_key, "answered", result=result)
        except ConnectorError:
            pass
        return result

    # ------------------------------------------------------------ dispatch

    def handles(self, event: AcceptedEvent) -> bool:
        return event.callback is not None or parse_command(event.text) is not None

    def __call__(self, event: AcceptedEvent) -> dict:
        key = event.dedup_key
        previous = self.store.operation(event.instance_id, key)
        if previous is not None and previous["state"] in ("answered", "failed", "uncertain"):
            # Already over: the same answer, no second effect.
            return dict(previous.get("result") or {"routed": "command", "command": previous["command"], "outcome": previous["state"]})
        if event.callback is not None:
            action = self.store.claim_callback(event.instance_id, event.binding["binding_id"], event.callback, key)
            if action is None:
                self._tell(event, "🪵 that button has expired or is not yours; ask again")
                return self._audit(event, "callback", "refused", reason="callback_invalid")
            command, argument = str(action.get("command", "")), action.get("argument")
            if command not in COMMANDS:
                return self._audit(event, "callback", "refused", reason="callback_unknown_command")
        else:
            parsed = parse_command(event.text)
            if parsed is None:
                return {"routed": "not_a_command"}
            command, argument = parsed
        if argument is not None and not valid_id(str(argument), 64):
            self._tell(event, "🪵 that is not a name I can use")
            return self._audit(event, command, "refused", reason="invalid_argument")
        handler = {
            "wolt": self._wolt, "new": self._new, "sessions": self._sessions, "kill": self._kill,
            "start": self._help, "help": self._help, "unsupported": self._unsupported,
        }[command]
        if command in ("new", "kill"):
            previous = self.store.begin_operation(event.instance_id, key, event.binding["binding_id"], command, argument)
        return handler(event, argument, previous)

    # ------------------------------------------------------------ commands

    def _state(self, event: AcceptedEvent) -> dict:
        return self.store.routing_state(event.instance_id, event.binding["binding_id"])

    def _unsupported(self, event: AcceptedEvent, argument: str | None, previous: dict | None) -> dict:
        self._tell(event, f"🪵 /{argument} is not supported here")
        return self._audit(event, "unsupported", "refused", reason="unsupported", name=argument)

    def _help(self, event: AcceptedEvent, argument: str | None, previous: dict | None) -> dict:
        binding = event.binding
        lines = ["Commands this chat may use:"]
        if binding.get("selectable_wolts"):
            lines.append("/wolt  pick a wolt: " + ", ".join(binding["selectable_wolts"]))
        if binding.get("may_create"):
            lines.append("/new [wolt]  start a session")
        lines.append("/sessions  the sessions this chat started or talked to")
        if binding.get("may_stop_own_sessions"):
            lines.append("/kill <session>  stop one of them")
        self._tell(event, "\n".join(lines))
        return self._audit(event, "help", "answered")

    def _wolt(self, event: AcceptedEvent, argument: str | None, previous: dict | None) -> dict:
        selectable = list(event.binding.get("selectable_wolts") or [])
        if not selectable:
            self._tell(event, "🪵 this chat may not pick a wolt; ask the owner")
            return self._audit(event, "wolt", "refused", reason="no_grant")
        if argument is None:
            active = self._state(event).get("active_wolt") or event.binding["default_wolt"]
            buttons = [[{"text": ("✓ " if name == active else "") + name, "action": {"command": "wolt", "argument": name}}]
                       for name in selectable]
            self._tell(event, f"pick a wolt (now: {active})", buttons)
            return self._audit(event, "wolt", "picker")
        if argument not in selectable:
            self._tell(event, f"🪵 {argument} is not a wolt this chat may pick")
            return self._audit(event, "wolt", "refused", reason="not_selectable", wolt=argument)
        self.store.remember_route(event.instance_id, event.binding["binding_id"], active_wolt=argument, active_session=None)
        self._tell(event, f"🐾 talking to {argument}")
        return self._audit(event, "wolt", "selected", wolt=argument)

    def _created_live(self, event: AcceptedEvent) -> int:
        """How many sessions this binding created are live now (checked outside the lock)."""
        sessions = self._state(event).get("sessions") or {}
        return sum(
            1 for name, record in sessions.items()
            if record.get("via") == "created" and self.live(name) in LIVE_STATES
        )

    def _new(self, event: AcceptedEvent, argument: str | None, previous: dict | None) -> dict:
        binding = event.binding
        binding_id = binding["binding_id"]
        key = event.dedup_key
        state = previous["state"] if previous else "reserved"
        wolt = (previous or {}).get("wolt") or argument or self._state(event).get("active_wolt") or binding["default_wolt"]
        if previous and previous["state"] != "reserved" or (previous and previous.get("wolt")):
            # A resumption: the grant is checked again, like a first execution.
            if not binding.get("may_create") or wolt not in (binding.get("selectable_wolts") or []):
                self.store.settle_operation(event.instance_id, key, "uncertain", note="grant withdrawn during the operation")
                self._tell(event, f"🪵 this chat may no longer start sessions; the owner has to look at {wolt}")
                return self._done(event, "new", "uncertain", wolt=wolt, reason="grant_withdrawn")
        if state == "reserved" and not (previous or {}).get("wolt"):
            # First time here: reserve against the current grant and both limits, atomically.
            reserved = self.store.reserve_create(event.instance_id, key, binding_id, wolt, self._created_live(event))
            if reserved.get("refused"):
                reason = reserved["refused"]
                text = {
                    "no_grant": "🪵 this chat may not start sessions; ask the owner",
                    "not_selectable": f"🪵 {wolt} is not a wolt this chat may start",
                    "rate_limited": f"🪵 this chat already started {binding.get('create_per_hour', 0)} session(s) this hour",
                    "concurrency_limited": "🪵 this chat already has as many live sessions as it may start",
                }[reason]
                self._tell(event, text)
                try:
                    self.store.settle_operation(event.instance_id, key, "failed")
                except ConnectorError:
                    pass
                return self._done(event, "new", "refused", reason=reason, wolt=wolt)
            previous = reserved
            state = "reserved"
            fresh = True
        else:
            fresh = False
        session = (previous or {}).get("session")
        if state == "effect_in_progress":
            # The start may or may not have happened: never run it again. Ask the
            # session boundary by the nonce; otherwise park it for the owner.
            session = self.find(key)
            if session is None:
                self.store.settle_operation(event.instance_id, key, "uncertain")
                self._tell(event, f"🪵 a session for {wolt} may or may not have started; the owner has to look")
                return self._done(event, "new", "uncertain", wolt=wolt)
            self.store.settle_operation(event.instance_id, key, "started", session=session)
            state = "started"
        if state == "reserved":
            routing = {"adapter": event.channel, "chat_id": event.conversation["provider_id"], "binding_id": binding_id, "operation": key}
            # Written before the call: a crash from here on is reconciled or parked, never rerun.
            self.store.settle_operation(event.instance_id, key, "effect_in_progress")
            try:
                result = self.start(wolt, "", routing)
            except Exception as exc:  # noqa: BLE001 - the session layer's reason, shortened, is the answer
                self.store.settle_operation(event.instance_id, key, "failed")
                self._tell(event, f"🪵 could not start a session for {wolt}: {str(exc)[:200]}")
                return self._done(event, "new", "failed", wolt=wolt)
            session = str(result.get("name", ""))
            self.store.settle_operation(event.instance_id, key, "started", session=session)
            state = "started"
        if state == "started":
            self.store.settle_operation(event.instance_id, key, "associated")
            state = "associated"
        self._tell(event, f"🐾 new session for {wolt}: {session}")
        return self._done(event, "new", "created", wolt=wolt, session=session)

    def _sessions(self, event: AcceptedEvent, argument: str | None, previous: dict | None) -> dict:
        sessions = self._state(event).get("sessions") or {}
        if not sessions:
            self._tell(event, "no sessions this chat started or talked to")
            return self._audit(event, "sessions", "answered", count=0)
        lines = []
        for name, record in sorted(sessions.items()):
            lines.append(f"{name}  {self.live(name) or 'gone'}  ({record.get('via')})")
        self._tell(event, "\n".join(lines))
        return self._audit(event, "sessions", "answered", count=len(sessions))

    def _kill(self, event: AcceptedEvent, argument: str | None, previous: dict | None) -> dict:
        binding = event.binding
        key = event.dedup_key
        if not binding.get("may_stop_own_sessions"):
            self._tell(event, "🪵 this chat may not stop sessions; ask the owner")
            return self._done(event, "kill", "refused", reason="no_grant")
        sessions = self._state(event).get("sessions") or {}
        if argument is None:
            live = [name for name in sorted(sessions) if self.live(name) in LIVE_STATES]
            if not live:
                self._tell(event, "nothing of this chat's is running")
                return self._done(event, "kill", "answered", count=0)
            buttons = [[{"text": f"stop {name}", "action": {"command": "kill", "argument": name}}] for name in live]
            self._tell(event, "stop which?", buttons)
            return self._done(event, "kill", "picker")
        if argument not in sessions:
            # Not this binding's: refused the same way whether it exists or not.
            self._tell(event, f"🪵 {argument} is not a session this chat started or talked to")
            return self._done(event, "kill", "refused", reason="not_associated", session=argument)
        state = (previous or {}).get("state", "reserved")
        if state == "started" or self.live(argument) not in LIVE_STATES:
            # Already stopped (by this operation's earlier attempt, or otherwise): no second stop.
            self._tell(event, f"🌙 {argument} is stopped")
            return self._done(event, "kill", "stopped", session=argument)
        if state == "effect_in_progress":
            # The stop was sent and the session still runs: not sent again on our own.
            self.store.settle_operation(event.instance_id, key, "uncertain")
            self._tell(event, f"🪵 {argument} was asked to stop and still runs; the owner has to look")
            return self._done(event, "kill", "uncertain", session=argument)
        self.store.settle_operation(event.instance_id, key, "effect_in_progress", session=argument)
        try:
            self.stop(argument)
        except Exception as exc:  # noqa: BLE001
            self.store.settle_operation(event.instance_id, key, "failed")
            self._tell(event, f"🪵 could not stop {argument}: {str(exc)[:200]}")
            return self._done(event, "kill", "failed", session=argument)
        self.store.settle_operation(event.instance_id, key, "started", session=argument)
        self._tell(event, f"🌙 stopped {argument}")
        return self._done(event, "kill", "stopped", session=argument)
