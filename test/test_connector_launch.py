"""The lodge launches the Telegram connector: plan, isolated environment,
hardened token handoff, restart re-mints, stop waits. Fake children only."""

import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from woltspace import handoff as handoff_module  # noqa: E402
from woltspace.channel_supervisor import ChannelSupervisor, read_connector_report  # noqa: E402
from woltspace.channels import ConnectorPlan, TelegramConnector, TelegramRuntimeConnector, plan_connectors  # noqa: E402
from woltspace.layout import RuntimeLayout  # noqa: E402
from server.connectors import ConnectorStore, set_channel_runtime  # noqa: E402

TOKEN = "123456:configured-telegram-token"
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp/home", "WOLTSPACE_ENTRYPOINT": "1", "SECRET_OF_THE_LODGE": "x"}

# A child that behaves like the runner's startup: takes the handoff the hard
# way, proves what it was given, then lives until told to stop.
CHILD = r'''
import json, os, signal, stat, sys, time
report = os.environ["CHILD_REPORT"]
path = os.environ.get("WOLTSPACE_CONNECTOR_TOKEN_FILE")
record = {"argv": sys.argv, "env": sorted(os.environ), "token": None, "mode": None, "dir_mode": None}
if path and os.environ.get("CHILD_CONSUME", "1") == "1":
    info = os.lstat(path)
    record["mode"] = stat.S_IMODE(info.st_mode)
    record["dir_mode"] = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
    with open(path) as handle:
        record["token"] = json.load(handle)["token"]
    os.unlink(path)
with open(report, "a") as handle:
    handle.write(json.dumps(record) + "\n")
def bye(*_):
    time.sleep(float(os.environ.get("CHILD_EXIT_DELAY", "0")))
    with open(report, "a") as handle:
        handle.write(json.dumps({"exit": "clean"}) + "\n")
    sys.exit(0)
signal.signal(signal.SIGTERM, bye)
while True:
    time.sleep(0.05)
'''


@pytest.fixture
def layout(tmp_path):
    return RuntimeLayout(
        wolts_dir=tmp_path / "wolts", install_root=ROOT, host="127.0.0.1", port=7799, isolation="host",
    )


@pytest.fixture
def colony(layout):
    """A lodge with a bot token configured and an enabled Telegram instance."""
    layout.platform_state.mkdir(parents=True, exist_ok=True)
    (layout.platform_state / "config.json").write_text(json.dumps({
        "channels": {"telegram": {"enabled": True, "token": TOKEN}},
    }))
    store = ConnectorStore(layout.wolts_dir / ".space" / "connectors")
    store.add_instance("tg", "telegram", "bot:123456")
    store.bind("tg", sender="101", conversation="101", default_wolt="softie")
    store.enable("tg")
    return store


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _child_plan(plan: ConnectorPlan, report: Path, **extra_env) -> ConnectorPlan:
    """The real plan with its command swapped for the fake child."""
    env = {**plan.env, "CHILD_REPORT": str(report), **extra_env}
    return ConnectorPlan(
        plan.name, True, plan.detail, (sys.executable, "-c", CHILD), plan.cwd, env, plan.remedy,
        plan.process_signature, isolated_env=plan.isolated_env, handoff=plan.handoff,
    )


# ------------------------------------------------------------------ plans


def test_the_plan_follows_the_switch_and_needs_an_enabled_instance(layout, colony):
    runtime, adapter = TelegramRuntimeConnector(), TelegramConnector()
    plan = runtime.plan(layout, ENV)
    assert plan.enabled is False and "telegram.runtime is adapter" in plan.detail
    assert adapter.plan(layout, ENV).enabled is True
    set_channel_runtime(layout.wolts_dir / "woltspace.json", "telegram", "connector")
    plan = runtime.plan(layout, ENV)
    assert plan.enabled is True
    assert adapter.plan(layout, ENV).enabled is False
    assert plan.command == (sys.executable, "-m", "woltspace_telegram")
    assert plan.cwd == str(ROOT / "connectors" / "telegram")
    assert plan.process_signature == ("-m", "woltspace_telegram")
    assert plan.isolated_env is True and plan.handoff is not None
    # Exactly one poller per channel is planned, whichever runtime owns it.
    names = [p.name for p in plan_connectors(layout, ENV) if p.enabled and p.name.startswith("telegram")]
    assert names == ["telegram-connector"]
    colony.disable("tg")
    plan = runtime.plan(layout, ENV)
    assert plan.enabled is False and "no enabled telegram instance" in plan.detail


