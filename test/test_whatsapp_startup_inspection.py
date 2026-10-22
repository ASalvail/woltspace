"""Offline orchestration: real notify CLI/API, synthetic registry, no model/provider."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_status_line_contains_only_fixed_diagnostics():
    from scripts import whatsapp_startup_inspection as startup
    diagnostics = startup.model.RunDiagnostics("codex")
    diagnostics.set_harness_process("present")
    diagnostics.set_harness_readiness("unsupported")
    assert startup._status_line(diagnostics) == (
        "Startup inspection process=present readiness=unsupported symptom=not_checked "
        "notify_attempt=not_seen cleanup=not_reached."
    )


def test_startup_uses_supported_session_creation_with_prompt_policy(monkeypatch):
    from scripts import whatsapp_startup_inspection as startup
    import sessions

    calls = []
    monkeypatch.setattr(sessions, "start_session", lambda **kw: calls.append(kw) or {"name": "synthetic"})
    assert startup._start_session() == {"name": "synthetic"}
    assert calls[0]["execution_policy"] == "prompt"
    assert calls[0]["harness"] == "codex"
    assert calls[0]["prompt"] == startup.PROMPT
    assert calls[0]["routing"]["chat_id"] == startup.PARTICIPANT


def test_real_prepare_preserves_trust_prompts_and_prompt_policy(tmp_path, monkeypatch):
    from scripts import whatsapp_startup_inspection as startup
    import sessions
    import harnesses
    from session_runtime import TmuxSessionRuntime

    wolts = tmp_path / "wolts"
    wolts.mkdir()
    wolt = startup.model._create_synthetic_wolt(wolts, "codex")
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(wolts))
    monkeypatch.setenv("WOLTS_DIR", str(wolts))
    monkeypatch.setenv("WOLTSPACE_DIR", str(ROOT))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "private-codex"))
    monkeypatch.setenv("WOLTSPACE_PRESERVE_TRUST_PROMPTS", "1")
    monkeypatch.setattr(harnesses, "codex_supports_no_daemon", lambda: False)
    def no_trust_write(*_):
        raise AssertionError("trust dialog must not be pre-accepted")
    monkeypatch.setattr(sessions, "ensure_codex_dir_trusted", no_trust_write)
    sessions.SessionRegistry(wolts).create(
        "synthetic-startup", wolt="wa-test", harness="codex", dir=str(wolt),
        execution_policy={"mode": "prompt", "isolation": "host"},
        adapter="whatsapp", chat_id=startup.PARTICIPANT, whatsapp_account=startup.ACCOUNT,
    )
    command = sessions.prepare_session_command("synthetic-startup", "spawn", startup.PROMPT)
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert not (tmp_path / "private-codex" / "config.toml").exists()
    # This is the actual launch envelope passed to tmux, which must carry the
    # opt-in to run-session.sh/session-reg in the separate bootstrap process.
    launch = TmuxSessionRuntime._launch_command("synthetic-command")
    assert "WOLTSPACE_PRESERVE_TRUST_PROMPTS=1" in launch


@pytest.mark.parametrize("scenario", [
    "success", "wrong_payload", "timeout", "trust_prompt", "spawn_failure", "cleanup_failure",
])
def test_full_offline_startup_orchestration(tmp_path, scenario):
    # Fresh interpreter ensures real server/session imports bind to only the
    # synthetic environment. No owner's private directory/auth is involved.
    code = r'''
import json, os, subprocess, sys, tempfile, threading
from pathlib import Path
from scripts import whatsapp_startup_inspection as startup
model = startup.model
base = Path(sys.argv[1])
scenario = sys.argv[2]
private = base / "private"
private.mkdir(mode=0o700)
auth = base / "synthetic-auth"
auth.mkdir(mode=0o700)
model.owner_test.PRIVATE = private
model.owner_test._check_private_dir = lambda: None
model._auth_path = lambda *_: ("CODEX_HOME", auth)
model._preflight_harness = lambda *args: args[-1].passed("preflight")
def key_forbidden():
    raise AssertionError("credential access forbidden")
model.owner_test._read_token = key_forbidden
model._verify_private_tmux = lambda *_: None
model._harness_process_status = lambda *_: True
model._harness_readiness_status = lambda *_: "unsupported"
model._capture_boot_symptom = lambda *_: "trust_or_approval" if scenario == "trust_prompt" else "none_detected"
real_cleanup = model._cleanup_resources
def cleanup(*args):
    return real_cleanup(*args, stop_session_fn=lambda _: {"status": "stopped"},
                        kill_tmux_fn=lambda *_: scenario != "cleanup_failure")
model._cleanup_resources = cleanup
cli_threads = []
cli_results = []
def fake_start():
    assert os.environ.get("WOLTSPACE_PRESERVE_TRUST_PROMPTS") == "1"
    if scenario == "spawn_failure":
        raise RuntimeError("private-raw-start-error synthetic-secret")
    # Supported registry path and real notify routing, without tmux/model spawn.
    from sessions import SessionRegistry
    registry = SessionRegistry()
    record = registry.create(
        "wa-test-offline-startup", wolt="wa-test", harness="codex", creature="otter",
        dir=str(Path(os.environ["WOLTS_DIR"]) / "wa-test"),
        adapter="whatsapp", chat_id=startup.PARTICIPANT,
        whatsapp_account=startup.ACCOUNT,
        execution_policy={"mode": "prompt", "isolation": "host"},
    )
    registry.update(record["name"], wolt="wa-test", runtime={
        "kind": "tmux", "tmux_session_name": "synthetic", "pane_id": "%1",
    })
    if scenario not in {"timeout", "trust_prompt"}:
        def cli():
            env = dict(os.environ)
            env["WOLTSPACE_WOLT_SESSION"] = record["name"]
            message = "wrong-synthetic-body" if scenario == "wrong_payload" else startup.BODY
            result = subprocess.run(
                [sys.executable, str(startup.ROOT / "container/bin/notify"),
                 "--whatsapp", startup.PARTICIPANT], input=message,
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=10,
            )
            cli_results.append(result.returncode)
        thread = threading.Thread(target=cli)
        thread.start()
        cli_threads.append(thread)
    return {"name": record["name"]}
startup._start_session = fake_start
diag = model.RunDiagnostics("codex")
with tempfile.TemporaryDirectory(prefix=".wsi-offline-", dir=startup.ROOT.parents[2]) as sockets:
    model.PRIVATE_TMUX_BASE = Path(sockets)
    try:
        result = model._run_model_test("codex", diag, startup_runner=lambda *args:
            startup._run_startup_session(*args, timeout_seconds=1 if scenario == "timeout" else 5))
    except Exception:
        result = 2
    for thread in cli_threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
record = diag.as_record()
assert model._valid_diagnostic_record(record)
assert "bot.whatsapp_adapter" not in sys.modules
if scenario == "success":
    assert result == 0
    assert record["notify_attempt"] == "sent"
    assert record["checks"]["notify"] == "passed"
    assert record["checks"]["cleanup"] == "passed"
    assert cli_results == [0]
    assert not list(private.glob("startup-*"))
elif scenario == "cleanup_failure":
    assert result == 2 and record["category"] == "cleanup_failed"
    assert record["notify_attempt"] == "sent"
    assert record["checks"]["cleanup"] == "failed"
else:
    assert result == 2
    assert record["category"] == {
        "wrong_payload": "notify_rejected", "timeout": "run_timeout",
        "trust_prompt": "trust_or_approval_prompt",
        "spawn_failure": "session_not_started",
    }[scenario]
    assert record["checks"]["notify"] != "passed"
    assert record["notify_attempt"] == ("rejected" if scenario == "wrong_payload" else "not_seen")
    assert record["checks"]["cleanup"] == "passed"
if scenario != "success":
    paths = list(private.glob("startup-*/failure.json"))
    assert len(paths) == 1
    data = json.loads(paths[0].read_text())
    assert model._valid_diagnostic_record(data)
    assert all(term not in str(data) for term in (startup.PARTICIPANT, startup.BODY, startup.PROMPT))
    assert not list(paths[0].parent.rglob("*.log"))
print("offline_startup_ok")
'''
    env = dict(os.environ)
    env.pop("WHATSAPP_AGENT_TOKEN", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), scenario], cwd=ROOT, env=env,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35,
    )
    # Every fixture is synthetic; failures here cannot expose real private text.
    assert result.returncode == 0, result.stderr
    assert "offline_startup_ok" in result.stdout
    assert "private-raw-start-error" not in result.stdout + result.stderr
