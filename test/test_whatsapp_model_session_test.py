import importlib.util
import asyncio
import runpy
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "whatsapp_model_session_test_script",
    ROOT / "scripts" / "whatsapp_model_session_test.py",
)
model_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(model_test)


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
        "127.0.0.1", 41321, inherited,
    )
    assert env["CLAUDE_CONFIG_DIR"] == str(auth.resolve())
    assert env["HOME"] == str(run_root / "home")
    assert env["TMUX_TMPDIR"] == str(run_root / "tmux")
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

    gate = model_test.OneNonceSendGate("🦦 wa-test: wa-model-check", verify)
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
    gate = model_test.OneNonceSendGate("🦦 wa-test: nonce", lambda *_: "")
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
    gate = model_test.OneNonceSendGate("🦦 wa-test: nonce", lambda *_: "")
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
    gate = model_test.OneNonceSendGate("🦦 wa-test: nonce", lambda *_: "s1")

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


def test_cleanup_finalizer_retains_root_on_failure_and_removes_on_success(tmp_path):
    retained = tmp_path / "retained"
    retained.mkdir(mode=0o700)
    assert not model_test._finalize_run_root(retained, False)
    assert retained.is_dir()
    assert (retained / ".cleanup-required").is_file()

    disposable = tmp_path / "disposable"
    disposable.mkdir(mode=0o700)
    assert model_test._finalize_run_root(disposable, True)
    assert not disposable.exists()
