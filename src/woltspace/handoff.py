"""The connector token handoff: minted on a real relaunch, delivered by file, never by argv.

The supervisor calls `prepare()` right before it spawns the runner and
nowhere else, so a token is rotated exactly when a process is launched: the
previous generation is revoked by the mint. The new generation is minted
*pending*: it authenticates nothing until the runner's first call with it
activates it, which is how consumption is acknowledged to the lodge. The
token goes into a 0600 file in a 0700 directory, created exclusively, next
to the lodge id, the instance id, the generation and the issue time; the
child learns the file's path and, independently, the lodge id, instance id
and generation it must find inside (`WOLTSPACE_CONNECTOR_*`), never the
token.

It fails closed, per generation. Everything after the mint runs under a
cleanup that revokes that generation: a directory that cannot be made or
opened, a file that cannot be written, a child that cannot be spawned, a
stillborn child. `check()` on every tick: a file still there after
`CONSUME_TIMEOUT` is removed and its generation revoked; a file gone but
whose generation was not activated within the window is a failure too,
never a success. `sweep_stale()` revokes pending generations older than
`STALE_AFTER` from the registry (deletion of a file is not revocation) and
removes their files. A revocation that fails is kept and retried on every
later call, and shows in `status()`.
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
# Older than this, a pending generation and its file are debris.
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
        # Generations whose revocation has not been confirmed yet, with the last error.
        self.unrevoked: dict[int, str] = {}
        self.activated = False

    def _store(self):
        return self._store_factory()

    # ------------------------------------------------------------- lifecycle

    def prepare(self) -> dict[str, str]:
        """Mint the next generation (pending) and write its handoff; returns the child's extra env."""
        self.abandon()
        self.sweep_stale()
        self.retry_revocations()
        store = self._store()
        lodge_id = store.lodge_id()
        token = store.issue_token(self.instance_id, pending=True)
        instance = next(i for i in store.list_instances() if i["instance_id"] == self.instance_id)
        generation = instance["generation"]
        self.generation = generation
        self.activated = False
        try:
            self.run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.run_dir, 0o700)
            path = self.run_dir / f"handoff-{secrets.token_hex(8)}.json"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump({
                        "token": token, "lodge_id": lodge_id, "instance_id": self.instance_id,
                        "generation": generation, "issued": self._clock(),
                    }, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        except BaseException:
            # Anything after the mint: that generation must not stay live.
            self._revoke(generation)
            self.generation = None
            raise
        self.pending = path
        self.issued_at = self._clock()
        return {
            "WOLTSPACE_CONNECTOR_TOKEN_FILE": str(path),
            "WOLTSPACE_LODGE_ID": lodge_id,
            "WOLTSPACE_CONNECTOR_INSTANCE": self.instance_id,
            "WOLTSPACE_CONNECTOR_GENERATION": str(generation),
            "WOLTSPACE_HANDOFF_WINDOW": str(CONSUME_TIMEOUT),
        }

    def check(self) -> str | None:
        """Whether this generation's handoff is still on course.

        None while the child may still take it, or once it has (the file is
        gone *and* the generation was activated by the child's first call).
        Otherwise the reason it failed, with the generation revoked: a file
        still there after the window, or gone without activation in it.
        """
        self.retry_revocations()
        if self.generation is None or self.activated:
            return None
        elapsed = self._clock() - self.issued_at
        state = self._token_state()
        if state == "active":
            self.activated = True
            self.pending = None
            return None
        if state == "revoked":
            self.pending_cleanup()
            self.generation = None
            return "the connector's generation was revoked before it started"
        if elapsed < CONSUME_TIMEOUT:
            return None
        taken = self.pending is not None and not self.pending.exists()
        self.abandon()
        if taken:
            return (
                f"the handoff was taken but the connector never acknowledged it within "
                f"{CONSUME_TIMEOUT:g}s; token revoked"
            )
        return f"the connector did not take its handoff within {CONSUME_TIMEOUT:g}s; token revoked"

    def abandon(self) -> None:
        """Remove a pending file, if any, and revoke the generation it carried."""
        self.pending_cleanup()
        if self.generation is not None:
            self._revoke(self.generation)
            self.generation = None
        self.activated = False

    def pending_cleanup(self) -> None:
        if self.pending is not None:
            self.pending.unlink(missing_ok=True)
            self.pending = None

    def sweep_stale(self) -> int:
        """Revoke pending generations and remove handoff files older than STALE_AFTER."""
        cutoff = self._clock() - STALE_AFTER
        try:
            for instance_id, generation in self._store().revoke_stale_pending(cutoff):
                if instance_id == self.instance_id:
                    self.unrevoked.pop(generation, None)
        except Exception as exc:  # noqa: BLE001 - the registry may be unreadable; retried later
            self.unrevoked.setdefault(-1, f"stale sweep: {exc}")
        removed = 0
        try:
            entries = list(self.run_dir.glob("handoff-*.json"))
        except OSError:
            return 0
        for entry in entries:
            try:
                info = os.lstat(entry)
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    # ------------------------------------------------------------ revocation

    def _revoke(self, generation: int) -> None:
        try:
            self._store().revoke_generation(self.instance_id, generation)
            self.unrevoked.pop(generation, None)
        except Exception as exc:  # noqa: BLE001 - kept, retried, shown
            self.unrevoked[generation] = str(exc)

    def retry_revocations(self) -> None:
        """A revocation that failed is tried again; nothing is forgotten."""
        for generation in list(self.unrevoked):
            if generation < 0:
                try:
                    self._store().revoke_stale_pending(self._clock() - STALE_AFTER)
                    self.unrevoked.pop(generation, None)
                except Exception as exc:  # noqa: BLE001
                    self.unrevoked[generation] = str(exc)
                continue
            self._revoke(generation)

    def _token_state(self) -> str | None:
        try:
            return self._store().token_state(self.instance_id, self.generation)
        except Exception:  # noqa: BLE001 - unreadable registry: treated as not activated
            return None

    def status(self) -> dict:
        """What the supervisor shows: the generation in flight and any revocation still owed."""
        return {
            "generation": self.generation, "activated": self.activated,
            "pending_file": self.pending is not None,
            "unrevoked": {str(g): e for g, e in self.unrevoked.items()},
        }
