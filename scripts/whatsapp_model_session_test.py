#!/usr/bin/env python3
"""Owner-operated real WhatsApp → model session → notify test.

This is deliberately separate from whatsapp_owner_test.py. It is guarded by
an interactive confirmation and uses a fresh private data root, loopback-only
uvicorn socket, and private tmux socket. Do not launch before review.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import scripts.whatsapp_owner_test as owner_test

TEST_WOLT = "wa-test"
TIMEOUT_SECONDS = 300
DISPATCH_WAIT_SECONDS = 45
_PATHS = (ROOT, ROOT / "container", ROOT / "container" / "lib", ROOT / "src")


def _say(message: str) -> None:
    """Write only fixed, credential-free progress messages to the terminal."""
    os.write(1, (message + "\n").encode("utf-8", errors="replace"))


def _auth_path(harness: str, inherited: dict[str, str]) -> tuple[str, Path]:
    """Select an existing supported login path without opening its contents."""
    variable, default_dir = {
        "claude": ("CLAUDE_CONFIG_DIR", ".claude"),
        "codex": ("CODEX_HOME", ".codex"),
    }[harness]
    configured = inherited.get(variable, "").strip()
    home = Path(inherited.get("HOME") or Path.home())
    selected = Path(configured).expanduser() if configured else home / default_dir
    if not selected.is_dir():
        raise RuntimeError("the selected harness has no existing authorized login directory")
    return variable, selected.resolve()


def _private_environment(
    run_root: Path, wolts_dir: Path, harness: str, auth_variable: str,
    auth_path: Path, host: str, port: int, inherited: dict[str, str],
) -> dict[str, str]:
    """Build a narrow process env; never copy owner API tokens into it."""
    home = run_root / "home"
    xdg = run_root / "xdg"
    tmux_tmp = run_root / "tmux"
    temp = run_root / "tmp"
    for directory in (home, xdg, xdg / "config", xdg / "data", xdg / "state",
                      xdg / "cache", tmux_tmp, temp):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)

    tmux = shutil.which("tmux", path=inherited.get("PATH"))
    if not tmux:
        raise RuntimeError("tmux is unavailable on PATH")
    env = {
        "PATH": inherited.get("PATH", os.defpath),
        "HOME": str(home),
        "TMPDIR": str(temp),
        "TMUX_TMPDIR": str(tmux_tmp),
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_DATA_HOME": str(xdg / "data"),
        "XDG_STATE_HOME": str(xdg / "state"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "WOLTSPACE_DIR": str(ROOT),
        "WOLTSPACE_WOLTS_DIR": str(wolts_dir),
        "WOLTS_DIR": str(wolts_dir),
        "WOLTSPACE_WOLT_DIR": str(wolts_dir / TEST_WOLT),
        "WOLT_DIR": str(wolts_dir / TEST_WOLT),
        "WOLTSPACE_WOLT_NAME": TEST_WOLT,
        "WOLT_NAME": TEST_WOLT,
        "WOLTSPACE_ISOLATION": "host",
        "WOLTSPACE_HOST": host,
        "WOLTSPACE_PORT": str(port),
        "PORT": str(port),
        "WOLTSPACE_API": f"http://{host}:{port}",
        "WOLTSPACE_PYTHON": sys.executable,
        "WOLTSPACE_TMUX_BIN": tmux,
        "WOLTSPACE_DEFAULT_HARNESS": harness,
        "BOT_ADAPTER": "whatsapp",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in ("LANG", "LC_ALL"):
        if inherited.get(key):
            env[key] = inherited[key]
    env[auth_variable] = str(auth_path)
    # Never pass terminal multiplexing state from the owner's current shell.
    for key in ("TMUX", "TMUX_PANE", "WHATSAPP_AGENT_TOKEN"):
        env.pop(key, None)
    return env


def _create_synthetic_wolt(wolts_dir: Path, harness: str) -> Path:
    wolt = wolts_dir / TEST_WOLT
    memory = wolt / "wolt" / "memory"
    memory.mkdir(mode=0o700, parents=True)
    (wolt / ".state").mkdir(mode=0o700)
    (memory / "identity.md").write_text(
        "# wa-test\n\nI am a temporary WhatsApp integration test wolt.\n",
        encoding="utf-8",
    )
    (memory / "context.md").write_text("# Context\n\nTemporary test only.\n", encoding="utf-8")
    (memory / "learnings.md").write_text("# Learnings\n\nNone.\n", encoding="utf-8")
    (wolt / "wolt" / "wolt.json").write_text(json.dumps({
        "name": TEST_WOLT,
        "type": "otter",
        "harness": harness,
        "description": "Temporary, isolated WhatsApp model-session test.",
    }, indent=2) + "\n", encoding="utf-8")
    (wolts_dir / "woltspace.json").write_text(json.dumps({
        "harness": {"default": harness},
        "channels": {
            "whatsapp": {"enabled": False},
            "telegram": {"enabled": False},
            "slack": {"enabled": False},
        },
    }, indent=2) + "\n", encoding="utf-8")
    for path in (wolt / "wolt" / "wolt.json", memory / "identity.md",
                 memory / "context.md", memory / "learnings.md",
                 wolts_dir / "woltspace.json"):
        os.chmod(path, 0o600)
    return wolt


def _bind_local_socket() -> tuple[socket.socket, int]:
    """Bind now and hand the bound socket directly to uvicorn (no port race)."""
    for _ in range(8):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        if port != 7777:
            listener.listen(128)
            listener.set_inheritable(False)
            return listener, port
        listener.close()
    raise RuntimeError("could not reserve an isolated non-7777 loopback port")


def _wait_for_server(
    base_url: str, expected_wolts: Path,
    thread: threading.Thread, server_errors: list[str],
) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server_errors or not thread.is_alive():
            raise RuntimeError("isolated local server did not start")
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as response:
                health = json.loads(response.read())
            if (health.get("ok") is True
                    and health.get("isolation") == "host"
                    and health.get("pid") == os.getpid()
                    and Path(health.get("wolts_dir", "/")).resolve() == expected_wolts.resolve()):
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("isolated local server did not pass its loopback health check")


def _tmux_value(env: dict[str, str], *args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [env["WOLTSPACE_TMUX_BIN"], *args], env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=5, check=False,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _verify_private_tmux(env: dict[str, str], run_root: Path, session: str) -> None:
    status, socket_path, _ = _tmux_value(env, "display-message", "-p", "#{socket_path}")
    if status or not socket_path:
        raise RuntimeError("could not verify the test tmux socket")
    try:
        Path(socket_path).resolve().relative_to(run_root.resolve())
    except ValueError:
        raise RuntimeError("tmux socket is outside the private test directory") from None
    status, value, _ = _tmux_value(env, "show-environment", "-g", "WHATSAPP_AGENT_TOKEN")
    if status == 0 and value and not _is_unset_or_empty_token(value):
        raise RuntimeError("WhatsApp credential appeared in the private tmux server environment")
    status, value, _ = _tmux_value(env, "show-environment", "-t", session,
                                  "WHATSAPP_AGENT_TOKEN")
    if status == 0 and value and not _is_unset_or_empty_token(value):
        raise RuntimeError("WhatsApp credential appeared in the model session environment")


def _is_unset_or_empty_token(value: str) -> bool:
    return value.startswith("-WHATSAPP_AGENT_TOKEN") or value == "WHATSAPP_AGENT_TOKEN="


def _kill_private_tmux(env: dict[str, str], run_root: Path) -> bool:
    try:
        status, socket_path, error = _tmux_value(
            env, "display-message", "-p", "#{socket_path}",
        )
    except Exception:
        return False
    if status:
        # Tmux's own explicit "no server running" response is the only
        # nonzero result that proves cleanup is already complete.
        return "no server running" in error.lower()
    try:
        Path(socket_path).resolve().relative_to(run_root.resolve())
    except ValueError:
        return False
    try:
        status, _, _ = _tmux_value(env, "kill-server")
    except Exception:
        return False
    return status == 0


class OneNonceSendGate:
    """Allow one precisely formatted notify send after authoritative checks."""

    def __init__(self, expected_payload: str, verify, dispatch_done: threading.Event,
                 wait_seconds: float = DISPATCH_WAIT_SECONDS):
        self.expected_payload = expected_payload
        self.verify = verify
        self.dispatch_done = dispatch_done
        self.wait_seconds = wait_seconds
        self._lock = threading.Lock()
        self.status = "ready"
        self.verified_session = ""
        self.failure = ""

    async def send(self, session: str, participant: str, message: str,
                   routing: dict | None, sender):
        # A model can notify while adapter.handle is still returning from
        # start_session and before it atomically saves adapter.ROUTES. Wait on
        # a thread event without blocking this server's asyncio loop or holding
        # the gate/phase locks. The dispatch thread always sets it in finally.
        dispatch_finished = await asyncio.to_thread(
            self.dispatch_done.wait, self.wait_seconds,
        )
        with self._lock:
            if self.status != "ready":
                raise RuntimeError("the single WhatsApp test send is already reserved")
            # Reserve after bounded dispatch synchronization but before route
            # validation and definitely before any network I/O.
            self.status = "reserved"
            if not dispatch_finished:
                self.status = "rejected"
                self.failure = "the inbound dispatch did not finish in time"
                raise RuntimeError(self.failure)
            if message != self.expected_payload:
                self.status = "rejected"
                self.failure = "the notify payload did not match the exact nonce"
                raise RuntimeError(self.failure)
            try:
                verified = self.verify(session, participant, routing)
            except Exception:
                verified = ""
            if not verified:
                self.status = "rejected"
                self.failure = "the test session route was not verified"
                raise RuntimeError(self.failure)
            self.verified_session = verified
        try:
            result = await sender()
        except BaseException:
            with self._lock:
                self.status = "ambiguous"
                self.failure = "the single WhatsApp send had an ambiguous outcome"
            raise
        with self._lock:
            self.status = "sent"
        return result


def _cleanup_resources(
    server, server_thread, listener, session_name: str, env: dict[str, str],
    run_root: Path, *, stop_session_fn=None, kill_tmux_fn=None,
) -> bool:
    """Attempt every independent cleanup step and report aggregate certainty."""
    ok = True
    if server is not None:
        try:
            server.should_exit = True
        except Exception:
            ok = False
    if server_thread is not None:
        try:
            server_thread.join(timeout=10)
            if server_thread.is_alive():
                ok = False
        except Exception:
            ok = False
    if listener is not None:
        try:
            listener.close()
        except Exception:
            ok = False
    if session_name:
        try:
            if stop_session_fn is None:
                from sessions import stop_session as stop_session_fn
            result = stop_session_fn(session_name)
            if not isinstance(result, dict) or result.get("status") != "stopped":
                ok = False
        except Exception:
            ok = False
    # Do this even if session or API-server cleanup failed; each is independent.
    try:
        if kill_tmux_fn is None:
            kill_tmux_fn = _kill_private_tmux
        if not kill_tmux_fn(env, run_root):
            ok = False
    except Exception:
        ok = False
    return ok


def _finalize_run_root(run_root: Path, cleanup_ok: bool) -> bool:
    """Delete only after verified cleanup; retain a marked root otherwise."""
    if cleanup_ok:
        try:
            shutil.rmtree(run_root)
            if not run_root.exists():
                return True
        except Exception:
            pass
    try:
        if run_root.exists():
            (run_root / ".cleanup-required").touch(mode=0o600, exist_ok=True)
    except Exception:
        pass
    return False


def _record_for_test(session: str, wolts_dir: Path) -> dict[str, Any] | None:
    from sessions import SessionRegistry
    return SessionRegistry(wolts_dir).get(session, wolt=TEST_WOLT, check_alive=False)


def _expected_notify_payload(nonce: str) -> str:
    # container/bin/notify prefixes the creature emoji and display name before
    # posting /notify. The synthetic wolt's creature is otter and name is fixed.
    return f"🦦 {TEST_WOLT}: {nonce}"


def _wrap_notify_send(real_send, gate: OneNonceSendGate):
    async def guarded_send(
        adapter_name: str, session: str, message: str, chat_id: str,
        thread_ts: str | None = None, routing: dict | None = None,
        attachments=(), speech=None, session_record: dict | None = None,
    ):
        if adapter_name != "whatsapp":
            return await real_send(
                adapter_name, session, message, chat_id, thread_ts, routing,
                attachments=attachments, speech=speech, session_record=session_record,
            )
        return await gate.send(
            session, str(chat_id), message, routing,
            lambda: real_send(
                adapter_name, session, message, chat_id, thread_ts, routing,
                attachments=attachments, speech=speech, session_record=session_record,
            ),
        )
    return guarded_send


def _run_model_test(harness: str) -> int:
    owner_test._check_private_dir()
    inherited = {key: value for key, value in os.environ.items()
                 if key != "WHATSAPP_AGENT_TOKEN"}
    auth_variable, auth_path = _auth_path(harness, inherited)
    token = owner_test._read_token()
    run_root = Path(tempfile.mkdtemp(prefix="model-", dir=owner_test.PRIVATE))
    secret = {"value": token}
    del token
    old_env = os.environ.copy()
    old_env.pop("WHATSAPP_AGENT_TOKEN", None)
    old_path = list(sys.path)
    try:
        os.chmod(run_root, 0o700)
        return _run_model_session(
            harness, inherited, auth_variable, auth_path, run_root, secret,
        )
    finally:
        secret["value"] = ""
        os.environ.clear()
        os.environ.update(old_env)
        sys.path[:] = old_path
        if run_root.exists() and not (run_root / ".cleanup-required").exists():
            shutil.rmtree(run_root, ignore_errors=True)


def _run_model_session(
    harness: str, inherited: dict[str, str], auth_variable: str,
    auth_path: Path, run_root: Path, secret: dict[str, str],
) -> int:
    wolts_dir = run_root / "wolts"
    wolts_dir.mkdir(mode=0o700)
    _create_synthetic_wolt(wolts_dir, harness)
    listener, port = _bind_local_socket()
    host = "127.0.0.1"
    env = _private_environment(
        run_root, wolts_dir, harness, auth_variable, auth_path,
        host, port, inherited,
    )
    if "WHATSAPP_AGENT_TOKEN" in env:
        raise RuntimeError("refusing to start with WhatsApp credentials in child environment")

    old_env = os.environ.copy()
    old_env.pop("WHATSAPP_AGENT_TOKEN", None)
    old_path = list(sys.path)
    account = hashlib.sha256(secret["value"].encode()).hexdigest()[:20]
    session_name = ""
    adapter = None
    server = None
    server_thread = None
    server_errors: list[str] = []
    verified_session = ""
    send_gate = None
    run_log = run_root / "runner.log"
    fd = os.open(run_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    log_stream = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    os.environ.clear()
    os.environ.update(env)
    sys.path[:0] = [str(p) for p in _PATHS]
    try:
        with contextlib.redirect_stdout(log_stream), contextlib.redirect_stderr(log_stream):
            import uvicorn
            # Import the real control plane only after its environment points
            # at this private root. Lifespan is off: no tunnel, channel runner,
            # watchers, or lodge lifecycle hooks start in this test server.
            import server.app as server_app
            import server.notify as notify

            notify.dotenv_env = lambda name: secret["value"] if name == "WHATSAPP_AGENT_TOKEN" else ""
            config = uvicorn.Config(
                server_app.app, host=host, port=port, log_level="error",
                access_log=False, log_config=None, lifespan="off",
            )
            server = uvicorn.Server(config)

            def serve() -> None:
                try:
                    asyncio.run(server.serve(sockets=[listener]))
                except Exception as exc:
                    server_errors.append(type(exc).__name__)

            server_thread = threading.Thread(target=serve, name="wa-model-test-api", daemon=True)
            server_thread.start()
            _wait_for_server(
                env["WOLTSPACE_API"], wolts_dir, server_thread, server_errors,
            )

            # Import the real adapter against a placeholder so the actual key
            # never enters os.environ, even transiently.
            os.environ["WHATSAPP_AGENT_TOKEN"] = "model-test-import-placeholder"
            try:
                import bot.whatsapp_adapter as adapter_module
            finally:
                os.environ.pop("WHATSAPP_AGENT_TOKEN", None)
            adapter = adapter_module
            adapter.TOKEN = secret["value"]
            adapter.ACCOUNT = account
            adapter.STATE = run_root / "wolts" / ".space" / "whatsapp"
            adapter.STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
            adapter.CURSOR = adapter.STATE / f"cursor-{account}.json"
            adapter.ROUTES = adapter.STATE / f"routes-{account}.json"
            adapter.LOCK = adapter.STATE / f"poll-{account}.lock"
            activation = int(time.time())
            state = {"offset": 0, "activation": activation, "processed": []}
            adapter.atomic_json(adapter.CURSOR, state)
            routes = adapter.load(adapter.ROUTES)

            nonce = "wa-model-check-" + secrets.token_hex(6)
            challenge = f"Reply with exactly {nonce} and do nothing else."
            phase: dict[str, Any] = {"selected": False, "prompted": False,
                                     "participant": "", "unexpected": False}
            phase_lock = threading.RLock()
            dispatch_done = threading.Event()

            def verify_send_route(session: str, participant: str,
                                  routing: dict | None) -> str:
                nonlocal session_name
                with phase_lock:
                    if (not phase["selected"] or not phase["prompted"]
                            or participant != phase["participant"]):
                        return ""
                if not session or not isinstance(routing, dict):
                    return ""
                if (routing.get("adapter") != "whatsapp"
                        or routing.get("chat_id") != participant
                        or routing.get("whatsapp_account") != account
                        or routing.get("wolt") != TEST_WOLT):
                    return ""
                persisted_routes = adapter.load(adapter.ROUTES)
                route = persisted_routes.get(participant, {})
                if route.get("wolt") != TEST_WOLT or route.get("session") != session:
                    return ""
                record = _record_for_test(session, wolts_dir)
                if (not record or record.get("adapter") != "whatsapp"
                        or record.get("whatsapp_account") != account
                        or record.get("chat_id") != participant
                        or record.get("wolt") != TEST_WOLT):
                    return ""
                _verify_private_tmux(env, run_root, session)
                session_name = session
                return session

            send_gate = OneNonceSendGate(
                _expected_notify_payload(nonce), verify_send_route, dispatch_done,
            )
            notify._send = _wrap_notify_send(notify._send, send_gate)

            def dispatch_test_message(item: dict, current_routes: dict, started_at: int) -> None:
                text = (item.get("text") or {}).get("body", "")
                participant = item.get("from", "")
                if text.strip() == f"wolt {TEST_WOLT}":
                    with phase_lock:
                        if phase["selected"]:
                            phase["unexpected"] = True
                            return
                        phase["participant"] = participant
                    adapter.handle(item, current_routes, started_at)
                    with phase_lock:
                        if current_routes.get(participant, {}).get("wolt") == TEST_WOLT:
                            phase["selected"] = True
                        else:
                            phase["unexpected"] = True
                    return
                with phase_lock:
                    accepted_challenge = (
                        phase["selected"] and not phase["prompted"]
                        and participant == phase["participant"]
                        and text.strip() == challenge
                    )
                    if not accepted_challenge:
                        phase["unexpected"] = True
                        return
                    # Mark before dispatch: a thrown/ambiguous start must not
                    # allow another model session from a duplicate phrase.
                    phase["prompted"] = True
                try:
                    adapter.handle(item, current_routes, started_at)
                finally:
                    # The notify server may already be waiting for route
                    # persistence. Signal only after handle fully returns.
                    dispatch_done.set()

            _say("Isolated server is healthy on a private 127.0.0.1 port.")
            _say("In WhatsApp, send `wolt wa-test` and wait for the selection confirmation.")
            _say(f"Then send exactly: `{challenge}`")
            _say("The test sends no automatic retry after an ambiguous outbound timeout.")
            deadline = time.monotonic() + TIMEOUT_SECONDS
            delivered = False
            while time.monotonic() < deadline:
                state = adapter.poll_once(
                    state, routes, activation, cursor_path=adapter.CURSOR,
                    dispatch=dispatch_test_message,
                )
                routes = adapter.load(adapter.ROUTES)
                if phase["unexpected"]:
                    raise RuntimeError("an unexpected WhatsApp message arrived; it was not dispatched")
                if send_gate.failure:
                    raise RuntimeError("the single notify attempt was rejected or ambiguous")
                # Participant IDs are opaque; discover them only in memory.
                matching = next((route for route in routes.values()
                                 if route.get("wolt") == TEST_WOLT), None)
                if matching and matching.get("session") and matching["session"] != verified_session:
                    session_name = matching["session"]
                    record = _record_for_test(session_name, wolts_dir)
                    if not record or record.get("adapter") != "whatsapp":
                        raise RuntimeError("the test session route was not persisted")
                    if record.get("whatsapp_account") != account:
                        raise RuntimeError("the test session account binding did not match")
                    if record.get("chat_id") != next(
                        participant for participant, route in routes.items()
                        if route is matching
                    ):
                        raise RuntimeError("the test session participant binding did not match")
                    _verify_private_tmux(env, run_root, session_name)
                    verified_session = session_name
                if send_gate.verified_session:
                    # The send gate may verify first if the model notifies
                    # before this polling loop observes the persisted route.
                    verified_session = send_gate.verified_session
                if (send_gate.status == "sent" and send_gate.verified_session
                        and verified_session == send_gate.verified_session
                        and session_name == send_gate.verified_session):
                    delivered = True
                    break
                if server_errors:
                    raise RuntimeError("the isolated local server stopped unexpectedly")
            if not delivered:
                raise TimeoutError("timed out waiting for the model reply")
            _say("Verified one real model session, account-bound registry route, and nonce reply.")
            return 0
    finally:
        cleanup_ok = _cleanup_resources(
            server, server_thread, listener, session_name, env, run_root,
        )
        if adapter is not None:
            adapter.TOKEN = ""
        secret["value"] = ""
        os.environ.clear()
        os.environ.update(old_env)
        sys.path[:] = old_path
        sys.stdout, sys.stderr = original_stdout, original_stderr
        log_stream.close()
        cleanup_ok = _finalize_run_root(run_root, cleanup_ok)
        if not cleanup_ok:
            _say("Cleanup needs attention; the private test directory was retained.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="run the guarded live model-session test")
    parser.add_argument("--harness", choices=("claude", "codex"),
                        help="use this already-authorized harness login path")
    args = parser.parse_args()
    if not args.run or not args.harness:
        parser.error("both --run and --harness are required")
    try:
        owner_test._require_tty()
        if input("This starts one real model session in private test state. Type MODEL to continue: ").strip() != "MODEL":
            print("Cancelled; no key was read and no process was started.")
            return 1
        return _run_model_test(args.harness)
    except KeyboardInterrupt:
        print("Stopped by owner; cleanup was attempted.")
        return 130
    except Exception as exc:
        print(f"Model-session test stopped ({type(exc).__name__}); no credential or message was printed.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
