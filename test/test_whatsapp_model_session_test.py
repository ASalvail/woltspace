import importlib.util
import asyncio
import os
import runpy
import stat
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "whatsapp_model_session_test_script",
    ROOT / "scripts" / "whatsapp_model_session_test.py",
)
model_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(model_test)


def _completed_event():
    event = threading.Event()
    event.set()
    return event


def test_auth_path_uses_existing_configured_harness_directory(tmp_path):
    config = tmp_path / "authorized-config"
    config.mkdir(mode=0o700)
    variable, path = model_test._auth_path(
        "claude", {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(config)}
    )
    assert variable == "CLAUDE_CONFIG_DIR"
    assert path == config.resolve()


def test_auth_path_supports_default_existing_codex_directory_without_reading_files(
    tmp_path, monkeypatch,
):
    auth_dir = tmp_path / ".codex"
    auth_dir.mkdir(mode=0o700)
    def no_auth_file_read(_path, *args, **kwargs):
        raise AssertionError("auth file contents must not be read")
    monkeypatch.setattr(Path, "read_text", no_auth_file_read)
    variable, path = model_test._auth_path("codex", {"HOME": str(tmp_path)})
    assert variable == "CODEX_HOME"
    assert path == auth_dir.resolve()


def test_auth_path_fails_when_existing_login_directory_is_unavailable(tmp_path):
    with pytest.raises(RuntimeError, match="no existing authorized login"):
        model_test._auth_path("claude", {"HOME": str(tmp_path)})


def test_private_environment_is_minimal_and_keeps_auth_out_of_home(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir(mode=0o700)
    wolts_dir = run_root / "wolts"
    wolts_dir.mkdir(mode=0o700)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tmux = bindir / "tmux"
    tmux.write_text("#!/bin/sh\nexit 0\n")
    tmux.chmod(0o700)
    auth = tmp_path / "claude-auth"
    auth.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix="wt-", dir="/tmp") as socket_base:
        tmux_tmp = model_test._prepare_private_tmux_dir(run_root, Path(socket_base))
    inherited = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "WHATSAPP_AGENT_TOKEN": "synthetic-whatsapp-secret",
        "ANTHROPIC_API_KEY": "synthetic-provider-secret",
        "TELEGRAM_BOT_TOKEN": "synthetic-chat-secret",
        "TMUX": "/owner/socket,1,0",
        "CLAUDE_CONFIG_DIR": str(auth),
    }
    env = model_test._private_environment(
        run_root, wolts_dir, "claude", "CLAUDE_CONFIG_DIR", auth,
        "127.0.0.1", 41321, inherited, tmux_tmp,
    )
    assert env["CLAUDE_CONFIG_DIR"] == str(auth.resolve())
    assert env["HOME"] == str(run_root / "home")
    assert env["TMUX_TMPDIR"] == str(tmux_tmp)
    assert env["WOLTSPACE_WOLTS_DIR"] == str(wolts_dir)
    assert env["WOLTSPACE_API"] == "http://127.0.0.1:41321"
    assert env["PORT"] != "7777"
    assert "TMUX" not in env and "TMUX_PANE" not in env
    assert "WHATSAPP_AGENT_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert stat.S_IMODE((run_root / "home").stat().st_mode) == 0o700


def test_synthetic_wolt_scaffolding_is_confined_to_private_test_root(tmp_path):
    wolts_dir = tmp_path / "wolts"
    wolts_dir.mkdir(mode=0o700)
    wolt = model_test._create_synthetic_wolt(wolts_dir, "codex")
    assert wolt == wolts_dir / "wa-test"
    manifest = (wolt / "wolt" / "wolt.json").read_text()
    assert '"harness": "codex"' in manifest
    config = (wolts_dir / "woltspace.json").read_text()
    assert '"enabled": false' in config
    assert all(path.resolve().is_relative_to(tmp_path.resolve())
               for path in wolt.rglob("*"))


