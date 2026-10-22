#!/usr/bin/env python3
"""Bounded real Codex startup → local mocked notify inspection. Review before launch."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import whatsapp_model_session_test as model

PARTICIPANT = "user:4242424242"
ACCOUNT = "synthetic-startup-account"
BODY = "startup-inspection-ok"
PROMPT = (
    "This is an isolated startup inspection. Use the notify CLI exactly once "
    "to send the text startup-inspection-ok via --whatsapp user:4242424242. "
    "Pass the text through standard input. The endpoint is a local mock and "
    "does not contact WhatsApp. Do nothing else."
)
MAX_SECONDS = 180


def _status_line(diagnostics):
    record = diagnostics.as_record()
    if not model._valid_diagnostic_record(record):
        return "Startup inspection status=invalid_diagnostics."
    return (
        f"Startup inspection process={record['harness_process']} "
        f"readiness={record['harness_readiness']} symptom={record['boot_symptom']} "
        f"notify_attempt={record['notify_attempt']} cleanup={record['checks']['cleanup']}."
    )


def _start_session():
    from sessions import start_session
    # Supported platform creation, including registry, bootstrap and runtime.
    # Prompt is explicit even though the fresh private grant store defaults to it.
    return start_session(
        wolt=model.TEST_WOLT, harness="codex", prompt=PROMPT,
        execution_policy="prompt",
        routing={"adapter": "whatsapp", "chat_id": PARTICIPANT,
                 "whatsapp_account": ACCOUNT},
    )


def _verify_record(session: str, wolts_dir: Path, env: dict,
                   tmux_root: Path) -> bool:
    record = model._record_for_test(session, wolts_dir)
    if not record:
        return False
    if not (
        record.get("adapter") == "whatsapp"
        and record.get("chat_id") == PARTICIPANT
        and record.get("whatsapp_account") == ACCOUNT
        and record.get("harness") == "codex"
        and record.get("wolt") == model.TEST_WOLT
        and isinstance(record.get("runtime"), dict) and record["runtime"]
        and (record.get("execution_policy") or {}).get("mode") == "prompt"
    ):
        return False
    model._verify_private_tmux(env, tmux_root, session)
    return True


def _observe_boot(session: str, env: dict, diagnostics) -> bool:
    process = model._harness_process_status(session, "codex")
    diagnostics.set_harness_process(
        "present" if process is True else "absent" if process is False else "unknown",
    )
    diagnostics.enter("harness_boot")
    if model._handle_boot_symptom(
        diagnostics, model._capture_boot_symptom(env, session, visible_only=True),
    ):
        return False
    readiness = model._harness_readiness_status(session, "codex")
    diagnostics.set_harness_readiness(readiness)
    if readiness == "ready_marker_seen":
        diagnostics.passed("harness_boot")
    return True


def _directory_trust_environment(env: dict, run_root: Path, mode: str) -> None:
    """Allow normal trust writes only to the fresh private Codex config."""
    if mode not in {"preserve", "normal"}:
        raise ValueError("unsupported directory trust inspection mode")
    home = Path(env.get("CODEX_HOME", ""))
    expected = run_root / "codex-home"
    if (home.is_symlink() or home.resolve() != expected.resolve()
            or not home.is_dir() or home.stat().st_uid != os.getuid()
            or (home / "config.toml").exists() or (home / "config.toml").is_symlink()):
        raise RuntimeError("startup inspection requires a fresh private Codex config")
    if mode == "preserve":
        env["WOLTSPACE_PRESERVE_TRUST_PROMPTS"] = "1"
    else:
        env.pop("WOLTSPACE_PRESERVE_TRUST_PROMPTS", None)


def _run_startup_session(
    harness, inherited, auth_variable, auth_path, run_root, tmux_root, diagnostics,
    *, timeout_seconds=120, directory_trust="preserve",
):
    """Real local server/session machinery; every outbound sender is mocked."""
    if harness != "codex" or not 1 <= timeout_seconds <= MAX_SECONDS:
        raise ValueError("unsupported startup inspection settings")
    wolts_dir = run_root / "wolts"
    listener, port = model._bind_local_socket()
    env = model._private_environment(
        run_root, wolts_dir, harness, auth_variable, auth_path,
        "127.0.0.1", port, inherited, tmux_root,
    )
    try:
        _directory_trust_environment(env, run_root, directory_trust)
    except BaseException:
        listener.close()
        raise
    old_env, old_path = os.environ.copy(), list(sys.path)
    old_env.pop("WHATSAPP_AGENT_TOKEN", None)
    session = ""
    server = thread = None
    notify_module = None
    original_send = original_dotenv = None
    server_errors = []
    dispatch_done = threading.Event()
    gate = None
    fd = os.open(run_root / "runner.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    log = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
    os.environ.clear()
    os.environ.update(env)
    sys.path[:0] = [str(p) for p in model._PATHS]
    try:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            import uvicorn
            import server.app as server_app
            import server.notify as notify_module

            original_send, original_dotenv = notify_module._send, notify_module.dotenv_env

            def verify(candidate, participant, routing):
                if (candidate != session or participant != PARTICIPANT
                        or not isinstance(routing, dict)
                        or routing.get("adapter") != "whatsapp"
                        or routing.get("chat_id") != PARTICIPANT
                        or routing.get("whatsapp_account") != ACCOUNT
                        or routing.get("wolt") != model.TEST_WOLT):
                    return ""
                return session if _verify_record(session, wolts_dir, env, tmux_root) else ""

            gate = model.OneNonceSendGate(
                model._expected_notify_payload(BODY), verify, dispatch_done,
                wait_seconds=min(timeout_seconds, model.DISPATCH_WAIT_SECONDS),
                diagnostics=diagnostics,
            )

            async def mocked_sender():
                # No provider call or credential lookup exists on this path.
                return {"adapter": "whatsapp", "mocked": True}

            async def local_send(
                adapter_name, candidate, message, chat_id, thread_ts=None,
                routing=None, attachments=(), speech=None, session_record=None,
            ):
                if adapter_name != "whatsapp" or attachments or speech:
                    gate._set_status("rejected")
                    gate.failure = "startup inspection refused unsupported notification"
                    raise RuntimeError("startup inspection refused unsupported notification")
                return await gate.send(candidate, str(chat_id), message, routing, mocked_sender)

            # Replace the entire outbound boundary, not just WhatsApp transport:
            # a malformed route cannot fall back to a live Telegram/Slack sender.
            notify_module._send = local_send
            notify_module.dotenv_env = lambda _name: ""
            server = uvicorn.Server(uvicorn.Config(
                server_app.app, host="127.0.0.1", port=port, log_level="error",
                access_log=False, log_config=None, lifespan="off",
            ))

            def serve():
                try:
                    asyncio.run(server.serve(sockets=[listener]))
                except BaseException:
                    server_errors.append("server_start_failed")

            thread = threading.Thread(target=serve, name="startup-inspection-api", daemon=True)
            thread.start()
            diagnostics.enter("server_health")
            model._wait_for_server(env["WOLTSPACE_API"], wolts_dir, thread, server_errors)
            diagnostics.passed("server_health")
            diagnostics.enter("session_spawn")
            deadline = time.monotonic() + timeout_seconds
            try:
                result = _start_session()
                session = result.get("name", "") if isinstance(result, dict) else ""
                if not session or not _verify_record(session, wolts_dir, env, tmux_root):
                    diagnostics.fail("session_not_started")
                    raise RuntimeError("startup session could not be verified")
                diagnostics.passed("session_spawn")
            finally:
                dispatch_done.set()
            while time.monotonic() < deadline:
                if server_errors:
                    diagnostics.enter("server_health")
                    diagnostics.fail("server_start_failed")
                    raise RuntimeError("local startup server stopped")
                if gate.failure:
                    diagnostics.enter("notify")
                    diagnostics.fail("notify_ambiguous" if gate.status == "ambiguous"
                                     else "notify_rejected")
                    raise RuntimeError("local mocked notification failed")
                if model._confirmed_notify(gate, session, diagnostics):
                    model._say("Startup inspection received one verified local mocked notification.")
                    return 0
                if not _observe_boot(session, env, diagnostics):
                    raise RuntimeError("startup inspection detected an interactive boot prompt")
                time.sleep(0.2)
            diagnostics.enter("notify")
            diagnostics.fail("run_timeout")
            raise TimeoutError("startup inspection timed out")
    except BaseException:
        if not diagnostics.category:
            diagnostics.fail()
        raise
    finally:
        diagnostics.enter("cleanup")
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            cleanup_ok = model._cleanup_resources(server, thread, listener, session, env, tmux_root)
        diagnostics.set_check("cleanup", "passed" if cleanup_ok else "failed")
        if not cleanup_ok:
            diagnostics.fail("cleanup_failed")
        if notify_module is not None and original_send is not None:
            notify_module._send = original_send
            notify_module.dotenv_env = original_dotenv
        os.environ.clear()
        os.environ.update(old_env)
        sys.path[:] = old_path
        log.close()
        model._remove_raw_log(run_root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--harness", choices=("codex",), required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--directory-trust", choices=("preserve", "normal"), default="preserve")
    args = parser.parse_args()
    if not args.run or not 1 <= args.timeout <= MAX_SECONDS:
        parser.error("--run and a timeout between 1 and 180 seconds are required")
    diagnostics = model.RunDiagnostics("codex")
    try:
        model._say(f"Startup inspection directory_trust={args.directory_trust}.")
        result = model._run_model_test(
            "codex", diagnostics,
            startup_runner=lambda *items: _run_startup_session(
                *items, timeout_seconds=args.timeout, directory_trust=args.directory_trust,
            ),
        )
        if result:
            model._say(model._diagnostic_line(diagnostics))
        else:
            model._say("Startup inspection cleanup=passed.")
        model._say(_status_line(diagnostics))
        return result
    except KeyboardInterrupt:
        model._say("Startup inspection interrupted; isolated cleanup was attempted.")
        return 130
    except Exception:
        if not diagnostics.category:
            diagnostics.fail()
        model._say(model._diagnostic_line(diagnostics))
        model._say(_status_line(diagnostics))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
