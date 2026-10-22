import importlib.util
import os
import sys
import stat
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "container" / "lib")]
SPEC = importlib.util.spec_from_file_location(
    "whatsapp_owner_test_script", ROOT / "scripts" / "whatsapp_owner_test.py"
)
owner_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner_test)


def test_private_credential_store_has_required_permissions(tmp_path, monkeypatch):
    private = tmp_path / "private-test"
    monkeypatch.setattr(owner_test, "PRIVATE", private)
    monkeypatch.setattr(owner_test, "TOKEN_FILE", private / "agent-key")
    owner_test._store_token("synthetic-test-placeholder")
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(owner_test.TOKEN_FILE.stat().st_mode) == 0o600
    assert owner_test._read_token() == "synthetic-test-placeholder"


def test_owner_child_environment_drops_whatsapp_secret(monkeypatch):
    monkeypatch.setenv("WHATSAPP_AGENT_TOKEN", "synthetic-test-placeholder")
    child_env = owner_test._child_environment()
    assert "WHATSAPP_AGENT_TOKEN" not in child_env
    assert os.environ["WHATSAPP_AGENT_TOKEN"] == "synthetic-test-placeholder"


def test_owner_notify_stage_uses_real_registry_and_notify_with_mocked_transport(
    tmp_path, monkeypatch
):
    import hashlib
    from types import SimpleNamespace
    import server.notify as notify

    sent = []
    monkeypatch.setattr(notify, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(notify, "dotenv_env", lambda name: "synthetic-token" if name == "WHATSAPP_AGENT_TOKEN" else "")
    monkeypatch.setattr(notify, "append_chat_history", lambda *args: None)
    async def mock_transport(*args):
        return _record(sent, args)
    monkeypatch.setattr(notify, "whatsapp_send", mock_transport)
    wolt_json = tmp_path / owner_test.TEST_WOLT / "wolt" / "wolt.json"
    wolt_json.parent.mkdir(parents=True)
    wolt_json.write_text('{"name":"wa-test","type":"otter"}\n')
    adapter = SimpleNamespace(
        TOKEN="synthetic-token",
        ACCOUNT=hashlib.sha256(b"synthetic-token").hexdigest()[:20],
    )
    challenge = "wa-check-synthetic"
    completed = {"reply": False, "error": ""}
    dispatch = owner_test._notify_path_dispatch(adapter, tmp_path, challenge, completed)
    dispatch({
        "from": "user:99",
        "text": {"body": challenge},
    }, {}, 100)
    assert completed == {"reply": True, "error": ""}
    assert sent == [("synthetic-token", "user:99", "WhatsApp registry and notify path check succeeded.")]


def _record(items, value):
    items.append(value)
    return {"ok": True}


def test_owner_setup_fails_closed_when_no_real_terminal(monkeypatch):
    monkeypatch.setattr(owner_test, "_require_tty", lambda: (_ for _ in ()).throw(
        RuntimeError("no terminal")
    ))
    monkeypatch.setattr(owner_test.getpass, "getpass", lambda *args, **kwargs: pytest.fail("credential prompt must not run"))
    with pytest.raises(RuntimeError, match="no terminal"):
        owner_test._setup()


def test_packaged_skill_tree_contains_whatsapp_start_chat_mode():
    skill = ROOT / "container" / "skills" / "start-chat"
    mode = skill / "modes" / "whatsapp.md"
    package = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert mode.is_file()
    assert "whatsapp" in (skill / "SKILL.md").read_text().lower()
    assert package["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]["container"]
    assert "/container" in package["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
