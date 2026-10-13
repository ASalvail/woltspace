"""The lodge launches the Telegram connector: plan, isolated environment,
hardened token handoff (pending generation, activation, anchored read),
cleanup on every failure, restart re-mints, stop waits. Fake children only."""

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))
sys.path.insert(0, str(ROOT / "connectors" / "telegram"))

import server.app as server_app  # noqa: E402
from server import connector_api  # noqa: E402
from server.connectors import ConnectorError, ConnectorStore, Limits, set_channel_runtime  # noqa: E402
from woltspace import handoff as handoff_module  # noqa: E402
from woltspace.channel_supervisor import ChannelSupervisor, read_connector_report  # noqa: E402
from woltspace.channels import ConnectorPlan, TelegramConnector, TelegramRuntimeConnector, plan_connectors  # noqa: E402
from woltspace.handoff import ConnectorHandoff  # noqa: E402
from woltspace.layout import RuntimeLayout  # noqa: E402
from woltspace_telegram.runner import read_handoff  # noqa: E402

TOKEN = "123456:configured-telegram-token"
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp/home", "WOLTSPACE_ENTRYPOINT": "1", "SECRET_OF_THE_LODGE": "x"}
DECLARED_ENV = {
    "PATH", "HOME", "TELEGRAM_BOT_TOKEN", "WOLTSPACE_API", "PYTHONPATH", "PYTHONUNBUFFERED",
    "WOLTSPACE_CONNECTOR_TOKEN_FILE", "WOLTSPACE_LODGE_ID", "WOLTSPACE_CONNECTOR_INSTANCE",
    "WOLTSPACE_CONNECTOR_GENERATION", "WOLTSPACE_HANDOFF_WINDOW",
}
# The interpreter adds its own locale variables on macOS.
PLATFORM_ADDED = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}

