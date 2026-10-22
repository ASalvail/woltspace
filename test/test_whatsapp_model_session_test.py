import importlib.util
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
