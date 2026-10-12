"""`woltspace access` restores remote access from the lodge's own machine,
and doctor names a published lodge that would refuse every remote request."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from woltspace.cli import main as cli_main  # noqa: E402
from woltspace.doctor import run_doctor  # noqa: E402
from woltspace.layout import RuntimeLayout, installation_root  # noqa: E402

VALUES = [
    "--team-domain", "team.cloudflareaccess.com", "--lodge-aud", "lodge-aud",
    "--owner-email", "Owner@Example.com",
]


@pytest.fixture
def colony(tmp_path, monkeypatch):
    wolts = tmp_path / "wolts"
    wolts.mkdir()
    (wolts / "woltspace.json").write_text(json.dumps({"default_harness": "claude"}))
    for name in ("WOLTSPACE_WOLTS_DIR", "WOLTS_DIR"):
        monkeypatch.setenv(name, str(wolts))
    monkeypatch.setenv("WOLTSPACE_DIR", str(ROOT))
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    return wolts


def test_set_show_and_clear(colony, capsys):
    assert cli_main(["access", "set", *VALUES]) == 0
    saved = json.loads((colony / "woltspace.json").read_text())
    assert saved["access"] == {
        "team_domain": "team.cloudflareaccess.com", "lodge_aud": "lodge-aud",
        "apps_aud": "", "owner_email": "owner@example.com",
    }
    assert saved["default_harness"] == "claude"

    capsys.readouterr()
    assert cli_main(["access", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["access"]["lodge_aud"] == "lodge-aud"

    assert cli_main(["access", "clear"]) == 0
    assert "access" not in json.loads((colony / "woltspace.json").read_text())


def test_invalid_values_are_refused_and_nothing_is_written(colony, capsys):
    before = (colony / "woltspace.json").read_text()
    bad = ["--team-domain", "https://team.example.com/x", "--lodge-aud", "a",
           "--owner-email", "o@example.com"]
    assert cli_main(["access", "set", *bad]) == 1
    assert "team_domain" in capsys.readouterr().err
    assert (colony / "woltspace.json").read_text() == before


def _layout(colony):
    return RuntimeLayout(colony, installation_root(), "127.0.0.1", 18777, "host")


def test_doctor_warns_when_a_published_lodge_does_not_verify_access(colony, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_TOKEN", "token")
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_URL", "https://lodge.example.com")
    monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "true")
    checks = {c.name: c for c in run_doctor(_layout(colony), check_port=False)}
    assert checks["remote-access"].status == "warn"
    assert "treated as you" in checks["remote-access"].detail
    assert "https://lodge.example.com" in checks["remote-access"].detail
    assert "woltspace access set" in checks["remote-access"].remedy

    assert cli_main(["access", "set", *VALUES]) == 0
    checks = {c.name: c for c in run_doctor(_layout(colony), check_port=False)}
    assert checks["remote-access"].status == "pass"


def test_doctor_is_quiet_about_access_when_nothing_is_published(colony, monkeypatch):
    for key in ("CLOUDFLARE_TUNNEL_TOKEN", "CLOUDFLARE_TUNNEL_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "false")
    checks = {c.name for c in run_doctor(_layout(colony), check_port=False)}
    assert "remote-access" not in checks