def test_the_child_environment_is_only_what_the_manifest_declares(layout, colony):
    set_channel_runtime(layout.wolts_dir / "woltspace.json", "telegram", "connector")
    plan = TelegramRuntimeConnector().plan(layout, ENV)
    assert set(plan.env) == {"PATH", "HOME", "TELEGRAM_BOT_TOKEN", "WOLTSPACE_API", "PYTHONPATH", "PYTHONUNBUFFERED"}
    assert plan.env["WOLTSPACE_API"] == "http://127.0.0.1:7799"
    assert "SECRET_OF_THE_LODGE" not in plan.env and "WOLTSPACE_ENTRYPOINT" not in plan.env
    assert TOKEN not in " ".join(plan.command)
    record = plan.to_record()
    assert "env" not in record and "handoff" not in record and TOKEN not in json.dumps(record)


# -------------------------------------------------------------- launching


def test_launch_hands_the_token_over_by_file_and_the_child_sees_nothing_else(layout, colony, monkeypatch):
    set_channel_runtime(layout.wolts_dir / "woltspace.json", "telegram", "connector")
    monkeypatch.setenv("SECRET_OF_THE_LODGE", "leaks?")
    report = layout.wolts_dir / "child-report.jsonl"
    plan = _child_plan(TelegramRuntimeConnector().plan(layout, ENV), report)
    sup = ChannelSupervisor(layout, [plan], poll_interval=0.05)
    sup.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _records(report):
            time.sleep(0.05)
        [seen] = _records(report)
        assert seen["token"].startswith("wct_") and seen["mode"] == 0o600 and seen["dir_mode"] == 0o700
        assert colony.authenticate(seen["token"]).generation == 1
        assert not any(seen["token"] in arg for arg in seen["argv"])
        # The interpreter adds its own locale variables on macOS; nothing of
        # the lodge's environment gets through.
        platform_added = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
        assert set(seen["env"]) - platform_added == {
            "PATH", "HOME", "TELEGRAM_BOT_TOKEN", "WOLTSPACE_API", "PYTHONPATH", "PYTHONUNBUFFERED",
            "WOLTSPACE_CONNECTOR_TOKEN_FILE", "CHILD_REPORT",
        }
        assert "SECRET_OF_THE_LODGE" not in seen["env"] and "WOLTSPACE_ENTRYPOINT" not in seen["env"]
        assert list((layout.platform_state / "run").glob("handoff-*.json")) == []
        sup.poll_once()
        state = read_connector_report(layout)["connectors"][0]
        assert state["state"] == "running" and TOKEN not in json.dumps(state)
    finally:
        sup.stop()
    # Stopped: the generation's token is revoked.
    assert colony.authenticate(seen["token"]) is None


