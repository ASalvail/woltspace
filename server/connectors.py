"""Core-owned connector state: instances, bindings, credentials, queues.

A connector carries messages between a provider (Telegram, Slack, ...) and the
lodge, and it decides nothing: which senders are allowed, where an event goes,
what it may fetch, and when a delivery has failed are all answered here,
against state the owner controls and only the lodge writes.

Everything lives under `.space/connectors/`:

    registry.json        instances, bindings and credential digests
    queues/<instance>.json   one instance's deliveries, dedup records,
                         upload handles and rate counters
    uploads/<instance>/<upload_id>   bytes a connector sent in
    files/<file_id>      bytes the lodge wants sent out
    inbound.jsonl        accepted events, until the real router takes them
    failures.jsonl       deliveries that ended without a provider message

Every write is temp + fsync + rename under one file lock, so a crash leaves
the previous state rather than half of the next one. A file that does not
parse is `ConnectorStateUnreadable`: nothing is guessed from it and no
credential it would have vouched for is accepted.

Stdlib only; the connector API and the CLI both import this.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import outbox

TOKEN_PREFIX = "wct_"
_ID_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


@dataclass(frozen=True)
class Limits:
    """Every bound the protocol enforces. Tests shrink them; nothing widens them."""

    lease_seconds: float = 60.0
    max_attempts: int = 5
    retry_window_seconds: float = 600.0
    # Backoff after a failed attempt, indexed by how many have failed so far.
    backoff_seconds: tuple[float, ...] = (2.0, 5.0, 15.0, 30.0, 60.0)
    dedup_retention_seconds: float = 7 * 86400.0
    upload_ttl_seconds: float = 3600.0
    upload_max_bytes: int = 50 * 1024 * 1024
    # Unclaimed plus in-flight upload bytes one instance may hold at once.
    upload_disk_cap_bytes: int = 200 * 1024 * 1024
    file_retention_seconds: float = 86400.0
    text_max_bytes: int = 16 * 1024
    id_max_chars: int = 256
    error_max_chars: int = 1000
    event_max_attachments: int = 10
    lease_max_batch: int = 10
    lease_max_wait_seconds: float = 30.0
    max_in_flight: int = 50
    events_per_minute: int = 120
    uploads_per_hour: int = 60
    upload_bytes_per_hour: int = 500 * 1024 * 1024


class ConnectorStateUnreadable(RuntimeError):
    """A state file does not parse. Nothing is served from a guess."""


class ConnectorError(ValueError):
    """A request the protocol refuses. `reason` is machine-readable."""

    def __init__(self, status: int, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.status = status
        self.reason = reason


class NoConnectorBinding(LookupError):
    """Core wants to send somewhere no enabled connector is bound to."""


@dataclass(frozen=True)
class Principal:
    """What a connector token proves: one instance, one process generation."""

    instance_id: str
    generation: int
    channel: str
    account: str
    enabled: bool


@dataclass(frozen=True)
class AcceptedEvent:
    """What the router seam receives once core has admitted an event."""

    instance_id: str
    channel: str
    account: str
    event_id: str
    binding: dict
    sender: dict
    conversation: dict
    text: str
    # [{"upload_id", "path", "name", "size"}]: the router owns these bytes
    # from here on and must move them before the upload TTL sweeps them.
    attachments: tuple[dict, ...]
    # The session the replied-to delivery came from, when reply_to resolved.
    reply_to_session: str | None


@dataclass
class _Queue:
    deliveries: list = field(default_factory=list)
    dedup: dict = field(default_factory=dict)
    uploads: dict = field(default_factory=dict)
    counters: dict = field(default_factory=lambda: {"events": [], "uploads": []})

    def to_json(self) -> dict:
        return {
            "version": 1, "deliveries": self.deliveries, "dedup": self.dedup,
            "uploads": self.uploads, "counters": self.counters,
        }

    @classmethod
    def from_json(cls, data: dict) -> "_Queue":
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ConnectorStateUnreadable("queue file has an unknown shape")
        queue = cls(
            data.get("deliveries", []), data.get("dedup", {}),
            data.get("uploads", {}), data.get("counters", {"events": [], "uploads": []}),
        )
        if not (isinstance(queue.deliveries, list) and isinstance(queue.dedup, dict)
                and isinstance(queue.uploads, dict) and isinstance(queue.counters, dict)):
            raise ConnectorStateUnreadable("queue file has an unknown shape")
        return queue


_TERMINAL = frozenset({"delivered", "failed", "cancelled"})


def valid_id(value: object, limit: int = 256) -> bool:
    """An owner- or install-chosen id: lower-case, no path or space characters."""
    return (
        isinstance(value, str) and 0 < len(value) <= limit
        and set(value) <= _ID_CHARS and value[0] not in "-_"
    )


def _provider_id(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    return value


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ConnectorStore:
    """The lodge's side of the connector protocol, on disk."""

    def __init__(
        self, root: Path, *, clock: Callable[[], float] = time.time,
        limits: Limits | None = None,
    ):
        self.root = Path(root)
        self.clock = clock
        self.limits = limits or Limits()
        self.router: Callable[[AcceptedEvent], dict] = self.record_inbound
        self._failure_listeners: list[Callable[[str, dict], None]] = [self.record_failure]
        # Upload bytes still arriving, per instance: {instance_id: {upload_id: bytes}}.
        self._in_flight: dict[str, dict[str, int]] = {}

    # ----------------------------------------------------------------- files

    @property
    def registry_path(self) -> Path:
        return self.root / "registry.json"

    def _queue_path(self, instance_id: str) -> Path:
        return self.root / "queues" / f"{instance_id}.json"

    def _upload_dir(self, instance_id: str) -> Path:
        return self.root / "uploads" / instance_id

    def _file_path(self, file_id: str) -> Path:
        return self.root / "files" / file_id

    @contextlib.contextmanager
    def _lock(self):
        """Serialize every read-modify-write across the lodge and the CLI."""
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.root / "lock", "w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_json(self, path: Path, default: dict) -> dict:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default
        except OSError as exc:
            raise ConnectorStateUnreadable(f"{path.name}: {exc.strerror}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise ConnectorStateUnreadable(f"{path.name} does not parse") from None
        if not isinstance(data, dict):
            raise ConnectorStateUnreadable(f"{path.name} has an unknown shape")
        return data

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _append_line(self, name: str, record: dict) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.root / name, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def _registry(self) -> dict:
        data = self._read_json(
            self.registry_path, {"version": 1, "instances": {}, "bindings": [], "tokens": []},
        )
        if data.get("version") != 1 or not (
            isinstance(data.get("instances"), dict) and isinstance(data.get("bindings"), list)
            and isinstance(data.get("tokens"), list)
        ):
            raise ConnectorStateUnreadable("registry.json has an unknown shape")
        return data

    def _queue(self, instance_id: str) -> _Queue:
        data = self._read_json(self._queue_path(instance_id), {})
        return _Queue() if not data else _Queue.from_json(data)

    def _save_queue(self, instance_id: str, queue: _Queue) -> None:
        self._write_json(self._queue_path(instance_id), queue.to_json())

    # ------------------------------------------------------------- instances

    def list_instances(self) -> list[dict]:
        registry = self._registry()
        return [
            {"instance_id": instance_id, **record}
            for instance_id, record in sorted(registry["instances"].items())
        ]

    def add_instance(self, instance_id: str, channel: str, account: str) -> dict:
        """Register an install, disabled: nothing routes to it until `enable`."""
        if not valid_id(instance_id, 64):
            raise ConnectorError(400, "invalid_instance_id")
        if not valid_id(channel, 32):
            raise ConnectorError(400, "invalid_channel")
        if _provider_id(account, self.limits.id_max_chars) is None:
            raise ConnectorError(400, "invalid_account")
        with self._lock():
            registry = self._registry()
            if instance_id in registry["instances"]:
                raise ConnectorError(409, "instance_exists")
            registry["instances"][instance_id] = {
                "channel": channel, "account": account, "enabled": False,
                "generation": 0, "created": self.clock(),
            }
            self._write_json(self.registry_path, registry)
            return {"instance_id": instance_id, **registry["instances"][instance_id]}

    def _instance(self, registry: dict, instance_id: str) -> dict:
        try:
            return registry["instances"][instance_id]
        except KeyError:
            raise ConnectorError(404, "unknown_instance") from None

    def enable(self, instance_id: str) -> None:
        """Resume routing and the retry clocks of everything that waited."""
        with self._lock():
            registry = self._registry()
            instance = self._instance(registry, instance_id)
            for other_id, other in registry["instances"].items():
                if other_id != instance_id and other["enabled"] and other["channel"] == instance["channel"]:
                    raise ConnectorError(
                        409, "channel_taken",
                        f"{other_id} already serves {instance['channel']}; disable it first",
                    )
            instance["enabled"] = True
            self._write_json(self.registry_path, registry)
            queue = self._queue(instance_id)
            now = self.clock()
            for delivery in queue.deliveries:
                if delivery.get("clock_paused"):
                    delivery["clock_started"] = now
                    delivery["clock_paused"] = False
            self._sweep(instance_id, queue, enabled=True)
            self._save_queue(instance_id, queue)

    def disable(self, instance_id: str) -> None:
        """Pause: nothing is leased, the retry window stops counting, files are kept."""
        with self._lock():
            registry = self._registry()
            instance = self._instance(registry, instance_id)
            instance["enabled"] = False
            self._write_json(self.registry_path, registry)
            queue = self._queue(instance_id)
            now = self.clock()
            for delivery in queue.deliveries:
                if delivery["status"] in _TERMINAL:
                    continue
                started = delivery.get("clock_started")
                if started is not None:
                    delivery["clock_used"] = delivery.get("clock_used", 0.0) + max(0.0, now - started)
                    delivery["clock_started"] = None
                    delivery["clock_paused"] = True
            self._save_queue(instance_id, queue)

    def remove_instance(
        self, instance_id: str, *, transfer_to: str | None = None,
        mapping: list[dict] | None = None,
    ) -> dict:
        """Revoke, detach and cancel; or transfer what an explicit mapping covers.

        A mapping entry is {"from": {account, conversation, thread}, "to":
        {account, conversation, thread}} and its "to" must be a binding of
        the replacement. Nothing is matched by looking alike: a delivery with
        no entry is cancelled, and no mapping at all cancels everything.
        """
        with self._lock():
            registry = self._registry()
            self._instance(registry, instance_id)
            queue = self._queue(instance_id)
            self._sweep(instance_id, queue, enabled=False)
            outcome = {"cancelled": 0, "transferred": 0}
            pending = [d for d in queue.deliveries if d["status"] not in _TERMINAL]
            moved: list[dict] = []
            if transfer_to is not None:
                replacement = self._instance(registry, transfer_to)
                if transfer_to == instance_id:
                    raise ConnectorError(400, "transfer_to_self")
                targets = {
                    (b["account"], b["conversation"], b.get("thread"))
                    for b in registry["bindings"] if b["instance_id"] == transfer_to
                }
                routes = {}
                for entry in mapping or ():
                    source, target = entry.get("from") or {}, entry.get("to") or {}
                    key = (source.get("account"), source.get("conversation"), source.get("thread"))
                    dest = (target.get("account"), target.get("conversation"), target.get("thread"))
                    if dest not in targets or dest[0] != replacement["account"]:
                        raise ConnectorError(
                            400, "unmapped_destination",
                            f"{dest[0]}/{dest[1]} is not a binding of {transfer_to}",
                        )
                    routes[key] = dest
                for delivery in pending:
                    key = (delivery["account"], delivery["conversation"], delivery.get("thread"))
                    dest = routes.get(key)
                    if dest is None:
                        continue
                    copy = dict(delivery)
                    copy.update({
                        "account": dest[0], "conversation": dest[1], "thread": dest[2],
                        "status": "queued", "lease": None, "clock_started": None,
                        "clock_paused": False,
                        "transferred_from": instance_id,
                    })
                    moved.append(copy)
                    delivery["status"] = "transferred"
                    outcome["transferred"] += 1
                if moved:
                    target_queue = self._queue(transfer_to)
                    target_queue.deliveries.extend(moved)
                    # Dedup lineage follows an account that stays itself.
                    same_account = {s for (s, _c, _t), (d, _dc, _dt) in routes.items() if s == d}
                    for key, record in queue.dedup.items():
                        if key.split("\x00", 1)[0] in same_account:
                            target_queue.dedup.setdefault(key, record)
                    self._save_queue(transfer_to, target_queue)
            for delivery in pending:
                if delivery["status"] == "transferred":
                    continue
                self._finish(instance_id, delivery, "cancelled", "instance removed")
                outcome["cancelled"] += 1
            for token in registry["tokens"]:
                if token["instance_id"] == instance_id and token.get("revoked") is None:
                    token["revoked"] = self.clock()
            registry["bindings"] = [
                b for b in registry["bindings"] if b["instance_id"] != instance_id
            ]
            del registry["instances"][instance_id]
            self._write_json(self.registry_path, registry)
            self._queue_path(instance_id).unlink(missing_ok=True)
            shutil.rmtree(self._upload_dir(instance_id), ignore_errors=True)
            self._in_flight.pop(instance_id, None)
            return outcome

    # -------------------------------------------------------------- bindings

    def list_bindings(self, instance_id: str | None = None) -> list[dict]:
        registry = self._registry()
        return [
            dict(b) for b in registry["bindings"]
            if instance_id is None or b["instance_id"] == instance_id
        ]

    def bind(
        self, instance_id: str, *, sender: str, conversation: str, default_wolt: str,
        thread: str | None = None, may_wake: bool = False,
    ) -> dict:
        """Allow one provider sender in one conversation; the owner's grant, typed."""
        limit = self.limits.id_max_chars
        if _provider_id(sender, limit) is None:
            raise ConnectorError(400, "invalid_sender")
        if _provider_id(conversation, limit) is None:
            raise ConnectorError(400, "invalid_conversation")
        if thread is not None and _provider_id(thread, limit) is None:
            raise ConnectorError(400, "invalid_thread")
        if not valid_id(default_wolt, 64):
            raise ConnectorError(400, "invalid_wolt")
        with self._lock():
            registry = self._registry()
            instance = self._instance(registry, instance_id)
            binding = {
                "instance_id": instance_id, "account": instance["account"],
                "sender": sender, "conversation": conversation, "thread": thread,
                "default_wolt": default_wolt, "may_wake": bool(may_wake),
            }
            key = ("instance_id", "sender", "conversation", "thread")
            registry["bindings"] = [
                b for b in registry["bindings"]
                if tuple(b.get(k) for k in key) != tuple(binding[k] for k in key)
            ]
            registry["bindings"].append(binding)
            self._write_json(self.registry_path, registry)
            return dict(binding)

    def unbind(self, instance_id: str, *, sender: str, conversation: str, thread: str | None = None) -> bool:
        with self._lock():
            registry = self._registry()
            before = len(registry["bindings"])
            registry["bindings"] = [
                b for b in registry["bindings"]
                if (b["instance_id"], b["sender"], b["conversation"], b.get("thread"))
                != (instance_id, sender, conversation, thread)
            ]
            if len(registry["bindings"]) != before:
                self._write_json(self.registry_path, registry)
            return len(registry["bindings"]) != before

    @staticmethod
    def _binding_for(registry: dict, instance_id: str, sender: str, conversation: str, thread: str | None):
        """The binding an event lands in, or the reason it has none."""
        sender_known = False
        for binding in registry["bindings"]:
            if binding["instance_id"] != instance_id or binding["sender"] != sender:
                continue
            sender_known = True
            if binding["conversation"] != conversation:
                continue
            if binding.get("thread") is not None and binding["thread"] != thread:
                continue
            return binding, None
        return None, "conversation_not_bound" if sender_known else "sender_not_allowed"

    # ------------------------------------------------------------ credentials

    def issue_token(self, instance_id: str) -> str:
        """Mint the next generation's token; older generations are revoked.

        Leases the old generation held are released at once (its acks are
        refused anyway), the queue itself is untouched. Called when a runner
        is actually relaunched, never because the lodge restarted.
        """
        with self._lock():
            registry = self._registry()
            instance = self._instance(registry, instance_id)
            instance["generation"] += 1
            now = self.clock()
            for token in registry["tokens"]:
                if token["instance_id"] == instance_id and token.get("revoked") is None:
                    token["revoked"] = now
            plain = TOKEN_PREFIX + secrets.token_urlsafe(32)
            registry["tokens"].append({
                "digest": _digest(plain), "instance_id": instance_id,
                "generation": instance["generation"], "issued": now, "revoked": None,
            })
            registry["tokens"] = [
                t for t in registry["tokens"]
                if t.get("revoked") is None or now - t["revoked"] < 86400
            ]
            self._write_json(self.registry_path, registry)
            queue = self._queue(instance_id)
            for delivery in queue.deliveries:
                lease = delivery.get("lease")
                if lease and lease["generation"] < instance["generation"]:
                    delivery["lease"] = None
                    delivery["status"] = "queued"
            self._save_queue(instance_id, queue)
            return plain

    def revoke_tokens(self, instance_id: str) -> int:
        with self._lock():
            registry = self._registry()
            self._instance(registry, instance_id)
            now, count = self.clock(), 0
            for token in registry["tokens"]:
                if token["instance_id"] == instance_id and token.get("revoked") is None:
                    token["revoked"] = now
                    count += 1
            if count:
                self._write_json(self.registry_path, registry)
            return count

    def authenticate(self, token: object) -> Principal | None:
        """The principal a bearer token proves, or None for anything else.

        An unreadable registry raises: no token is believed on a guess, and
        the caller must never fall back to the token-less owner path.
        """
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > 128:
            return None
        registry = self._registry()
        digest = _digest(token)
        for record in registry["tokens"]:
            if hmac.compare_digest(record["digest"], digest) and record.get("revoked") is None:
                instance = registry["instances"].get(record["instance_id"])
                if instance is None or instance["generation"] != record["generation"]:
                    return None
                return Principal(
                    record["instance_id"], record["generation"], instance["channel"],
                    instance["account"], bool(instance["enabled"]),
                )
        return None

    # ------------------------------------------------------------- sweeping

    def _finish(self, instance_id: str, delivery: dict, status: str, error: str) -> None:
        delivery.update({
            "status": status, "lease": None, "error": error[: self.limits.error_max_chars],
            "finished": self.clock(),
        })
        self._drop_files(delivery)
        for listener in list(self._failure_listeners):
            try:
                listener(instance_id, dict(delivery))
            except Exception:
                pass

    def _drop_files(self, delivery: dict) -> None:
        for attachment in delivery.get("attachments", ()):
            self._file_path(attachment["file_id"]).unlink(missing_ok=True)

    def _attempt_failed(self, instance_id: str, delivery: dict, error: str, *, backoff: bool) -> None:
        """One more attempt gone; retry (with backoff after a nack), or give up for good."""
        delivery["attempts"] = delivery.get("attempts", 0) + 1
        delivery["lease"] = None
        delivery["last_error"] = error[: self.limits.error_max_chars]
        if delivery["attempts"] >= self.limits.max_attempts:
            self._finish(instance_id, delivery, "failed", f"{delivery['attempts']} attempts: {error}")
            return
        step = min(delivery["attempts"], len(self.limits.backoff_seconds)) - 1
        delivery["status"] = "queued"
        delivery["not_before"] = self.clock() + (self.limits.backoff_seconds[step] if backoff else 0)

    def _window_used(self, delivery: dict) -> float:
        used = delivery.get("clock_used", 0.0)
        started = delivery.get("clock_started")
        if started is not None:
            used += max(0.0, self.clock() - started)
        return used

    def _sweep(self, instance_id: str, queue: _Queue, *, enabled: bool) -> None:
        """Apply the clocks: leases, retry windows, file and upload retention."""
        now = self.clock()
        limits = self.limits
        for delivery in queue.deliveries:
            if delivery["status"] in _TERMINAL or delivery["status"] == "transferred":
                continue
            lease = delivery.get("lease")
            if lease and lease["expires"] <= now:
                self._attempt_failed(instance_id, delivery, "lease expired without an ack", backoff=False)
                if delivery["status"] in _TERMINAL:
                    continue
            for attachment in delivery.get("attachments", ()):
                if attachment["expires"] <= now:
                    # Disabled or not: a delivery never waits without its file.
                    self._finish(instance_id, delivery, "failed", "attachment retention expired")
                    break
            if delivery["status"] in _TERMINAL:
                continue
            if enabled and self._window_used(delivery) >= limits.retry_window_seconds and not delivery.get("lease"):
                self._finish(
                    instance_id, delivery, "failed",
                    f"{delivery.get('attempts', 0)} attempts in the retry window",
                )
        queue.deliveries = [
            d for d in queue.deliveries
            if not (d["status"] in _TERMINAL and now - d.get("finished", now) > limits.dedup_retention_seconds)
            and d["status"] != "transferred"
        ]
        for upload_id, record in list(queue.uploads.items()):
            if record["expires"] <= now:
                (self._upload_dir(instance_id) / upload_id).unlink(missing_ok=True)
                del queue.uploads[upload_id]
        queue.dedup = {
            key: record for key, record in queue.dedup.items()
            if now - record["at"] < limits.dedup_retention_seconds
        }
        queue.counters["events"] = [t for t in queue.counters.get("events", []) if now - t < 60]
        queue.counters["uploads"] = [
            entry for entry in queue.counters.get("uploads", []) if now - entry[0] < 3600
        ]

    # --------------------------------------------------------------- inbound

    def _parse_event(self, payload: object) -> dict:
        """Bound and normalize an inbound event, or say what is wrong with it."""
        limits = self.limits
        if not isinstance(payload, dict):
            raise ConnectorError(400, "malformed_event", "event must be an object")
        event_id = _provider_id(payload.get("event_id"), limits.id_max_chars)
        if event_id is None:
            raise ConnectorError(400, "malformed_event", "event_id is required")
        sender, conversation = payload.get("sender"), payload.get("conversation")
        if not isinstance(sender, dict) or not isinstance(conversation, dict):
            raise ConnectorError(400, "malformed_event", "sender and conversation are required")
        sender_id = _provider_id(sender.get("provider_id"), limits.id_max_chars)
        conversation_id = _provider_id(conversation.get("provider_id"), limits.id_max_chars)
        if sender_id is None or conversation_id is None:
            raise ConnectorError(400, "malformed_event", "provider ids are required")
        thread = conversation.get("thread")
        if thread is not None and _provider_id(thread, limits.id_max_chars) is None:
            raise ConnectorError(400, "malformed_event", "thread is not a provider id")
        text = payload.get("text", "")
        if not isinstance(text, str):
            raise ConnectorError(400, "malformed_event", "text must be a string")
        if len(text.encode("utf-8")) > limits.text_max_bytes:
            raise ConnectorError(413, "text_too_long", f"text is over {limits.text_max_bytes} bytes")
        attachments = payload.get("attachments") or []
        if not isinstance(attachments, list) or len(attachments) > limits.event_max_attachments:
            raise ConnectorError(400, "malformed_event", "too many attachments")
        upload_ids = []
        for entry in attachments:
            upload_id = entry.get("upload_id") if isinstance(entry, dict) else None
            if not isinstance(upload_id, str) or outbox.ENTRY_ID_RE.fullmatch(upload_id) is None:
                raise ConnectorError(400, "malformed_event", "attachment needs an upload_id")
            upload_ids.append(upload_id)
        if len(set(upload_ids)) != len(upload_ids):
            raise ConnectorError(400, "malformed_event", "an upload is named twice")
        reply_to = payload.get("reply_to")
        reply_delivery = None
        if reply_to is not None:
            reply_delivery = reply_to.get("delivery_id") if isinstance(reply_to, dict) else None
            if not isinstance(reply_delivery, str) or outbox.ENTRY_ID_RE.fullmatch(reply_delivery) is None:
                raise ConnectorError(400, "malformed_event", "reply_to needs a delivery_id")
        display = sender.get("display")
        return {
            "event_id": event_id, "sender": sender_id,
            "display": display if isinstance(display, str) else "",
            "conversation": conversation_id, "thread": thread, "text": text,
            "attachments": upload_ids, "reply_to": reply_delivery,
        }

    def accept_event(self, who: Principal, payload: object) -> tuple[int, dict]:
        """Admit one provider event: dedup, binding, reply scope, uploads, route.

        Returns (status, body). Policy drops are 200 with accepted=false so a
        provider never retries them; protocol faults are 4xx.
        """
        event = self._parse_event(payload)
        # The digest leaves out the display name: it is shown, never routed on.
        digest = _canonical({k: v for k, v in event.items() if k != "display"})
        key = f"{who.account}\x00{event['event_id']}"
        with self._lock():
            registry = self._registry()
            queue = self._queue(who.instance_id)
            self._sweep(who.instance_id, queue, enabled=who.enabled)
            now = self.clock()
            record = queue.dedup.get(key)
            if record is not None:
                # Same answer first, before any question about its uploads.
                if record["digest"] != digest:
                    self._release_uploads(who.instance_id, queue, event["attachments"])
                    self._save_queue(who.instance_id, queue)
                    raise ConnectorError(409, "event_conflict", "event_id seen with another payload")
                return record["status"], dict(record["body"])
            if len(queue.counters["events"]) >= self.limits.events_per_minute:
                self._release_uploads(who.instance_id, queue, event["attachments"])
                self._save_queue(who.instance_id, queue)
                raise ConnectorError(429, "events_rate_limited")
            queue.counters["events"].append(now)

            def drop(reason: str) -> tuple[int, dict]:
                body = {"accepted": False, "reason": reason, "event_id": event["event_id"]}
                self._release_uploads(who.instance_id, queue, event["attachments"])
                queue.dedup[key] = {"digest": digest, "status": 200, "body": body, "at": now}
                self._save_queue(who.instance_id, queue)
                return 200, body

            if not who.enabled:
                return drop("instance_disabled")
            binding, reason = self._binding_for(
                registry, who.instance_id, event["sender"], event["conversation"], event["thread"],
            )
            if binding is None:
                return drop(reason)
            reply_session = None
            if event["reply_to"] is not None:
                target = next(
                    (d for d in queue.deliveries if d["delivery_id"] == event["reply_to"]), None,
                )
                if target is None or (target["account"], target["conversation"]) != (
                    who.account, event["conversation"],
                ) or (target.get("thread") is not None and target["thread"] != event["thread"]):
                    return drop("reply_to_out_of_binding")
                reply_session = target.get("session")
            claimed = []
            for upload_id in event["attachments"]:
                handle = queue.uploads.get(upload_id)
                if handle is None or handle["expires"] <= now:
                    return drop("upload_expired")
                if handle.get("claimed_by") is not None:
                    return drop("upload_consumed")
                claimed.append((upload_id, handle))
            for upload_id, handle in claimed:
                handle["claimed_by"] = event["event_id"]
            accepted = AcceptedEvent(
                who.instance_id, who.channel, who.account, event["event_id"], dict(binding),
                {"provider_id": event["sender"], "display": outbox.display_name(event["display"]) if event["display"] else ""},
                {"provider_id": event["conversation"], "thread": event["thread"]},
                event["text"],
                tuple(
                    {
                        "upload_id": upload_id, "path": str(self._upload_dir(who.instance_id) / upload_id),
                        "name": handle["name"], "size": handle["size"],
                    } for upload_id, handle in claimed
                ),
                reply_session,
            )
            # Under the store lock: the router hands off and returns, it does
            # not wait on a session.
            routed = self.router(accepted)
            body = {
                "accepted": True, "event_id": event["event_id"],
                "wolt": binding["default_wolt"], "may_wake": binding["may_wake"],
                "session": reply_session, **(routed or {}),
            }
            queue.dedup[key] = {"digest": digest, "status": 200, "body": body, "at": now}
            self._save_queue(who.instance_id, queue)
            return 200, dict(body)

    def _release_uploads(self, instance_id: str, queue: _Queue, upload_ids: list[str]) -> None:
        """A rejected event gives its handles back at once: bytes and quota both."""
        for upload_id in upload_ids:
            handle = queue.uploads.get(upload_id)
            if handle is not None and handle.get("claimed_by") is None:
                del queue.uploads[upload_id]
                (self._upload_dir(instance_id) / upload_id).unlink(missing_ok=True)

    def record_inbound(self, event: AcceptedEvent) -> dict:
        """The default router: a durable record, until sessions take events."""
        self._append_line("inbound.jsonl", {
            "at": self.clock(), "instance_id": event.instance_id, "channel": event.channel,
            "account": event.account, "event_id": event.event_id,
            "wolt": event.binding["default_wolt"], "may_wake": event.binding["may_wake"],
            "session": event.reply_to_session, "sender": event.sender,
            "conversation": event.conversation, "text": event.text,
            "attachments": list(event.attachments),
        })
        return {"routed": "recorded"}

    # --------------------------------------------------------------- uploads

    def begin_upload(self, who: Principal) -> tuple[str, Path]:
        """Open an upload: rate windows and the disk cap are checked before a byte lands.

        Quota is the instance's, whoever the eventual sender turns out to be:
        an unbound sender's bytes count exactly like a bound one's, so the
        event's sender check (which comes later) can never be used to fill
        the disk for free.
        """
        with self._lock():
            queue = self._queue(who.instance_id)
            self._sweep(who.instance_id, queue, enabled=who.enabled)
            self._save_queue(who.instance_id, queue)
            window = queue.counters["uploads"]
            if len(window) >= self.limits.uploads_per_hour:
                raise ConnectorError(429, "uploads_rate_limited")
            if sum(size for _at, size in window) >= self.limits.upload_bytes_per_hour:
                raise ConnectorError(429, "upload_bytes_rate_limited")
            stored = sum(h["size"] for h in queue.uploads.values() if h.get("claimed_by") is None)
        in_flight = self._in_flight.setdefault(who.instance_id, {})
        if stored + sum(size for size, _stored in in_flight.values()) >= self.limits.upload_disk_cap_bytes:
            raise ConnectorError(429, "upload_quota")
        upload_id = secrets.token_hex(16)
        directory = self._upload_dir(who.instance_id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # [bytes so far, stored unclaimed bytes when this upload began].
        in_flight[upload_id] = [0, stored]
        return upload_id, directory / f".{upload_id}.part"

    def account_upload_bytes(self, who: Principal, upload_id: str, received: int) -> None:
        """Count bytes as they arrive; raises once the instance is over a cap."""
        in_flight = self._in_flight.setdefault(who.instance_id, {})
        entry = in_flight.setdefault(upload_id, [0, 0])
        entry[0] = received
        if received > self.limits.upload_max_bytes:
            raise ConnectorError(413, "upload_too_large")
        held = entry[1] + sum(size for other, (size, _s) in in_flight.items() if other != upload_id)
        if held + received > self.limits.upload_disk_cap_bytes:
            raise ConnectorError(429, "upload_quota")

    def abort_upload(self, who: Principal, upload_id: str, part: Path) -> None:
        self._in_flight.get(who.instance_id, {}).pop(upload_id, None)
        part.unlink(missing_ok=True)

    def commit_upload(self, who: Principal, upload_id: str, part: Path, name: object) -> dict:
        try:
            size = part.stat().st_size
            if size == 0:
                raise ConnectorError(400, "upload_empty")
            with self._lock():
                queue = self._queue(who.instance_id)
                now = self.clock()
                queue.uploads[upload_id] = {
                    "name": outbox.display_name(name), "size": size, "created": now,
                    "expires": now + self.limits.upload_ttl_seconds, "claimed_by": None,
                }
                queue.counters["uploads"].append([now, size])
                os.replace(part, self._upload_dir(who.instance_id) / upload_id)
                self._save_queue(who.instance_id, queue)
        finally:
            self.abort_upload(who, upload_id, part)
        return {"upload_id": upload_id, "size": size, "expires": queue.uploads[upload_id]["expires"]}

    # -------------------------------------------------------------- outbound

    def enqueue(
        self, *, channel: str, account: str, conversation: str, text: str,
        thread: str | None = None, attachment: Path | None = None,
        attachment_name: str = "", attachment_kind: str = "file",
        reply_footer: str = "", session: str | None = None,
    ) -> str:
        """Queue one message for the enabled connector bound to that conversation.

        This is the seam notify will call. The attachment is copied into
        core's own files, so the connector only ever reads a lodge-owned path.
        """
        with self._lock():
            registry = self._registry()
            instance_id = next(
                (i for i, r in registry["instances"].items()
                 if r["channel"] == channel and r["account"] == account),
                None,
            )
            if instance_id is None:
                raise NoConnectorBinding(f"no {channel} connector for account {account}")
            bound = any(
                b["instance_id"] == instance_id and b["conversation"] == conversation
                and (b.get("thread") is None or b["thread"] == thread)
                for b in registry["bindings"]
            )
            if not bound:
                raise NoConnectorBinding(f"{channel} {conversation} is not bound to {instance_id}")
            now = self.clock()
            delivery_id = secrets.token_hex(16)
            attachments = []
            if attachment is not None:
                file_id = secrets.token_hex(16)
                target = self._file_path(file_id)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                tmp = target.with_name(f".{file_id}.tmp")
                try:
                    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as sink, open(attachment, "rb") as source:
                        shutil.copyfileobj(source, sink)
                    os.replace(tmp, target)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
                attachments.append({
                    "file_id": file_id, "name": outbox.display_name(attachment_name or attachment.name),
                    "kind": attachment_kind if attachment_kind in ("file", "voice") else "file",
                    "size": target.stat().st_size,
                    "expires": now + self.limits.file_retention_seconds,
                })
            queue = self._queue(instance_id)
            queue.deliveries.append({
                "delivery_id": delivery_id, "account": account, "conversation": conversation,
                "thread": thread, "text": text, "attachments": attachments,
                "reply_footer": reply_footer, "session": session, "created": now,
                "status": "queued", "attempts": 0, "lease": None, "acks": {},
                "not_before": now, "clock_started": None, "clock_used": 0.0,
                "clock_paused": False,
            })
            self._save_queue(instance_id, queue)
            return delivery_id

    def list_deliveries(self, instance_id: str) -> list[dict]:
        """Owner view of a queue, with the clocks applied first."""
        with self._lock():
            registry = self._registry()
            instance = self._instance(registry, instance_id)
            queue = self._queue(instance_id)
            self._sweep(instance_id, queue, enabled=instance["enabled"])
            self._save_queue(instance_id, queue)
            return [dict(d) for d in queue.deliveries]

    def lease(self, who: Principal, max_items: int) -> dict:
        """Hand out up to `max_items` queued deliveries, each leased for 60 s."""
        if not isinstance(max_items, int) or max_items < 1:
            raise ConnectorError(400, "malformed_lease", "max must be a positive integer")
        max_items = min(max_items, self.limits.lease_max_batch)
        with self._lock():
            queue = self._queue(who.instance_id)
            self._sweep(who.instance_id, queue, enabled=who.enabled)
            now = self.clock()
            leased = []
            if who.enabled:
                in_flight = sum(1 for d in queue.deliveries if d["status"] == "leased")
                if in_flight >= self.limits.max_in_flight:
                    self._save_queue(who.instance_id, queue)
                    raise ConnectorError(429, "deliveries_in_flight")
                for delivery in queue.deliveries:
                    if len(leased) >= max_items or in_flight + len(leased) >= self.limits.max_in_flight:
                        break
                    if delivery["status"] != "queued" or delivery["not_before"] > now:
                        continue
                    attempt_id = secrets.token_hex(16)
                    delivery["lease"] = {
                        "attempt_id": attempt_id, "generation": who.generation,
                        "expires": now + self.limits.lease_seconds,
                    }
                    delivery["status"] = "leased"
                    if delivery.get("clock_started") is None and not delivery.get("clock_paused"):
                        delivery["clock_started"] = now
                    leased.append({
                        "delivery_id": delivery["delivery_id"], "attempt_id": attempt_id,
                        "lease_expires": delivery["lease"]["expires"],
                        "conversation": {"provider_id": delivery["conversation"], "thread": delivery.get("thread")},
                        "text": delivery["text"],
                        "attachments": [
                            {k: a[k] for k in ("file_id", "name", "kind", "size")}
                            for a in delivery["attachments"]
                        ],
                        "reply_footer": delivery.get("reply_footer", ""),
                    })
            self._save_queue(who.instance_id, queue)
            return {"deliveries": leased, "paused": not who.enabled}

    def ack(self, who: Principal, delivery_id: object, body: object) -> dict:
        """Close an attempt. A repeat for the same attempt returns the first answer."""
        if not isinstance(body, dict):
            raise ConnectorError(400, "malformed_ack")
        attempt_id = body.get("attempt_id")
        if not isinstance(attempt_id, str) or outbox.ENTRY_ID_RE.fullmatch(attempt_id) is None:
            raise ConnectorError(400, "malformed_ack", "attempt_id is required")
        if not isinstance(body.get("ok"), bool):
            raise ConnectorError(400, "malformed_ack", "ok must be true or false")
        provider_message_id = body.get("provider_message_id")
        if provider_message_id is not None and _provider_id(provider_message_id, self.limits.id_max_chars) is None:
            raise ConnectorError(400, "malformed_ack", "provider_message_id is not a provider id")
        error = body.get("error", "")
        if not isinstance(error, str):
            raise ConnectorError(400, "malformed_ack", "error must be a string")
        retry = body.get("retry", True)
        if not isinstance(retry, bool):
            raise ConnectorError(400, "malformed_ack", "retry must be true or false")
        with self._lock():
            queue = self._queue(who.instance_id)
            delivery = next(
                (d for d in queue.deliveries if d["delivery_id"] == delivery_id), None,
            )
            if delivery is not None and attempt_id in delivery.get("acks", {}):
                return dict(delivery["acks"][attempt_id])
            self._sweep(who.instance_id, queue, enabled=who.enabled)
            lease = delivery.get("lease") if delivery is not None else None
            if (
                delivery is None or lease is None or lease["attempt_id"] != attempt_id
                or lease["generation"] != who.generation
            ):
                # Foreign, stale or expired: one answer for all three, so a
                # connector learns nothing about queues that are not its own.
                self._save_queue(who.instance_id, queue)
                raise ConnectorError(409, "stale_attempt", "no active lease for that attempt")
            if body["ok"]:
                delivery.update({
                    "status": "delivered", "lease": None, "finished": self.clock(),
                    "provider_message_id": provider_message_id,
                })
                self._drop_files(delivery)
            elif retry:
                self._attempt_failed(who.instance_id, delivery, error or "connector reported a failure", backoff=True)
            else:
                self._finish(who.instance_id, delivery, "failed", error or "connector refused for good")
            result = {"ok": True, "status": delivery["status"], "attempts": delivery["attempts"]}
            if delivery["status"] == "queued":
                result["retry_at"] = delivery["not_before"]
            delivery.setdefault("acks", {})[attempt_id] = result
            self._save_queue(who.instance_id, queue)
            return dict(result)

    def open_file(self, who: Principal, file_id: object, attempt_id: object) -> tuple[Path, dict]:
        """The path of an attachment this attempt may read, and its metadata.

        Current generation, active lease for exactly that attempt, enabled
        instance: anything less is refused the same way, and the file itself
        is never named by anything the connector chose.
        """
        if (
            not isinstance(file_id, str) or outbox.ENTRY_ID_RE.fullmatch(file_id) is None
            or not isinstance(attempt_id, str) or outbox.ENTRY_ID_RE.fullmatch(attempt_id) is None
        ):
            raise ConnectorError(404, "file_not_available")
        with self._lock():
            queue = self._queue(who.instance_id)
            self._sweep(who.instance_id, queue, enabled=who.enabled)
            self._save_queue(who.instance_id, queue)
            for delivery in queue.deliveries:
                lease = delivery.get("lease")
                if (
                    delivery["status"] != "leased" or lease is None
                    or lease["attempt_id"] != attempt_id or lease["generation"] != who.generation
                ):
                    continue
                for attachment in delivery["attachments"]:
                    if attachment["file_id"] == file_id and who.enabled:
                        return self._file_path(file_id), dict(attachment)
        raise ConnectorError(404, "file_not_available")

    # -------------------------------------------------------------- failures

    def on_failure(self, listener: Callable[[str, dict], None]) -> None:
        """Hear about every delivery that ends without a provider message.

        The seam that will tell the session; the default writes a record.
        Listeners must never enqueue through this store from inside the call.
        """
        self._failure_listeners.append(listener)

    def record_failure(self, instance_id: str, delivery: dict) -> None:
        self._append_line("failures.jsonl", {
            "at": self.clock(), "instance_id": instance_id,
            "delivery_id": delivery["delivery_id"], "status": delivery["status"],
            "attempts": delivery.get("attempts", 0), "error": delivery.get("error", ""),
            "session": delivery.get("session"), "conversation": delivery["conversation"],
            "delivered": False,
        })