def test_expected_nonce_matches_the_real_notify_cli_prefix(monkeypatch):
    notify_cli = runpy.run_path(str(ROOT / "container" / "bin" / "notify"))
    monkeypatch.setitem(
        notify_cli["_format_full_message"].__globals__,
        "_creature_emoji", lambda *_: "🦦",
    )
    nonce = "wa-model-check-a1b2c3"
    actual_cli_payload = notify_cli["_format_full_message"](
        "test-session", "wa-test", nonce, ROOT / "container" / "bin" / "session-reg",
    )
    assert actual_cli_payload == model_test._expected_notify_payload(nonce)
    assert actual_cli_payload == f"🦦 wa-test: {nonce}"


def test_notify_gate_allows_only_one_exact_payload_after_route_verification():
    attempts = []
    routing = {
        "adapter": "whatsapp", "chat_id": "user:42",
        "whatsapp_account": "test-account", "wolt": "wa-test",
    }
    def verify(session, participant, supplied_routing):
        assert (session, participant, supplied_routing) == ("s1", "user:42", routing)
        return "s1"

    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: wa-model-check", verify, _completed_event(),
    )
    wrapped = model_test._wrap_notify_send(
        lambda *args, **kwargs: _async_record(attempts, *args, **kwargs), gate,
    )

    async def run():
        await wrapped("whatsapp", "s1", "🦦 wa-test: wa-model-check", "user:42",
                      routing=routing)
        with pytest.raises(RuntimeError, match="already reserved"):
            await wrapped("whatsapp", "s1", "🦦 wa-test: wa-model-check", "user:42",
                          routing=routing)

    asyncio.run(run())
    assert len(attempts) == 1
    assert gate.status == "sent"
    assert gate.verified_session == "s1"


def test_notify_gate_rejects_wrong_payload_or_unverified_session_before_io():
    attempts = []
    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: nonce", lambda *_: "", _completed_event(),
    )
    wrapped = model_test._wrap_notify_send(
        lambda *args, **kwargs: _async_record(attempts, *args, **kwargs), gate,
    )

    async def run():
        with pytest.raises(RuntimeError, match="exact nonce"):
            await wrapped("whatsapp", "s1", "🦦 wa-test: nonce plus", "user:42",
                          routing={})

    asyncio.run(run())
    assert attempts == []
    assert gate.status == "rejected"


def test_notify_gate_requires_authoritative_verification_before_io():
    attempts = []
    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: nonce", lambda *_: "", _completed_event(),
    )
    wrapped = model_test._wrap_notify_send(
        lambda *args, **kwargs: _async_record(attempts, *args, **kwargs), gate,
    )

    async def run():
        with pytest.raises(RuntimeError, match="route was not verified"):
            await wrapped("whatsapp", "s1", "🦦 wa-test: nonce", "user:42",
                          routing={"adapter": "whatsapp"})

    asyncio.run(run())
    assert attempts == []
    assert gate.status == "rejected"


def test_ambiguous_notify_send_is_terminal_and_never_retried():
    attempts = []
    routing = {"adapter": "whatsapp", "chat_id": "user:42",
               "whatsapp_account": "test-account", "wolt": "wa-test"}
    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: nonce", lambda *_: "s1", _completed_event(),
    )

    async def fail_ambiguous(*_args, **_kwargs):
        attempts.append("sent")
        raise TimeoutError("simulated timeout")

    wrapped = model_test._wrap_notify_send(fail_ambiguous, gate)

    async def run():
        with pytest.raises(TimeoutError):
            await wrapped("whatsapp", "s1", "🦦 wa-test: nonce", "user:42",
                          routing=routing)
        with pytest.raises(RuntimeError, match="already reserved"):
            await wrapped("whatsapp", "s1", "🦦 wa-test: nonce", "user:42",
                          routing=routing)

    asyncio.run(run())
    assert attempts == ["sent"]
    assert gate.status == "ambiguous"