# A child that does what the runner's startup does: takes the handoff the
# hard way (the runner's own reader), activates its generation with the
# lodge (here: the store directly, as there is no HTTP lodge), proves what
# it was given, then lives until told to stop.
CHILD = r'''
import json, os, signal, stat, sys, time
sys.path.insert(0, os.environ["CHILD_ROOT"]); sys.path.insert(0, os.environ["CHILD_ROOT"] + "/container/lib")
sys.path.insert(0, os.environ["CHILD_ROOT"] + "/connectors/telegram")
from woltspace_telegram.runner import connector_token
from server.connectors import ConnectorStore
report = os.environ["CHILD_REPORT"]
path = os.environ.get("WOLTSPACE_CONNECTOR_TOKEN_FILE")
record = {"argv": sys.argv, "env": sorted(k for k in os.environ if not k.startswith("CHILD_")), "token": None, "mode": None, "dir_mode": None}
if path and os.environ.get("CHILD_CONSUME", "1") == "1":
    info = os.lstat(path)
    record["mode"] = stat.S_IMODE(info.st_mode)
    record["dir_mode"] = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
    token, activation = connector_token()
    record["token"] = token
    if os.environ.get("CHILD_ACTIVATE", "1") == "1":
        ConnectorStore(os.environ["CHILD_STORE"]).activate(token, **activation)
        record["activated"] = True
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
    """A lodge with a bot token configured and an enabled Telegram instance, switched to the connector."""
    layout.platform_state.mkdir(parents=True, exist_ok=True)
    (layout.platform_state / "config.json").write_text(json.dumps({
        "channels": {"telegram": {"enabled": True, "token": TOKEN}},
    }))
    store = ConnectorStore(layout.wolts_dir / ".space" / "connectors")
    store.add_instance("tg", "telegram", "bot:123456")
    store.bind("tg", sender="101", conversation="101", default_wolt="softie")
    store.enable("tg")
    set_channel_runtime(layout.wolts_dir / "woltspace.json", "telegram", "connector")
    return store


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _live(store: ConnectorStore) -> list[dict]:
    return [t for t in store._registry()["tokens"] if t["revoked"] is None]


def _child_plan(plan: ConnectorPlan, report: Path, colony: ConnectorStore, **extra_env) -> ConnectorPlan:
    """The real plan with its command swapped for the fake child."""
    env = {**plan.env, "CHILD_REPORT": str(report), "CHILD_ROOT": str(ROOT), "CHILD_STORE": str(colony.root), **extra_env}
    return ConnectorPlan(
        plan.name, True, plan.detail, (sys.executable, "-c", CHILD), plan.cwd, env, plan.remedy,
        plan.process_signature, isolated_env=plan.isolated_env, handoff=plan.handoff,
    )


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ------------------------------------------------------------------ plans


def test_the_plan_follows_the_switch_and_needs_an_enabled_instance(layout, colony):
    runtime, adapter = TelegramRuntimeConnector(), TelegramConnector()
    plan = runtime.plan(layout, ENV)
    assert plan.enabled is True and adapter.plan(layout, ENV).enabled is False
    assert plan.command == (sys.executable, "-m", "woltspace_telegram")
    assert plan.cwd == str(ROOT / "connectors" / "telegram")
    assert plan.process_signature == ("-m", "woltspace_telegram")
    assert plan.isolated_env is True and plan.handoff is not None
    names = [p.name for p in plan_connectors(layout, ENV) if p.enabled and p.name.startswith("telegram")]
    assert names == ["telegram-connector"]
    set_channel_runtime(layout.wolts_dir / "woltspace.json", "telegram", "adapter")
    plan = runtime.plan(layout, ENV)
    assert plan.enabled is False and "telegram.runtime is adapter" in plan.detail
    assert adapter.plan(layout, ENV).enabled is True
    set_channel_runtime(layout.wolts_dir / "woltspace.json", "telegram", "connector")
    colony.disable("tg")
    plan = runtime.plan(layout, ENV)
    assert plan.enabled is False and "no enabled telegram instance" in plan.detail


@pytest.mark.parametrize("runtime", ["adapter", "connector"])
def test_the_channels_off_switch_means_the_same_under_both_runtimes(layout, colony, runtime):
    """Finding 3: channels.telegram.enabled and ENABLE_TELEGRAM_BOT=0 switch both runtimes off."""
    set_channel_runtime(layout.wolts_dir / "woltspace.json", "telegram", runtime)
    planner = TelegramRuntimeConnector() if runtime == "connector" else TelegramConnector()
    assert planner.plan(layout, ENV).enabled is True
    assert planner.plan(layout, {**ENV, "ENABLE_TELEGRAM_BOT": "0"}).enabled is False
    (layout.platform_state / "config.json").write_text(json.dumps({
        "channels": {"telegram": {"enabled": False, "token": TOKEN}},
    }))
    plan = planner.plan(layout, ENV)
    assert plan.enabled is False and plan.detail == "disabled"
    # The environment can still turn it on, as for the adapter.
    assert planner.plan(layout, {**ENV, "ENABLE_TELEGRAM_BOT": "1"}).enabled is True


def test_the_child_environment_is_only_what_the_manifest_declares(layout, colony):
    plan = TelegramRuntimeConnector().plan(layout, ENV)
    assert set(plan.env) == {"PATH", "HOME", "TELEGRAM_BOT_TOKEN", "WOLTSPACE_API", "PYTHONPATH", "PYTHONUNBUFFERED"}
    assert plan.env["WOLTSPACE_API"] == "http://127.0.0.1:7799"
    assert "SECRET_OF_THE_LODGE" not in plan.env and "WOLTSPACE_ENTRYPOINT" not in plan.env
    assert TOKEN not in " ".join(plan.command)
    record = plan.to_record()
    assert "env" not in record and TOKEN not in json.dumps(record)


# -------------------------------------------------------------- launching


def test_launch_hands_a_pending_token_over_by_file_and_the_child_activates_it(layout, colony, monkeypatch):
    monkeypatch.setenv("SECRET_OF_THE_LODGE", "leaks?")
    report = layout.wolts_dir / "child-report.jsonl"
    plan = _child_plan(TelegramRuntimeConnector().plan(layout, ENV), report, colony)
    sup = ChannelSupervisor(layout, [plan], poll_interval=0.05)
    sup.start()
    try:
        assert _wait_for(lambda: _records(report))
        [seen] = _records(report)
        assert seen["token"].startswith("wct_") and seen["mode"] == 0o600 and seen["dir_mode"] == 0o700
        assert seen["activated"] is True
        assert colony.authenticate(seen["token"]).generation == 1
        assert colony.token_state("tg", 1) == "active"
        assert not any(seen["token"] in arg for arg in seen["argv"])
        assert set(seen["env"]) - PLATFORM_ADDED == DECLARED_ENV
        assert "SECRET_OF_THE_LODGE" not in seen["env"] and "WOLTSPACE_ENTRYPOINT" not in seen["env"]
        assert list((layout.platform_state / "run").glob("handoff-*.json")) == []
        sup.poll_once()
        state = sup.report()["connectors"][0]
        assert state["state"] == "running" and TOKEN not in json.dumps(state)
        assert state["handoff"] == {"generation": 1, "activated": True, "pending_file": False, "unrevoked": {}}
    finally:
        sup.stop()
    after = read_connector_report(layout)["connectors"][0]
    assert after["shutdown"] == "confirmed"
    assert colony.authenticate(seen["token"]) is None and _live(colony) == []


def test_a_pending_token_authenticates_nowhere_but_activation(layout, colony, monkeypatch):
    """A handoff in flight opens no route; activation needs the registry's own identity."""
    handoff = ConnectorHandoff(lambda: colony, "tg", layout.platform_state / "run")
    env = handoff.prepare()
    token = json.loads(Path(env["WOLTSPACE_CONNECTOR_TOKEN_FILE"]).read_text())["token"]
    assert colony.authenticate(token) is None and colony.token_state("tg", 1) == "pending"
    monkeypatch.setattr(connector_api, "CONNECTORS_DIR", colony.root)
    monkeypatch.setattr(connector_api, "_store", colony)
    async def calls():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server_app.app, client=("127.0.0.1", 1)), base_url="http://localhost:7777",
        ) as api:
            headers = {"Authorization": f"Bearer {token}"}
            assert (await api.get("/connector/v1/state", headers=headers)).status_code == 401
            assert (await api.post("/connector/v1/deliveries/lease", json={"max": 1}, headers=headers)).status_code == 401
            wrong = await api.post("/connector/v1/activate", json={
                "lodge_id": "not-this-lodge", "instance_id": "tg", "generation": 1,
            }, headers=headers)
            assert wrong.status_code == 401
            wrong_generation = await api.post("/connector/v1/activate", json={
                "lodge_id": env["WOLTSPACE_LODGE_ID"], "instance_id": "tg", "generation": 2,
            }, headers=headers)
            assert wrong_generation.status_code == 401
            assert colony.token_state("tg", 1) == "pending"
            good = await api.post("/connector/v1/activate", json={
                "lodge_id": env["WOLTSPACE_LODGE_ID"], "instance_id": "tg", "generation": 1,
            }, headers=headers)
            assert good.json() == {"activated": True, "instance_id": "tg", "generation": 1}
            assert (await api.get("/connector/v1/state", headers=headers)).status_code == 200
            # Exactly once.
            again = await api.post("/connector/v1/activate", json={
                "lodge_id": env["WOLTSPACE_LODGE_ID"], "instance_id": "tg", "generation": 1,
            }, headers=headers)
            assert again.status_code == 401

    import asyncio
    asyncio.run(calls())
    assert handoff.check() is None and handoff.activated is True


def test_activation_races_timeout_and_revocation_with_one_outcome(layout, colony):
    """Finding 2 (c): exactly one activation wins; a timed-out or revoked generation cannot."""
    clock = [1000.0]
    store = ConnectorStore(colony.root, clock=lambda: clock[0], limits=Limits(handoff_window_seconds=5))
    lodge_id = store.lodge_id()
    token = store.issue_token("tg", pending=True)
    expectation = {"lodge_id": lodge_id, "instance_id": "tg", "generation": 1}
    # Two consumers racing activation: one wins, the other is refused.
    outcomes = []
    barrier = threading.Barrier(2)

    def race():
        barrier.wait()
        try:
            store.activate(token, **expectation)
            outcomes.append("won")
        except ConnectorError:
            outcomes.append("refused")

    threads = [threading.Thread(target=race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["refused", "won"] and store.token_state("tg", 1) == "active"
    # Timed out: activation revokes it on the spot.
    token = store.issue_token("tg", pending=True)
    clock[0] += 6
    with pytest.raises(ConnectorError):
        store.activate(token, lodge_id=lodge_id, instance_id="tg", generation=2)
    assert store.token_state("tg", 2) == "revoked"
    # Revoked first: activation is refused and the record stays revoked.
    token = store.issue_token("tg", pending=True)
    store.revoke_generation("tg", 3)
    with pytest.raises(ConnectorError):
        store.activate(token, lodge_id=lodge_id, instance_id="tg", generation=3)
    assert store.token_state("tg", 3) == "revoked"
    # Activation then a revocation of a newer generation leaves the older one alone.
    token = store.issue_token("tg", pending=True)
    store.activate(token, lodge_id=lodge_id, instance_id="tg", generation=4)
    newer = store.issue_token("tg", pending=True)
    store.revoke_generation("tg", 5)
    assert store.token_state("tg", 4) == "revoked"   # the mint of 5 revoked 4, by design
    assert store.authenticate(newer) is None


def test_a_restart_is_a_relaunch_and_re_mints(layout, colony):
    report = layout.wolts_dir / "child-report.jsonl"
    plan = _child_plan(TelegramRuntimeConnector().plan(layout, ENV), report, colony)
    sup = ChannelSupervisor(layout, [plan], poll_interval=0.05)
    sup.start()
    try:
        assert _wait_for(lambda: _records(report))
        first = _records(report)[0]["token"]
        os.kill(read_connector_report(layout)["connectors"][0]["pid"], signal.SIGKILL)
        assert _wait_for(lambda: len([r for r in _records(report) if r.get("token")]) == 2, timeout=20)
        second = [r for r in _records(report) if r.get("token")][1]["token"]
        assert second != first
        assert colony.authenticate(first) is None
        assert colony.authenticate(second).generation == 2
    finally:
        sup.stop()
    assert colony.authenticate(second) is None


def test_stop_waits_for_the_child_and_says_when_it_cannot_confirm(layout, colony, monkeypatch):
    report = layout.wolts_dir / "child-report.jsonl"
    plan = _child_plan(TelegramRuntimeConnector().plan(layout, ENV), report, colony, CHILD_EXIT_DELAY="0.4")
    sup = ChannelSupervisor(layout, [plan])
    sup.start()
    assert _wait_for(lambda: _records(report))
    pid = read_connector_report(layout)["connectors"][0]["pid"]
    started = time.monotonic()
    sup.stop()
    assert time.monotonic() - started >= 0.4
    assert _records(report)[-1] == {"exit": "clean"}
    assert read_connector_report(layout)["connectors"][0]["shutdown"] == "confirmed"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    # An exit that cannot be confirmed is reported as such, not as stopped.
    plan = _child_plan(TelegramRuntimeConnector().plan(layout, ENV), report, colony)
    sup = ChannelSupervisor(layout, [plan])
    sup.start()
    assert _wait_for(lambda: len([r for r in _records(report) if r.get("token")]) == 2)
    child = next(iter(sup._children.values()))
    monkeypatch.setattr(child, "wait", lambda timeout=None: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", timeout)))
    sup.stop(timeout=0.5)
    state = read_connector_report(layout)["connectors"][0]
    assert state["shutdown"] == "unknown" and state["state"] == "failed"
    assert "run lock may still be held" in state["error"]
    assert _live(colony) == []


# -------------------------------------------------------- failure cleanup


def test_every_post_mint_failure_revokes_that_generation(layout, colony, tmp_path, monkeypatch):
    """Finding 1: mkdir, open, log, Popen, stillborn: no live or pending token survives."""
    # prepare: the run dir is a file.
    blocked = tmp_path / "blocked"
    blocked.write_text("file")
    handoff = ConnectorHandoff(lambda: colony, "tg", blocked)
    with pytest.raises(OSError):
        handoff.prepare()
    assert _live(colony) == [] and colony.token_state("tg", 1) == "revoked"
    # prepare: the directory is fine but the file cannot be written out.
    with monkeypatch.context() as patched:
        patched.setattr(handoff_module.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk full")))
        handoff = ConnectorHandoff(lambda: colony, "tg", tmp_path / "run")
        with pytest.raises(OSError):
            handoff.prepare()
    assert _live(colony) == [] and list((tmp_path / "run").glob("handoff-*.json")) == []
    # The log cannot be opened.
    plan = TelegramRuntimeConnector().plan(layout, ENV)
    layout.logs_dir.mkdir(parents=True, exist_ok=True)
    (layout.logs_dir / "connector-telegram-connector.log").mkdir()
    sup = ChannelSupervisor(layout, [plan])
    sup.start()
    state = read_connector_report(layout)["connectors"][0]
    assert state["state"] == "failed" and "cannot open" in state["error"]
    sup.stop()
    assert _live(colony) == [] and list((layout.platform_state / "run").glob("handoff-*.json")) == []
    (layout.logs_dir / "connector-telegram-connector.log").rmdir()
    # Popen fails: no such command.
    broken = ConnectorPlan(
        plan.name, True, plan.detail, ("/no/such/interpreter", "-m", "x"), plan.cwd, plan.env,
        plan.remedy, plan.process_signature, isolated_env=True, handoff=plan.handoff,
    )
    sup = ChannelSupervisor(layout, [broken])
    sup.start()
    assert read_connector_report(layout)["connectors"][0]["state"] == "failed"
    sup.stop()
    assert _live(colony) == [] and list((layout.platform_state / "run").glob("handoff-*.json")) == []
    # Stillborn: stop() lands between the spawn and its registration.
    sleeper = ConnectorPlan(
        plan.name, True, plan.detail, (sys.executable, "-c", "import time; time.sleep(30)"), plan.cwd,
        plan.env, plan.remedy, plan.process_signature, isolated_env=True, handoff=plan.handoff,
    )
    holder = {}

    def popen_then_stop(*args, **kwargs):
        child = subprocess.Popen(*args, **kwargs)
        holder["sup"]._stopping.set()
        return child

    sup = ChannelSupervisor(layout, [sleeper], popen=popen_then_stop)
    holder["sup"] = sup
    sup.start(watch=False)
    state = read_connector_report(layout)["connectors"][0]
    assert state["state"] == "stopped" and state["pid"] is None
    sup.stop()
    assert _live(colony) == [] and list((layout.platform_state / "run").glob("handoff-*.json")) == []
    # Every failure revoked its own generation only: the registry shows them all revoked, none pending.
    assert all(t["revoked"] is not None for t in colony._registry()["tokens"])


def test_stale_sweep_revokes_the_pending_generation_not_just_the_file(layout, colony):
    """Astre's probe: deletion is not revocation."""
    clock = [1000.0]
    store = ConnectorStore(colony.root, clock=lambda: clock[0])
    handoff = ConnectorHandoff(lambda: store, "tg", layout.platform_state / "run", clock=lambda: clock[0])
    env = handoff.prepare()
    path = Path(env["WOLTSPACE_CONNECTOR_TOKEN_FILE"])
    token = json.loads(path.read_text())["token"]
    os.utime(path, (1, 1))
    clock[0] += handoff_module.STALE_AFTER + 1
    fresh = ConnectorHandoff(lambda: store, "tg", layout.platform_state / "run", clock=lambda: clock[0])
    assert fresh.sweep_stale() == 1
    assert store.token_state("tg", 1) == "revoked" and store.authenticate(token) is None
    # A newer generation, minted by another launch, is untouched by an old one's cleanup.
    old = ConnectorHandoff(lambda: store, "tg", layout.platform_state / "run", clock=lambda: clock[0])
    old.prepare()                                           # generation 2
    new = ConnectorHandoff(lambda: store, "tg", layout.platform_state / "run", clock=lambda: clock[0])
    newer = new.prepare()                                   # generation 3; the mint revoked 2
    assert store.token_state("tg", 3) == "pending"
    old.abandon()                                           # cleans up its own file and generation only
    assert store.token_state("tg", 3) == "pending"
    assert json.loads(Path(newer["WOLTSPACE_CONNECTOR_TOKEN_FILE"]).read_text())["generation"] == 3
    assert old.status()["unrevoked"] == {}


def test_a_failed_revocation_is_kept_visible_and_retried(layout, colony, monkeypatch):
    handoff = ConnectorHandoff(lambda: colony, "tg", layout.platform_state / "run")
    handoff.prepare()
    broken = [True]
    real = ConnectorStore.revoke_generation

    def flaky(self, instance_id, generation):
        if broken[0]:
            raise OSError("registry on fire")
        return real(self, instance_id, generation)

    monkeypatch.setattr(ConnectorStore, "revoke_generation", flaky)
    handoff.abandon()
    assert handoff.status()["unrevoked"] == {"1": "registry on fire"}
    assert colony.token_state("tg", 1) == "pending"     # still owed
    handoff.check()
    assert handoff.status()["unrevoked"] == {"1": "registry on fire"}
    broken[0] = False
    handoff.check()
    assert handoff.status()["unrevoked"] == {} and colony.token_state("tg", 1) == "revoked"


def test_a_handoff_taken_but_never_acknowledged_fails_closed(layout, colony, monkeypatch):
    """The file is gone, the generation was never activated: not a success."""
    monkeypatch.setattr(handoff_module, "CONSUME_TIMEOUT", 0.3)
    monkeypatch.setattr(handoff_module, "STALE_AFTER", 0.3)
    report = layout.wolts_dir / "child-report.jsonl"
    plan = _child_plan(TelegramRuntimeConnector().plan(layout, ENV), report, colony, CHILD_ACTIVATE="0")
    sup = ChannelSupervisor(layout, [plan], sleep=lambda _s: None)
    sup.start()
    try:
        assert _wait_for(lambda: _records(report))
        minted = _records(report)[0]["token"]
        assert colony.token_state("tg", 1) == "pending"
        time.sleep(0.35)
        sup.poll_once()
        assert colony.token_state("tg", 1) == "revoked" and colony.authenticate(minted) is None
        state = read_connector_report(layout)["connectors"][0]
        assert state["restarts"] == 1
    finally:
        sup.stop()
    assert _live(colony) == []


def test_a_handoff_not_consumed_in_time_fails_closed(layout, colony, monkeypatch):
    monkeypatch.setattr(handoff_module, "CONSUME_TIMEOUT", 0.3)
    report = layout.wolts_dir / "child-report.jsonl"
    plan = _child_plan(TelegramRuntimeConnector().plan(layout, ENV), report, colony, CHILD_CONSUME="0")
    sup = ChannelSupervisor(layout, [plan], sleep=lambda _s: None)
    sup.start()
    try:
        assert _wait_for(lambda: _records(report))
        [pending] = list((layout.platform_state / "run").glob("handoff-*.json"))
        assert colony.token_state("tg", 1) == "pending"
        time.sleep(0.35)
        sup.poll_once()
        assert not pending.exists() and colony.token_state("tg", 1) == "revoked"
        assert read_connector_report(layout)["connectors"][0]["restarts"] == 1
    finally:
        sup.stop()


def test_a_child_that_dies_before_consuming_leaves_no_live_token(layout, colony):
    plan = TelegramRuntimeConnector().plan(layout, ENV)
    crasher = ConnectorPlan(
        plan.name, True, plan.detail, (sys.executable, "-c", "import sys; sys.exit(3)"), plan.cwd,
        plan.env, plan.remedy, plan.process_signature, isolated_env=True, handoff=plan.handoff,
    )
    sup = ChannelSupervisor(layout, [crasher], sleep=lambda _s: None, max_restarts=1)
    sup.start()
    try:
        assert _wait_for(lambda: (sup.poll_once(), read_connector_report(layout)["connectors"][0]["state"] == "failed")[1])
    finally:
        sup.stop()
    assert _live(colony) == [] and list((layout.platform_state / "run").glob("handoff-*.json")) == []


# ------------------------------------------------------ the runner's read


def _envelope(tmp_path, **override) -> tuple[Path, dict]:
    run = tmp_path / "run"
    run.mkdir(mode=0o700, exist_ok=True)
    path = run / "handoff-x.json"
    data = {"token": "wct_x", "lodge_id": "L", "instance_id": "tg", "generation": 3, "issued": 1000.0, **override}
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path, {"lodge_id": "L", "instance_id": "tg", "generation": 3}


def test_the_runner_validates_the_envelope_against_what_it_was_told(tmp_path):
    path, expected = _envelope(tmp_path)
    assert read_handoff(str(path), expected=expected, window=10, now=1005.0)["token"] == "wct_x"
    assert not path.exists()
    for change, match in (
        ({"lodge_id": "other"}, "lodge_id"),
        ({"instance_id": "wrong"}, "instance_id"),
        ({"generation": -1}, "generation"),
        ({"generation": 4}, "generation"),
        ({"issued": 900.0}, "stale"),
        ({"issued": "yesterday"}, "stale"),
        ({"token": ""}, "no token"),
    ):
        path, expected = _envelope(tmp_path, **change)
        with pytest.raises(SystemExit, match=match):
            read_handoff(str(path), expected=expected, window=10, now=1005.0)
        # Claimed then refused: gone either way, and the lodge's check sees
        # "taken but never activated" and revokes.
        assert not path.exists() and list((tmp_path / "run").iterdir()) == [], change
    with pytest.raises(SystemExit, match="already consumed"):
        read_handoff(str(tmp_path / "run" / "missing.json"), expected=expected, window=10, now=1005.0)


def test_the_runner_opens_the_handoff_anchored_in_a_private_directory(tmp_path):
    path, expected = _envelope(tmp_path)
    # A symlinked parent directory is refused.
    link_dir = tmp_path / "link-run"
    link_dir.symlink_to(tmp_path / "run")
    with pytest.raises(SystemExit, match="handoff directory"):
        read_handoff(str(link_dir / "handoff-x.json"), expected=expected, window=10, now=1005.0)
    # A directory others can read is refused.
    (tmp_path / "run").chmod(0o755)
    with pytest.raises(SystemExit, match="not private"):
        read_handoff(str(path), expected=expected, window=10, now=1005.0)
    (tmp_path / "run").chmod(0o700)
    # A FIFO in the file's place cannot hang the read: the non-blocking open and the regular check refuse it.
    fifo = tmp_path / "run" / "handoff-fifo.json"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(SystemExit, match="not a regular file"):
        read_handoff(str(fifo), expected=expected, window=10, now=1005.0)
    # A symlinked leaf is refused (claimed, then the no-follow open fails), and the target is untouched.
    leaf = tmp_path / "run" / "handoff-link.json"
    leaf.symlink_to(path)
    with pytest.raises(SystemExit):
        read_handoff(str(leaf), expected=expected, window=10, now=1005.0)
    assert path.exists() and not leaf.exists()
    # A file others can read is refused; a private one is taken.
    path.chmod(0o640)
    with pytest.raises(SystemExit, match="not private"):
        read_handoff(str(path), expected=expected, window=10, now=1005.0)
    path, expected = _envelope(tmp_path)
    assert read_handoff(str(path), expected=expected, window=10, now=1005.0)["token"] == "wct_x"


def test_two_concurrent_consumers_exactly_one_wins(tmp_path):
    path, expected = _envelope(tmp_path)
    barrier = threading.Barrier(2)
    outcomes = []

    def consume():
        barrier.wait()
        try:
            read_handoff(str(path), expected=expected, window=10, now=1005.0)
            outcomes.append("won")
        except SystemExit:
            outcomes.append("lost")
        except BaseException as exc:  # noqa: BLE001 - a thread must report, never vanish
            outcomes.append(("error", repr(exc)))

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["lost", "won"] and not path.exists()
