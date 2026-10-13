"""The connector token handoff: minted on a real relaunch, delivered by file, never by argv.

The supervisor calls `prepare()` right before it spawns the runner and
nowhere else, so a token is rotated exactly when a process is launched: the
previous generation is revoked by the mint. The mint is one locked store
operation that hands back the token with its generation, issue time and
the lodge id; nothing is re-read afterwards, and from the moment it
returns the generation is tracked for cleanup. The generation is minted
*pending*: it authenticates nothing until the runner activates it, which is
how consumption is acknowledged to the lodge.

The file lives under an anchored runtime root: the lodge's platform state
directory, canonicalized once (a platform alias such as /var -> /private/var
is fine, that is the root's own name) and from then on traversed only with
directory-relative, no-follow operations: `run` is made and opened relative
to the root and refused if it is a link, checked to be ours and 0700, and
the file is created `O_EXCL|O_NOFOLLOW` relative to it. The child learns
the root, the file's path and, independently, the lodge id, instance id and
generation it must find inside (`WOLTSPACE_CONNECTOR_*`), never the token.

It fails closed, per generation. Everything after the mint runs under a
cleanup that revokes that generation. `abandon()` revokes first and
unconditionally, then removes the file; a failure of either is kept as a
debt (`unrevoked`, `uncleaned`), retried on every later call from any
state, and shown in `status()`. `check()` on every tick: a file still there
after `CONSUME_TIMEOUT` is removed and its generation revoked; a file gone
but whose generation was not activated within the window is a failure too,
never a success. A revocation that fails is also written down as a debt in
`run/debts.json` (0600, anchored like everything else): exact
(instance, generation) records, so that `recover()` at the next supervisor
start revokes precisely those and nothing newer, a connector that
legitimately survived included. Pending generations never activated within
the window are revoked at recovery too.
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
RUN_DIR = "run"
DEBTS_FILE = "debts.json"


def canonical_root(root: Path) -> str:
    """The runtime root's own name: resolved once, before anything is anchored to it."""
    return os.path.realpath(root)


