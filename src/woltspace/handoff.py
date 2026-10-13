"""The connector token handoff: minted on a real relaunch, delivered by file, never by argv.

The supervisor calls `prepare()` right before it spawns the runner and
nowhere else, so a token is rotated exactly when a process is launched: the
previous generation is revoked by the mint. The token goes into a 0600 file
in a 0700 directory, created exclusively; the child learns only the file's
path (`WOLTSPACE_CONNECTOR_TOKEN_FILE`), reads it once and unlinks it.

It fails closed. The supervisor asks `check()` on every tick while the file
is still there: a child that has not consumed it within `CONSUME_TIMEOUT`
seconds is not considered started, the file is removed and the token
revoked. A child that exits, and the supervisor stopping, revoke it too.
The same lodge code is the connector store, so the registry it writes is
the one the API reads.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Callable

CONSUME_TIMEOUT = 10.0
# Older than this at the supervisor's start, a handoff file is debris.
STALE_AFTER = 60.0


class ConnectorHandoff:
    def __init__(
        self, store_factory: Callable[[], object], instance_id: str, run_dir: Path, *,
        clock: Callable[[], float] = time.time,
    ):
        self._store_factory = store_factory
        self.instance_id = instance_id
        self.run_dir = Path(run_dir)
        self._clock = clock
        self.pending: Path | None = None
        self.issued_at: float = 0.0
        self.generation: int | None = None

    def _store(self):
        return self._store_factory()

    def prepare(self) -> dict[str, str]:
        """Mint the next generation and write its handoff; returns the child's extra env."""
        self.abandon()
        self.sweep_stale()
        store = self._store()
        token = store.issue_token(self.instance_id)
        instance = next(i for i in store.list_instances() if i["instance_id"] == self.instance_id)
        self.generation = instance["generation"]
        self.run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.run_dir, 0o700)
        path = self.run_dir / f"handoff-{secrets.token_hex(8)}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({
                    "token": token, "instance_id": self.instance_id,
                    "generation": self.generation, "issued": self._clock(),
                }, handle)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            store.revoke_tokens(self.instance_id)
            raise
        self.pending = path
        self.issued_at = self._clock()
        return {"WOLTSPACE_CONNECTOR_TOKEN_FILE": str(path)}

    def check(self) -> str | None:
        """While the file is still there: None if the child may still take it, else why it failed.

        Consumed means gone (the child unlinks on read). A file that lingers
        past the timeout is removed and its token revoked.
        """
        if self.pending is None:
            return None
        if not self.pending.exists():
            self.pending = None
            return None
        if self._clock() - self.issued_at < CONSUME_TIMEOUT:
            return None
        self.abandon()
        return f"the connector did not take its handoff within {CONSUME_TIMEOUT:g}s; token revoked"

    def abandon(self) -> None:
        """Remove a pending file, if any, and revoke the token it carried."""
        if self.pending is not None:
            self.pending.unlink(missing_ok=True)
            self.pending = None
        if self.generation is not None:
            try:
                self._store().revoke_tokens(self.instance_id)
            except Exception:  # noqa: BLE001 - the registry may be unreadable; nothing else to do
                pass
            self.generation = None

    def sweep_stale(self) -> int:
        """Remove handoff files older than STALE_AFTER left by a previous life."""
        removed = 0
        try:
            entries = list(self.run_dir.glob("handoff-*.json"))
        except OSError:
            return 0
        cutoff = self._clock() - STALE_AFTER
        for entry in entries:
            try:
                info = os.lstat(entry)
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