@pytest.mark.parametrize("explicit", [
    None,
    {"adapter": "whatsapp", "participant": "user:42"},
])
def test_real_send_notification_passes_registry_routing_to_gate(
    tmp_path, monkeypatch, explicit,
):
    import server.notify as notify

    routing = {
        "adapter": "whatsapp", "chat_id": "user:42",
        "whatsapp_account": "test-account", "wolt": "wa-test",
    }
    expected = "🦦 wa-test: nonce"
    attempts = []

    def verify(session, participant, supplied_routing):
        assert (session, participant) == ("s1", "user:42")
        assert supplied_routing is routing
        return "s1"

    async def fake_send(adapter, session, message, chat_id, thread_ts=None,
                        supplied_routing=None, **kwargs):
        attempts.append((adapter, session, message, chat_id, thread_ts,
                         supplied_routing, kwargs))
        return {"adapter": adapter, "participant": chat_id}

    gate = model_test.OneNonceSendGate(
        expected, verify, _completed_event(),
    )
    monkeypatch.setattr(notify, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(notify, "read_session_registry", lambda _session: routing)
    monkeypatch.setattr(notify, "_send", model_test._wrap_notify_send(fake_send, gate))

    result = asyncio.run(notify.send_notification("s1", expected, explicit))

    assert result == {"adapter": "whatsapp", "participant": "user:42"}
    assert len(attempts) == 1
    assert attempts[0][5] is routing
    assert gate.status == "sent"


def test_real_send_notification_waits_for_dispatch_route_persistence(
    tmp_path, monkeypatch,
):
    import server.notify as notify

    routing = {
        "adapter": "whatsapp", "chat_id": "user:42",
        "whatsapp_account": "test-account", "wolt": "wa-test",
    }
    expected = "🦦 wa-test: nonce"
    dispatch_done = threading.Event()
    route_persisted = {"value": False}
    attempts = []

    def verify(session, participant, supplied_routing):
        if (route_persisted["value"] and session == "s1"
                and participant == "user:42" and supplied_routing is routing):
            return "s1"
        return ""

    async def fake_send(*args, **kwargs):
        attempts.append((args, kwargs))
        return {"adapter": "whatsapp", "participant": "user:42"}

    gate = model_test.OneNonceSendGate(
        expected, verify, dispatch_done, wait_seconds=1,
    )
    monkeypatch.setattr(notify, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(notify, "read_session_registry", lambda _session: routing)
    monkeypatch.setattr(notify, "_send", model_test._wrap_notify_send(fake_send, gate))

    async def run():
        pending = asyncio.create_task(notify.send_notification("s1", expected))
        await asyncio.sleep(0.02)
        assert not pending.done()
        assert gate.status == "ready"
        assert attempts == []
        route_persisted["value"] = True
        dispatch_done.set()
        return await pending

    assert asyncio.run(run())["participant"] == "user:42"
    assert len(attempts) == 1
    assert gate.status == "sent"
    assert gate.verified_session == "s1"


def test_real_send_notification_fails_closed_after_dispatch_without_route(
    tmp_path, monkeypatch,
):
    import server.notify as notify

    routing = {
        "adapter": "whatsapp", "chat_id": "user:42",
        "whatsapp_account": "test-account", "wolt": "wa-test",
    }
    attempts = []
    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: nonce", lambda *_: "", _completed_event(),
    )
    monkeypatch.setattr(notify, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(notify, "read_session_registry", lambda _session: routing)
    monkeypatch.setattr(
        notify, "_send",
        model_test._wrap_notify_send(
            lambda *args, **kwargs: _async_record(attempts, *args, **kwargs), gate,
        ),
    )

    with pytest.raises(RuntimeError, match="route was not verified"):
        asyncio.run(notify.send_notification("s1", "🦦 wa-test: nonce"))
    assert attempts == []
    assert gate.status == "rejected"


async def _async_record(records, *args, **kwargs):
    records.append((args, kwargs))
    return {"ok": True}


def test_cleanup_attempts_tmux_even_when_session_and_server_cleanup_fail(tmp_path):
    calls = []
    class Server:
        should_exit = False
    class Thread:
        def join(self, timeout):
            calls.append("join")
        def is_alive(self):
            return True
    class Listener:
        def close(self):
            calls.append("close")

    ok = model_test._cleanup_resources(
        Server(), Thread(), Listener(), "s1", {}, tmp_path,
        stop_session_fn=lambda _name: (_ for _ in ()).throw(RuntimeError("failed")),
        kill_tmux_fn=lambda _env, _root: calls.append("tmux") or True,
    )
    assert not ok
    assert calls == ["join", "close", "tmux"]


def test_private_tmux_cleanup_only_accepts_confirmed_absence(tmp_path, monkeypatch):
    env = {"WOLTSPACE_TMUX_BIN": "tmux"}
    monkeypatch.setattr(
        model_test, "_tmux_value",
        lambda *_: (1, "", "no server running on /private/tmux/default"),
    )
    assert model_test._kill_private_tmux(env, tmp_path)
    monkeypatch.setattr(
        model_test, "_tmux_value",
        lambda *_: (1, "", "permission denied"),
    )
    assert not model_test._kill_private_tmux(env, tmp_path)


def test_private_tmux_path_preflight_checks_ownership_and_linux_limit(tmp_path):
    run_root = tmp_path / "model-12345678"
    run_root.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix="wt-", dir="/tmp") as base_name:
        base = Path(base_name)
        socket_dir = model_test._prepare_private_tmux_dir(run_root, base)
        assert socket_dir.parent == base
        assert stat.S_IMODE(socket_dir.stat().st_mode) == 0o700
        candidate = socket_dir / f"tmux-{os.getuid()}" / "default"
        assert len(os.fsencode(candidate)) < 108

    long_base = Path("/tmp") / ("x" * 90)
    try:
        with pytest.raises(RuntimeError, match="socket path exceeds"):
            model_test._prepare_private_tmux_dir(run_root, long_base)
        assert not (long_base / run_root.name).exists()
    finally:
        if long_base.exists():
            long_base.rmdir()


def test_path_length_preflight_happens_before_private_key_read(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(model_test.owner_test, "PRIVATE", private)
    monkeypatch.setattr(model_test.owner_test, "_check_private_dir", lambda: None)
    monkeypatch.setattr(
        model_test, "_auth_path", lambda *_: ("CLAUDE_CONFIG_DIR", tmp_path),
    )
    too_long = Path("/tmp") / ("y" * 90)
    monkeypatch.setattr(model_test, "PRIVATE_TMUX_BASE", too_long)
    key_reads = []
    monkeypatch.setattr(
        model_test.owner_test, "_read_token", lambda: key_reads.append(True),
    )
    try:
        with pytest.raises(RuntimeError, match="socket path exceeds"):
            model_test._run_model_test("claude")
        assert key_reads == []
    finally:
        if too_long.exists():
            too_long.rmdir()


def test_real_tmux_private_socket_start_and_stop_smoke(tmp_path):
    import shutil
    import tempfile

    tmux_bin = shutil.which("tmux")
    if not tmux_bin:
        pytest.skip("tmux is not installed")
    run_root = tmp_path / "model-smoke"
    run_root.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix="wt-", dir="/tmp") as base_name:
        tmux_root = model_test._prepare_private_tmux_dir(run_root, Path(base_name))
        env = {
            "WOLTSPACE_TMUX_BIN": tmux_bin,
            "TMUX_TMPDIR": str(tmux_root),
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(tmp_path),
        }
        try:
            status, _, error = model_test._tmux_value(
                env, "new-session", "-d", "-s", "isolated-smoke", "sleep 30",
            )
            assert status == 0, error
            status, socket_path, error = model_test._tmux_value(
                env, "display-message", "-p", "#{socket_path}",
            )
            assert status == 0, error
            assert Path(socket_path).resolve().is_relative_to(tmux_root.resolve())
            assert model_test._tmux_value(
                env, "show-environment", "-g", "WHATSAPP_AGENT_TOKEN",
            )[0] != 0
            assert model_test._kill_private_tmux(env, tmux_root)
            assert not Path(socket_path).exists()
        finally:
            model_test._kill_private_tmux(env, tmux_root)
            shutil.rmtree(tmux_root, ignore_errors=True)


def test_cleanup_finalizer_retains_root_on_failure_and_removes_on_success(tmp_path, monkeypatch):
    retained = tmp_path / "retained"
    retained.mkdir(mode=0o700)
    assert not model_test._finalize_run_root(retained, False)
    assert retained.is_dir()
    assert (retained / ".cleanup-required").is_file()

    disposable = tmp_path / "disposable"
    disposable.mkdir(mode=0o700)
    socket_base = Path(tempfile.mkdtemp(prefix="wt-", dir="/tmp"))
    monkeypatch.setattr(model_test, "PRIVATE_TMUX_BASE", socket_base)
    tmux_root = model_test._prepare_private_tmux_dir(disposable, socket_base)
    assert model_test._finalize_run_root(disposable, True, tmux_root)
    assert not disposable.exists()
    assert not tmux_root.exists()
