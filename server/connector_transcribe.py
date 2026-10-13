"""Voice transcription for connector events: owner opt-in, bounded, cached.

A voice or audio file that came with an accepted event is transcribed by
core, never by the connector, and only when the binding's owner turned on
`may_transcribe`: the audio then goes to OpenAI (`whisper-1`) with the
lodge's `OPENAI_API_KEY`. The transcript is message content for the
session, nothing more: the command parser never sees it.

Bounds are bytes and requests, measured on the stored file, never a
duration: at most `transcribe_max_bytes` per file, and a per-instance
UTC-day budget of bytes and requests, charged when an attempt is claimed
and never refunded (a timed-out call may still have been billed). At most
two calls run lodge-wide at once (flock slots, held until the HTTP client
has closed, cancellation included); policy is re-checked after a slot is
won. Each (event, attachment) has at most `transcribe_max_attempts`
attempts, ever; results are cached and reused by every retry, so a crash
after a call and before its result is saved can call again, within that
cap. Every outcome is saved: the text, or a fixed reason (not_enabled,
no_key, too_large, unsupported_format, unreadable, over_budget,
retries_exhausted, timeout, network, provider_<status>, bad_response,
response_too_large). A skip saved while the grant was off, or with no
key, stays a skip for that attachment even after the owner turns it on.
Provider error bodies are never read or relayed; nothing here logs audio,
transcripts or headers.

The file is opened through the uploads directory with no-follow
traversal and the bytes sent come from that same descriptor, after a
size check against what was accepted. Like everything in the lodge this
runs as the owner's OS user: a same-user process that can rewrite the
lodge's files is outside what this can contain (the trusted-local-process
limit).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import time
from pathlib import Path
from typing import Callable

import httpx

from .connectors import AcceptedEvent, ConnectorStore

API_URL = "https://api.openai.com/v1/audio/transcriptions"
AUDIO_TYPES = {
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".mp3": "audio/mpeg", ".mpga": "audio/mpeg",
    ".m4a": "audio/mp4", ".wav": "audio/wav", ".webm": "audio/webm", ".flac": "audio/flac",
}
TRANSCRIPT_MAX_CHARS = 4000
RESPONSE_MAX_BYTES = 64 * 1024
CALL_DEADLINE_SECONDS = 60.0
SLOTS = 2
SLOT_WAIT_SECONDS = 20.0


class TranscriptionPending(RuntimeError):
    """Another attempt holds this attachment, or no slot came free: defer the handoff."""


def openai_transcribe(data: bytes, filename: str, content_type: str, key: str) -> dict:
    """One call: `{"text"}` or `{"reason"}`. Fixed endpoint, no redirects, bounded everything."""
    deadline = time.monotonic() + CALL_DEADLINE_SECONDS
    body = bytearray()
    try:
        with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(CALL_DEADLINE_SECONDS, connect=10.0)) as client:
            with client.stream(
                "POST", API_URL, headers={"Authorization": f"Bearer {key}"},
                files={"file": (filename, data, content_type)},
                data={"model": "whisper-1", "response_format": "json"},
            ) as response:
                if response.status_code != 200:
                    return {"reason": f"provider_{response.status_code}"}     # body never read
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > RESPONSE_MAX_BYTES:
                        return {"reason": "response_too_large"}
                    if time.monotonic() > deadline:
                        return {"reason": "timeout"}
    except httpx.TimeoutException:
        return {"reason": "timeout"}
    except httpx.HTTPError:
        return {"reason": "network"}
    try:
        text = json.loads(bytes(body))["text"]
    except (ValueError, KeyError, TypeError):
        return {"reason": "bad_response"}
    if not isinstance(text, str):
        return {"reason": "bad_response"}
    return {"text": " ".join(text.split())[:TRANSCRIPT_MAX_CHARS]}


def read_anchored(root: Path, parts: tuple[str, ...], name: str, size: int, cap: int) -> bytes | None:
    """The file's bytes through no-follow traversal from `root`, if it is the regular file of `size`."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except OSError:
        os.close(fd)
        return None
    os.close(fd)
    try:
        info = os.fstat(leaf)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size or size > cap:
            return None
        chunks, total = [], 0
        while total <= size:
            chunk = os.read(leaf, min(1 << 16, size + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        return data if len(data) == size else None
    except OSError:
        return None
    finally:
        os.close(leaf)


@contextlib.contextmanager
def lodge_slot(root: Path, *, slots: int = SLOTS, wait: float = SLOT_WAIT_SECONDS):
    """One of `slots` lodge-wide transcription slots (flock, across processes), or TranscriptionPending."""
    directory = root / "transcribe"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        for index in range(slots):
            fd = os.open(directory / f"slot-{index}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            return
        if time.monotonic() >= deadline:
            raise TranscriptionPending("no transcription slot free")
        time.sleep(0.1)


class Transcriber:
    def __init__(
        self, store: ConnectorStore, uploads_root: Path, *,
        call: Callable[[bytes, str, str, str], dict] = openai_transcribe,
        key: Callable[[], str | None] = lambda: os.environ.get("OPENAI_API_KEY") or None,
        slot_wait: float = SLOT_WAIT_SECONDS,
    ):
        self.store = store
        self.uploads_root = uploads_root
        self.call = call
        self.key = key
        self.slot_wait = slot_wait

    def notes(self, event: AcceptedEvent, files: list[Path]) -> list[str]:
        """One line per audio attachment: its transcript, or why there is none."""
        lines = []
        for attachment, path in zip(event.attachments, files):
            extension = Path(attachment.get("name", "")).suffix.lower()
            if extension not in AUDIO_TYPES:
                continue
            outcome = self._one(event, attachment, Path(path), extension)
            if "text" in outcome:
                lines.append(f"[voice note transcript]: {outcome['text']}")
            else:
                lines.append(f"[voice note: not transcribed ({outcome['reason']})]")
        return lines

    def _one(self, event: AcceptedEvent, attachment: dict, path: Path, extension: str) -> dict:
        key, upload_id, size = event.dedup_key, attachment["upload_id"], attachment.get("size")
        api_key = self.key()
        with lodge_slot(self.store.root, wait=self.slot_wait):
            claimed = self.store.claim_transcription(
                event.instance_id, key, upload_id, size, key_present=api_key is not None,
            )
            if "done" in claimed:
                return claimed["done"]
            if claimed.get("busy"):
                raise TranscriptionPending("another attempt is transcribing this file")
            claim = claimed["claim"]
            data = read_anchored(
                self.uploads_root, (event.channel, "uploads"), path.name, size,
                self.store.limits.transcribe_max_bytes,
            )
            if data is None:
                outcome = {"reason": "unreadable"}
            else:
                name = "voice.ogg" if extension in (".ogg", ".oga") else f"audio{extension}"
                try:
                    outcome = self.call(data, name, AUDIO_TYPES[extension], api_key)
                except Exception:  # noqa: BLE001 - a fixed reason, never the provider's words
                    outcome = {"reason": "network"}
            return self.store.settle_transcription(event.instance_id, key, upload_id, claim, outcome)