def test_a_restart_is_a_relaunch_and_re_mints(layout, colony):
    set_channel_runtime(layout.wolts_dir / "woltspace.json", "telegram", "connector")
    report = layout.wolts_dir / "child-report.jsonl"
    plan = _child_plan(TelegramRuntimeConnector().plan(layout, ENV), report)
    sup = ChannelSupervisor(layout, [plan], poll_interval=0.05)
    sup.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _records(report):
            time.sleep(0.05)
        first = _records(report)[0]["token"]
        os.kill(read_connector_report(layout)["connectors"][0]["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            records = [r for r in _records(report) if r.get("token")]
            if len(records) == 2:
                break
            time.sleep(0.05)
        second = records[1]["token"]
        assert second != first
        assert colony.authenticate(first) is None
        assert colony.authenticate(second).generation == 2
    finally:
        sup.stop()
    assert colony.authenticate(second) is None


def test_stop_waits_for_the_child_to_exit(layout, colony):
    set_channel_runtime(layout.wolts_dir / "woltspace.json", "telegram", "connector")
    report = layout.wolts_dir / "child-report.jsonl"
    plan = _child_plan(TelegramRuntimeConnector().plan(layout, ENV), report, CHILD_EXIT_DELAY="0.4")
    sup = ChannelSupervisor(layout, [plan])
    sup.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not _records(report):
        time.sleep(0.05)
    pid = read_connector_report(layout)["connectors"][0]["pid"]
    started = time.monotonic()
    sup.stop()
    assert time.monotonic() - started >= 0.4
    assert _records(report)[-1] == {"exit": "clean"}
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_handoff_not_consumed_in_time_fails_closed(layout, colony, monkeypatch):
    """The child never reads its handoff: the token is revoked, the child is not started."""
    monkeypatch.setattr(handoff_module, "CONSUME_TIMEOUT", 0.3)
    set_channel_runtime(layout.wolts_dir / "woltspace.json", "telegram", "connector")
    report = layout.wolts_dir / "child-report.jsonl"
    plan = _child_plan(TelegramRuntimeConnector().plan(layout, ENV), report, CHILD_CONSUME="0")
    sup = ChannelSupervisor(layout, [plan], sleep=lambda _s: None)
    sup.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _records(report):
            time.sleep(0.05)
        [pending] = list((layout.platform_state / "run").glob("handoff-*.json"))
        minted = json.loads(pending.read_text())["token"]
        assert colony.authenticate(minted) is not None
        time.sleep(0.35)
        sup.poll_once()
        assert not pending.exists()
        assert colony.authenticate(minted) is None
        state = read_connector_report(layout)["connectors"][0]
        assert "did not take its handoff" in (state["error"] or "") or state["restarts"] >= 1
        assert state["restarts"] == 1
    finally:
        sup.stop()


def test_a_handoff_consumed_twice_or_tampered_is_refused_by_the_runner(tmp_path):
    sys.path.insert(0, str(ROOT / "connectors" / "telegram"))
    from woltspace_telegram.runner import read_handoff

    path = tmp_path / "handoff.json"
    path.write_text(json.dumps({"token": "wct_x", "instance_id": "tg", "generation": 1}))
    os.chmod(path, 0o600)
    assert read_handoff(str(path))["token"] == "wct_x"
    with pytest.raises(SystemExit, match="already consumed"):
        read_handoff(str(path))
    path.write_text(json.dumps({"token": "wct_x"}))
    os.chmod(path, 0o644)
    with pytest.raises(SystemExit, match="readable by others"):
        read_handoff(str(path))
    os.chmod(path, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(SystemExit):
        read_handoff(str(link))
    path.write_text("{not json")
    with pytest.raises(SystemExit, match="does not parse"):
        read_handoff(str(path))
    assert not path.exists()


def test_a_child_that_dies_before_consuming_leaves_no_live_token(layout, colony):
    set_channel_runtime(layout.wolts_dir / "woltspace.json", "telegram", "connector")
    plan = TelegramRuntimeConnector().plan(layout, ENV)
    crasher = ConnectorPlan(
        plan.name, True, plan.detail, (sys.executable, "-c", "import sys; sys.exit(3)"), plan.cwd,
        plan.env, plan.remedy, plan.process_signature, isolated_env=True, handoff=plan.handoff,
    )
    sup = ChannelSupervisor(layout, [crasher], sleep=lambda _s: None, max_restarts=1)
    sup.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            sup.poll_once()
            if read_connector_report(layout)["connectors"][0]["state"] == "failed":
                break
            time.sleep(0.05)
    finally:
        sup.stop()
    registry = json.loads((colony.root / "registry.json").read_text())
    assert registry["tokens"] and all(t["revoked"] is not None for t in registry["tokens"])
    assert list((layout.platform_state / "run").glob("handoff-*.json")) == []
