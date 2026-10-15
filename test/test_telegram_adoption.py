"""The connector is Telegram's default: an adapter-era setup is carried over once, at start."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from server.connector_commands import allowed_wolts  # noqa: E402
from server.connectors import ConnectorStore  # noqa: E402
from woltspace.channels import TelegramRuntimeConnector, adopt_telegram  # noqa: E402
from woltspace.layout import RuntimeLayout  # noqa: E402

TOKEN = "123456:configured-telegram-token"


@pytest.fixture
def layout(tmp_path):
    wolts = tmp_path / "wolts"
    for name, kind in (("softie", "raccoon"), ("astre", "otter"), ("rex", "dog")):
        (wolts / name / "wolt").mkdir(parents=True)
        (wolts / name / "wolt" / "wolt.json").write_text(json.dumps({"name": name, "type": kind}))
    return RuntimeLayout(wolts_dir=wolts, install_root=ROOT, host="127.0.0.1", port=7799, isolation="host")


def _configure(layout, **telegram):
    layout.platform_state.mkdir(parents=True, exist_ok=True)
    (layout.platform_state / "config.json").write_text(
        json.dumps({"channels": {"telegram": {"enabled": True, "token": TOKEN, **telegram}}})
    )


def _store(layout):
    return ConnectorStore(layout.wolts_dir / ".space" / "connectors")


def test_an_adapter_setup_becomes_an_owner_binding_per_allowed_user(layout):
    _configure(layout)
    chats = layout.wolts_dir / ".space" / "telegram" / "chats"
    chats.mkdir(parents=True)
    (chats / "101.json").write_text(json.dumps({"active_wolt": "astre", "active_session": "astre-calm-owl-1a2b3c"}))
    said = adopt_telegram(layout, {"TELEGRAM_ALLOWED_USERS": "101, 202", "PATH": "/usr/bin"})
    assert "bot 123456" in said and "101, 202" in said
    store = _store(layout)
    [instance] = store.list_instances()
    assert (instance["instance_id"], instance["channel"], instance["account"], instance["enabled"]) == (
        "telegram", "telegram", "123456", True,
    )
    bindings = {b["sender"]: b for b in store.list_bindings("telegram")}
    owner = bindings["101"]
    assert owner["conversation"] == "101"            # their own chat, not groups
    assert owner["default_wolt"] == "astre"          # where the adapter left the chat
    assert all(owner[g] for g in ("may_wake", "any_wolt", "may_create", "may_stop_own_sessions", "may_manage_apps", "may_transcribe"))
    assert store.routing_state("telegram", owner["binding_id"])["active_session"] == "astre-calm-owl-1a2b3c"
    # No saved chat: the first chat wolt.
    assert bindings["202"]["default_wolt"] == "astre"
    # Once: a second start changes nothing.
    assert adopt_telegram(layout, {"TELEGRAM_ALLOWED_USERS": "101,202,303"}) is None
    assert len(store.list_bindings("telegram")) == 2
    # And the connector now has what it needs to launch.
    assert TelegramRuntimeConnector().plan(layout, {"PATH": "/usr/bin"}).enabled is True


def test_any_wolt_is_every_chat_wolt_now_and_later_never_the_dog(layout):
    _configure(layout, allowed_users="101")
    adopt_telegram(layout, {})
    store = _store(layout)
    [binding] = store.list_bindings("telegram")
    assert allowed_wolts(store.binding_now(binding["binding_id"])) == {"softie", "astre"}
    (layout.wolts_dir / "newt" / "wolt").mkdir(parents=True)
    (layout.wolts_dir / "newt" / "wolt" / "wolt.json").write_text(json.dumps({"type": "beaver"}))
    assert "newt" in allowed_wolts(store.binding_now(binding["binding_id"]))


@pytest.mark.parametrize("case", ["adapter_chosen", "disabled", "ambient_only", "bad_token", "already_there"])
def test_nothing_is_carried_over_unless_the_adapter_would_have_run_here(layout, case):
    env = {"TELEGRAM_ALLOWED_USERS": "101"}
    if case == "adapter_chosen":
        _configure(layout)
        (layout.wolts_dir / "woltspace.json").write_text(json.dumps({"telegram": {"runtime": "adapter"}}))
    elif case == "disabled":
        _configure(layout, enabled=False)
    elif case == "ambient_only":
        # Inherited environment alone, not the entrypoint: a stray serve must not take the bot.
        env.update({"ENABLE_TELEGRAM_BOT": "true", "TELEGRAM_BOT_TOKEN": TOKEN})
    elif case == "bad_token":
        _configure(layout, token="not-a-token")
    else:
        _configure(layout)
        _store(layout).add_instance("mine", "telegram", "999")
    assert adopt_telegram(layout, env) is None
    instances = _store(layout).list_instances()
    assert [i["instance_id"] for i in instances] == (["mine"] if case == "already_there" else [])


def test_the_entrypoint_may_adopt_from_its_environment(layout):
    env = {"ENABLE_TELEGRAM_BOT": "true", "TELEGRAM_BOT_TOKEN": TOKEN, "WOLTSPACE_ENTRYPOINT": "1",
           "TELEGRAM_ALLOWED_USERS": "101"}
    assert adopt_telegram(layout, env) is not None
    assert [b["sender"] for b in _store(layout).list_bindings("telegram")] == ["101"]


def test_a_lodge_set_up_before_its_first_wolt_is_adopted_once_one_exists(tmp_path):
    """Astre's rc1 review: no wolt yet must not leave an enabled instance with nobody bound."""
    layout = RuntimeLayout(wolts_dir=tmp_path / "wolts", install_root=ROOT, host="127.0.0.1", port=7799, isolation="host")
    _configure(layout, allowed_users="101")
    assert adopt_telegram(layout, {}) is None
    assert _store(layout).list_instances() == []          # nothing written yet
    (layout.wolts_dir / "softie" / "wolt").mkdir(parents=True)
    (layout.wolts_dir / "softie" / "wolt" / "wolt.json").write_text(json.dumps({"type": "raccoon"}))
    assert adopt_telegram(layout, {}) is not None
    store = _store(layout)
    assert [b["sender"] for b in store.list_bindings("telegram")] == ["101"]
    assert store.list_instances()[0]["enabled"] is True