def open_anchored_run_dir(root: str, *, create: bool) -> tuple[int, int]:
    """(root_fd, run_fd): the root as given, `run` reached below it without following links.

    The root must be a directory we own. `run` is made relative to it when
    asked (an existing link in its place makes the mkdir say it exists and
    the no-follow open refuse it), opened `O_DIRECTORY|O_NOFOLLOW`, and must
    be ours and 0700 (set with `fchmod` on the opened directory, never
    through a path). Raises OSError; the caller closes both descriptors.
    """
    if create:
        # The lodge's own state root; making it is as harmless as it is rare.
        os.makedirs(root, mode=0o700, exist_ok=True)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        info = os.fstat(root_fd)
        if info.st_uid != os.getuid():
            raise PermissionError(f"the runtime root {root} is not ours")
        if create:
            try:
                os.mkdir(RUN_DIR, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        run_fd = os.open(RUN_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            run_info = os.fstat(run_fd)
            if run_info.st_uid != os.getuid():
                raise PermissionError(f"{root}/{RUN_DIR} is not ours")
            if create and stat.S_IMODE(run_info.st_mode) != 0o700:
                os.fchmod(run_fd, 0o700)
            elif stat.S_IMODE(run_info.st_mode) & 0o077:
                raise PermissionError(f"{root}/{RUN_DIR} is not private")
        except BaseException:
            os.close(run_fd)
            raise
        return root_fd, run_fd
    except BaseException:
        os.close(root_fd)
        raise


class ConnectorHandoff:
    def __init__(
        self, store_factory: Callable[[], object], instance_id: str, root: Path, *,
        clock: Callable[[], float] = time.time,
    ):
        self._store_factory = store_factory
        self.instance_id = instance_id
        # Canonicalized here, once; every later step is relative to it.
        self.root = canonical_root(root)
        self._clock = clock
        self.pending: str | None = None        # the file's name inside run/
        self.issued_at: float = 0.0
        self.generation: int | None = None
        # Debts: generations whose revocation failed, files whose removal
        # failed, each with the last error. Retried on every call.
        self.unrevoked: dict[int, str] = {}
        self.uncleaned: dict[str, str] = {}
        self.activated = False

    def _store(self):
        return self._store_factory()

    @property
    def run_dir(self) -> Path:
        return Path(self.root) / RUN_DIR

    # ------------------------------------------------------------- lifecycle

    def prepare(self) -> dict[str, str]:
        """Mint the next generation (pending) and write its handoff; returns the child's extra env."""
        self.abandon()
        self.retry_debts()
        self.sweep_stale()
        minted = self._store().mint(self.instance_id, pending=True)
        # Tracked from here on: whatever fails below, this generation is revoked.
        self.generation = minted["generation"]
        self.activated = False
        try:
            name = f"handoff-{secrets.token_hex(8)}.json"
            root_fd, run_fd = open_anchored_run_dir(self.root, create=True)
            try:
                descriptor = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=run_fd,
                )
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        json.dump({
                            "token": minted["token"], "lodge_id": minted["lodge_id"],
                            "instance_id": self.instance_id, "generation": minted["generation"],
                            "issued": minted["issued"],
                        }, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                except BaseException:
                    try:
                        os.unlink(name, dir_fd=run_fd)
                    except OSError:
                        pass
                    raise
            finally:
                os.close(run_fd)
                os.close(root_fd)
        except BaseException:
            self.abandon()
            raise
        self.pending = name
        self.issued_at = self._clock()
        return {
            "WOLTSPACE_CONNECTOR_TOKEN_FILE": str(self.run_dir / name),
            "WOLTSPACE_HANDOFF_ROOT": self.root,
            "WOLTSPACE_LODGE_ID": minted["lodge_id"],
            "WOLTSPACE_CONNECTOR_INSTANCE": self.instance_id,
            "WOLTSPACE_CONNECTOR_GENERATION": str(minted["generation"]),
            "WOLTSPACE_HANDOFF_WINDOW": str(CONSUME_TIMEOUT),
        }

    def check(self) -> str | None:
        """Whether this generation's handoff is still on course.

        None while the child may still take it, or once it has (the file is
        gone *and* the generation was activated by the child). Otherwise
        the reason it failed, with the generation revoked: a file still
        there after the window, or gone without activation in it. Debts are
        retried on every call, whatever the state.
        """
        self.retry_debts()
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
        taken = self.pending is not None and not self._pending_exists()
        self.abandon()
        if taken:
            return (
                f"the handoff was taken but the connector never acknowledged it within "
                f"{CONSUME_TIMEOUT:g}s; token revoked"
            )
        return f"the connector did not take its handoff within {CONSUME_TIMEOUT:g}s; token revoked"

    def abandon(self) -> None:
        """Revoke the generation in flight, whatever happens to its file; then remove the file.

        Never raises: a revocation or a removal that fails becomes a debt.
        """
        generation, self.generation = self.generation, None
        self.activated = False
        if generation is not None:
            self._revoke(generation)
        self.pending_cleanup()

    def pending_cleanup(self) -> None:
        name, self.pending = self.pending, None
        if name is not None:
            self._remove(name)

    def _remove(self, name: str) -> None:
        try:
            root_fd, run_fd = open_anchored_run_dir(self.root, create=False)
        except OSError as exc:
            self.uncleaned[name] = str(exc)
            return
        try:
            os.unlink(name, dir_fd=run_fd)
            self.uncleaned.pop(name, None)
        except FileNotFoundError:
            self.uncleaned.pop(name, None)
        except OSError as exc:
            self.uncleaned[name] = str(exc)
        finally:
            os.close(run_fd)
            os.close(root_fd)

    def _pending_exists(self) -> bool:
        try:
            info = os.lstat(self.run_dir / self.pending)
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode)

    def sweep_stale(self) -> int:
        """Revoke pending generations and remove handoff files older than STALE_AFTER."""
        cutoff = self._clock() - STALE_AFTER
        try:
            self._store().revoke_stale_pending(cutoff)
            self.unrevoked.pop(-1, None)
        except Exception as exc:  # noqa: BLE001 - the registry may be unreadable; retried later
            self.unrevoked[-1] = f"stale sweep: {exc}"
        removed = 0
        try:
            root_fd, run_fd = open_anchored_run_dir(self.root, create=False)
        except OSError:
            return 0
        try:
            for entry in os.listdir(run_fd):
                if not entry.startswith("handoff-") or not entry.endswith(".json"):
                    continue
                try:
                    info = os.stat(entry, dir_fd=run_fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                        os.unlink(entry, dir_fd=run_fd)
                        removed += 1
                except OSError:
                    continue
        finally:
            os.close(run_fd)
            os.close(root_fd)
        return removed

    def recover(self) -> None:
        """At a supervisor's start: the debts a previous supervisor wrote down are settled.

        Exactly the generations it could not revoke, never a newer one; plus
        every pending generation that was never activated within the window.
        """
        for instance_id, generation in self._read_debts():
            if instance_id == self.instance_id:
                self.unrevoked.setdefault(generation, "owed by a previous supervisor")
        try:
            self._store().revoke_stale_pending(self._clock() - CONSUME_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - retried on the next tick as a debt
            self.unrevoked[-1] = f"stale sweep: {exc}"
        self.retry_debts()

    # ------------------------------------------------------------ debt file

    def _read_debts(self) -> list[tuple[str, int]]:
        try:
            root_fd, run_fd = open_anchored_run_dir(self.root, create=False)
        except OSError:
            return []
        try:
            try:
                descriptor = os.open(DEBTS_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=run_fd)
            except OSError:
                return []
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    return []
                raw = os.read(descriptor, 64 * 1024)
            finally:
                os.close(descriptor)
            data = json.loads(raw)
            return [
                (str(d["instance_id"]), int(d["generation"])) for d in data
                if isinstance(d, dict) and "instance_id" in d and "generation" in d
            ] if isinstance(data, list) else []
        except (OSError, ValueError, TypeError):
            return []
        finally:
            os.close(run_fd)
            os.close(root_fd)

    def _write_debts(self, debts: list[tuple[str, int]]) -> None:
        root_fd, run_fd = open_anchored_run_dir(self.root, create=True)
        try:
            tmp = f".{DEBTS_FILE}.{os.getpid()}.tmp"
            descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=run_fd)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump([{"instance_id": i, "generation": g} for i, g in debts], handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(tmp, DEBTS_FILE, src_dir_fd=run_fd, dst_dir_fd=run_fd)
        finally:
            os.close(run_fd)
            os.close(root_fd)

    def _note_debt(self, generation: int, owed: bool) -> None:
        """Write the debt down (or strike it) so the next supervisor settles it; best effort."""
        try:
            debts = [d for d in self._read_debts() if d != (self.instance_id, generation)]
            if owed:
                debts.append((self.instance_id, generation))
            self._write_debts(debts)
        except OSError:
            pass

    # ------------------------------------------------------------ revocation

    def _revoke(self, generation: int) -> None:
        try:
            self._store().revoke_generation(self.instance_id, generation)
        except Exception as exc:  # noqa: BLE001 - kept, written down, retried, shown
            self.unrevoked[generation] = str(exc)
            self._note_debt(generation, owed=True)
            return
        if self.unrevoked.pop(generation, None) is not None:
            self._note_debt(generation, owed=False)

    def retry_debts(self) -> None:
        """Every revocation or removal that failed is tried again; nothing is forgotten."""
        for generation in list(self.unrevoked):
            if generation == -1:
                try:
                    self._store().revoke_stale_pending(self._clock() - STALE_AFTER)
                    self.unrevoked.pop(generation, None)
                except Exception as exc:  # noqa: BLE001
                    self.unrevoked[generation] = str(exc)
            else:
                self._revoke(generation)
        for name in list(self.uncleaned):
            self._remove(name)

    retry_revocations = retry_debts

    def _token_state(self) -> str | None:
        try:
            return self._store().token_state(self.instance_id, self.generation)
        except Exception:  # noqa: BLE001 - unreadable registry: treated as not activated
            return None

    def status(self) -> dict:
        """What the supervisor shows: the generation in flight and any debt still owed."""
        return {
            "generation": self.generation, "activated": self.activated,
            "pending_file": self.pending is not None,
            "unrevoked": {str(g): e for g, e in self.unrevoked.items()},
            "uncleaned": dict(self.uncleaned),
        }