def test_no_allowed_user_writes_nothing(layout):
    _configure(layout)
    assert adopt_telegram(layout, {}) is None
    assert _store(layout).list_instances() == []


@pytest.mark.parametrize("step", ["bind_first", "bind_second", "remember", "enable", "finish"])
def test_an_interrupted_adoption_resumes_at_the_next_start(layout, monkeypatch, step):
    _configure(layout, allowed_users=["101", "202"])
    real = {name: getattr(ConnectorStore, name) for name in ("bind", "remember_route", "enable", "finish_adoption")}
    calls = {"bind": 0}

    def failing(name):
        def wrapper(self, *args, **kwargs):
            if name == "bind":
                calls["bind"] += 1
                if (step == "bind_first" and calls["bind"] == 1) or (step == "bind_second" and calls["bind"] == 2):
                    raise OSError("injected interruption")
            elif step == {"remember_route": "remember", "enable": "enable", "finish_adoption": "finish"}[name]:
                raise OSError("injected interruption")
            return real[name](self, *args, **kwargs)
        return wrapper

    for name in real:
        monkeypatch.setattr(ConnectorStore, name, failing(name))
    first = adopt_telegram(layout, {})
    assert "next start resumes" in first
    store = _store(layout)
    [instance] = store.list_instances()
    assert instance["adoption"] == "pending"
    for name, fn in real.items():
        monkeypatch.setattr(ConnectorStore, name, fn)
    assert "owner chats: 101, 202" in adopt_telegram(layout, {})
    [instance] = store.list_instances()
    assert (instance["adoption"], instance["enabled"]) == ("done", True)
    assert sorted(b["sender"] for b in store.list_bindings("telegram")) == ["101", "202"]
    # Done is done: later starts change nothing.
    assert adopt_telegram(layout, {"TELEGRAM_ALLOWED_USERS": "101,202,303"}) is None


def test_resuming_never_rebinds_a_user_it_already_bound(layout, monkeypatch):
    _configure(layout, allowed_users=["101", "202"])
    real_bind = ConnectorStore.bind

    def second_fails(self, *args, **kwargs):
        if kwargs.get("sender") == "202":
            raise OSError("injected interruption")
        return real_bind(self, *args, **kwargs)

    monkeypatch.setattr(ConnectorStore, "bind", second_fails)
    adopt_telegram(layout, {})
    monkeypatch.setattr(ConnectorStore, "bind", real_bind)
    store = _store(layout)
    # The owner narrows the first chat before the next start.
    store.bind("telegram", sender="101", conversation="101", default_wolt="softie")
    adopt_telegram(layout, {})
    owner = next(b for b in store.list_bindings("telegram") if b["sender"] == "101")
    assert (owner["default_wolt"], owner["may_create"], owner["any_wolt"]) == ("softie", False, False)
