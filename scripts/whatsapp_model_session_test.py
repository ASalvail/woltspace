#!/usr/bin/env python3
"""Owner-operated real WhatsApp → model session → notify test.

This is deliberately separate from whatsapp_owner_test.py. It is guarded by
an interactive confirmation and uses a fresh private data root, loopback-only
uvicorn socket, and private tmux socket. Do not launch before review.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_TMUX_BASE = ROOT.parents[2] / ".whatsapp-model-tmux"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import scripts.whatsapp_owner_test as owner_test

TEST_WOLT = "wa-test"
TIMEOUT_SECONDS = 300
DISPATCH_WAIT_SECONDS = 45
_PATHS = (ROOT, ROOT / "container", ROOT / "container" / "lib", ROOT / "src")
_DIAGNOSTIC_STAGES = (
    "preflight", "server_health", "inbound_selection", "selection_reply",
    "challenge_dispatch", "session_spawn", "harness_boot", "session_ack",
    "notify", "cleanup",
)
_DIAGNOSTIC_STATUSES = {"pending", "passed", "failed", "not_reached"}
_NOTIFY_ATTEMPT_STATES = {
    "not_seen", "dispatch_wait", "reserved", "route_verified",
    "send_in_flight", "sent", "rejected", "ambiguous",
}
_PROCESS_STATES = {"not_checked", "present", "absent", "unknown"}
_READINESS_STATES = {
    "not_checked", "ready_marker_seen", "prompt_detected", "not_ready",
    "unknown", "unsupported",
}
_FAILURE_CATEGORIES = {
    "preflight_failed", "executable_missing", "version_probe_failed",
    "help_probe_failed", "bootstrap_missing", "server_start_failed",
    "selection_not_confirmed", "adapter_reply_failed", "session_not_started", "auth_or_config",
    "harness_boot_unconfirmed", "notify_rejected", "notify_ambiguous",
    "trust_or_approval_prompt", "waiting_for_input", "run_timeout",
    "directory_trust_prompt", "onboarding_prompt", "tool_approval_prompt",
    "cleanup_failed", "internal_error",
}


class RunDiagnostics:
    """Fixed-schema status only; never stores exception, prompt, or identity text."""

    def __init__(self, harness: str):
        self._lock = threading.RLock()
        self.harness = harness if harness in {"claude", "codex"} else "unknown"
        self.stage = "preflight"
        self.failure_stage = ""
        self.category = ""
        self.checks = {name: "pending" for name in _DIAGNOSTIC_STAGES}
        self.checks["cleanup"] = "not_reached"
        self.notify_attempt = "not_seen"
        self.harness_process = "not_checked"
        self.harness_readiness = "not_checked"
        self.boot_symptom = "not_checked"

    def enter(self, stage: str) -> None:
        with self._lock:
            if stage in _DIAGNOSTIC_STAGES:
                self.stage = stage

    def passed(self, stage: str) -> None:
        with self._lock:
            if stage in _DIAGNOSTIC_STAGES:
                self.checks[stage] = "passed"

    def set_check(self, stage: str, state: str) -> None:
        with self._lock:
            if stage in _DIAGNOSTIC_STAGES and state in _DIAGNOSTIC_STATUSES:
                self.checks[stage] = state

    def set_notify_attempt(self, state: str) -> None:
        with self._lock:
            if state in _NOTIFY_ATTEMPT_STATES:
                self.notify_attempt = state

    def set_harness_process(self, state: str) -> None:
        with self._lock:
            if state in _PROCESS_STATES:
                self.harness_process = state

    def set_harness_readiness(self, state: str) -> None:
        with self._lock:
            if state in _READINESS_STATES:
                self.harness_readiness = state

    def set_boot_symptom(self, state: str) -> None:
        with self._lock:
            if state in _BOOT_SYMPTOMS:
                self.boot_symptom = state

    def fail(self, category: str | None = None) -> None:
        with self._lock:
            if not self.failure_stage:
                self.failure_stage = self.stage
            if self.stage in _DIAGNOSTIC_STAGES:
                self.checks[self.stage] = "failed"
            if category in _FAILURE_CATEGORIES and not self.category:
                self.category = category
            elif not self.category:
                self.category = {
                    "preflight": "preflight_failed",
                    "server_health": "server_start_failed",
                    "inbound_selection": "selection_not_confirmed",
                    "selection_reply": "adapter_reply_failed",
                    "challenge_dispatch": "session_not_started",
                    "session_spawn": "session_not_started",
                    "harness_boot": "harness_boot_unconfirmed",
                    "session_ack": "internal_error",
                    "notify": "notify_rejected",
                    "cleanup": "cleanup_failed",
                }.get(self.stage, "internal_error")

    def as_record(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": 2,
                "harness": self.harness,
                "stage": self.failure_stage or self.stage,
                "category": self.category or "internal_error",
                "checks": {name: self.checks[name] for name in _DIAGNOSTIC_STAGES},
                "notify_attempt": self.notify_attempt,
                "harness_process": self.harness_process,
                "harness_readiness": self.harness_readiness,
                "boot_symptom": self.boot_symptom,
            }


_BOOT_SYMPTOMS = {
    "auth_or_config", "bootstrap_missing", "trust_or_approval",
    "directory_trust_prompt", "onboarding_prompt", "tool_approval_prompt",
    "waiting_for_input", "none_detected", "capture_unavailable",
}
_BOOT_PATTERNS = (
    ("auth_or_config", re.compile(
        r"not logged in|login required|run codex login|authentication required|"
        r"unauthorized|invalid config|failed to load config|api key required", re.I,
    )),
    ("bootstrap_missing", re.compile(
        r"command not found|no such file or directory|notify: not found|"
        r"skill.{0,40}(not found|unavailable)|unknown skill", re.I,
    )),
    ("trust_or_approval", re.compile(
        r"trust(ed)? .{0,60}(folder|workspace|project)|"
        r"(approve|approval).{0,60}(workspace|permission|request|command)|"
        r"permission.{0,60}(approval|allow|request)|onboard(ing)?|allow .{0,40}\?", re.I,
    )),
    ("waiting_for_input", re.compile(
        r"press (enter|return)|waiting for (input|your)|type .* to continue|"
        r"continue\?\s*$|waiting for approval", re.I | re.M,
    )),
)


def _classify_boot_symptom(text: str) -> str:
    """Map private pane text to one fixed category; never return source text."""
    # Require both a specific question/header and concrete interactive choices.
    # A mere occurrence of "trust"/"approval" is only an ambiguous symptom.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    choices = bool(re.search(
        r"(?im)^\s*(?:[>❯›]\s*)?\d+[.)]\s*(yes|no|proceed|exit|dark|light)\b|"
        r"(?im:press (enter|return) to (confirm|continue))", text,
    ))
    directory = choices and bool(re.search(
        r"do you trust (the contents of )?(this|the) (directory|folder|workspace|project)\??",
        text, re.I,
    ))
    tool = choices and bool(re.search(
        r"would you like to run the following command\??|approve (this|the following) command\??",
        text, re.I,
    ))
    onboarding = choices and bool(re.search(
        r"welcome to codex[\s\S]{0,300}(choose|select).{0,50}(theme|appearance)|"
        r"(choose|select).{0,50}(theme|appearance)[\s\S]{0,300}welcome to codex",
        text, re.I,
    ))
    specific = [name for name, match in (
        ("directory_trust_prompt", directory), ("tool_approval_prompt", tool),
        ("onboarding_prompt", onboarding),
    ) if match]
    if len(specific) == 1:
        return specific[0]
    if len(specific) > 1:
        return "trust_or_approval"
    for category, pattern in _BOOT_PATTERNS:
        if pattern.search(text):
            return category
    return "none_detected"


def _write_failure_record(run_root: Path, diagnostics: RunDiagnostics) -> Path:
    """Atomically store only the allowlisted status schema with mode 0600."""
    path = run_root / "failure.json"
    temp = run_root / ".failure.json.tmp"
    record = diagnostics.as_record()
    if not _valid_diagnostic_record(record):
        raise ValueError("diagnostic record failed its fixed-schema check")
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)
    return path


def _valid_diagnostic_record(record: Any) -> bool:
    """Validate the bounded versioned schema before it reaches disk."""
    def enum(value: Any, choices) -> bool:
        return isinstance(value, str) and value in choices

    return bool(
        isinstance(record, dict)
        and set(record) == {
            "schema", "harness", "stage", "category", "checks",
            "notify_attempt", "harness_process", "harness_readiness", "boot_symptom",
        }
        and record.get("schema") == 2
        and enum(record.get("harness"), {"claude", "codex", "unknown"})
        and enum(record.get("stage"), _DIAGNOSTIC_STAGES)
        and enum(record.get("category"), _FAILURE_CATEGORIES)
        and isinstance(record.get("checks"), dict)
        and set(record["checks"]) == set(_DIAGNOSTIC_STAGES)
        and all(enum(value, _DIAGNOSTIC_STATUSES) for value in record["checks"].values())
        and enum(record.get("notify_attempt"), _NOTIFY_ATTEMPT_STATES)
        and enum(record.get("harness_process"), _PROCESS_STATES)
        and enum(record.get("harness_readiness"), _READINESS_STATES)
        and enum(record.get("boot_symptom"), _BOOT_SYMPTOMS | {"not_checked"})
    )


def _remove_raw_log(run_root: Path) -> None:
    for path in run_root.rglob("*.log"):
        try:
            if not path.is_symlink() and path.is_file():
                path.unlink()
        except OSError:
            pass


def _diagnostic_line(diagnostics: RunDiagnostics) -> str:
    stage = diagnostics.failure_stage or diagnostics.stage
    category = diagnostics.category if diagnostics.category in _FAILURE_CATEGORIES else "internal_error"
    return f"Run stopped: stage={stage} category={category}."


def _track_adapter_replies(real_api, diagnostics: RunDiagnostics):
    """Track only success/failure of the two expected adapter control replies."""
    def tracked(method: str, path: str, payload: dict | None = None):
        body = ((payload or {}).get("text") or {}).get("body", "")
        if method == "POST" and path == "/messages" and body.startswith("Selected wa-test."):
            stage = "selection_reply"
        elif method == "POST" and path == "/messages" and body.startswith("Session started:"):
            stage = "session_ack"
        else:
            return real_api(method, path, payload)
        diagnostics.enter(stage)
        try:
            result = real_api(method, path, payload)
        except Exception:
            diagnostics.set_check(stage, "failed")
            if stage == "selection_reply":
                diagnostics.fail("adapter_reply_failed")
            raise
        if isinstance(result, tuple) and result and isinstance(result[0], int) and 200 <= result[0] < 300:
            diagnostics.passed(stage)
        else:
            diagnostics.set_check(stage, "failed")
            if stage == "selection_reply":
                diagnostics.fail("adapter_reply_failed")
        return result
    return tracked


def _capture_boot_symptom(env: dict[str, str], session: str, *, visible_only=False) -> str:
    """Classify the private tmux pane without returning or persisting its text."""
    try:
        args = ("capture-pane", "-p", "-t", session)
        if not visible_only:
            args += ("-S", "-80")
        status, text, _ = _tmux_value(env, *args)
    except Exception:
        return "capture_unavailable"
    if status:
        return "capture_unavailable"
    return _classify_boot_symptom(text)


def _handle_boot_symptom(diagnostics: RunDiagnostics, symptom: str) -> bool:
    """Record an allowlisted boot symptom; return true for actionable prompts."""
    diagnostics.set_boot_symptom(symptom)
    if symptom in {"auth_or_config", "bootstrap_missing"}:
        diagnostics.set_harness_readiness("prompt_detected")
        diagnostics.enter("harness_boot")
        diagnostics.fail(symptom)
        return True
    if symptom in {"trust_or_approval", "waiting_for_input", "directory_trust_prompt",
                   "onboarding_prompt", "tool_approval_prompt"}:
        diagnostics.set_harness_readiness("prompt_detected")
        diagnostics.enter("harness_boot")
        diagnostics.fail(
            "trust_or_approval_prompt" if symptom == "trust_or_approval" else symptom,
        )
        return True
    return False


def _harness_readiness_status(session: str, harness: str) -> str:
    """Report configured TUI readiness evidence, never process presence as readiness."""
    try:
        from harnesses import get_harness
        marker = get_harness(harness).get("tui_ready_marker")
    except Exception:
        return "unknown"
    # Codex currently has no documented, versioned ready marker. Do not infer
    # model readiness from a live process or a raw terminal.
    if not marker:
        return "unsupported"
    try:
        from sessions import SessionRegistry
        record = SessionRegistry().get(session, check_alive=False)
        if not record:
            return "unknown"
        pane = _capture_boot_symptom_text(record)
    except Exception:
        return "unknown"
    if pane is None:
        return "unknown"
    return "ready_marker_seen" if marker in pane else "not_ready"


def _capture_boot_symptom_text(record: dict) -> str | None:
    """Capture a pane transiently for readiness matching; caller never persists text."""
    try:
        from session_runtime import get_runtime
        return get_runtime().capture(record, start="-80")
    except Exception:
        return None


def _harness_process_status(session: str, harness: str) -> bool | None:
    """Probe the actual harness, excluding launching shims; never return argv."""
    try:
        from sessions import session_has_agent_process
        return session_has_agent_process(session, harness=harness, include_launching=False)
    except Exception:
        return None


def _say(message: str) -> None:
    """Write only fixed, credential-free progress messages to the terminal."""
    os.write(1, (message + "\n").encode("utf-8", errors="replace"))


def _auth_path(harness: str, inherited: dict[str, str]) -> tuple[str, Path]:
    """Select an existing supported login path without opening its contents."""
    variable, default_dir = {
        "claude": ("CLAUDE_CONFIG_DIR", ".claude"),
        "codex": ("CODEX_HOME", ".codex"),
    }[harness]
    configured = inherited.get(variable, "").strip()
    home = Path(inherited.get("HOME") or Path.home())
    selected = Path(configured).expanduser() if configured else home / default_dir
    if not selected.is_dir():
        raise RuntimeError("the selected harness has no existing authorized login directory")
    return variable, selected.resolve()


def _prepare_private_tmux_dir(run_root: Path, base: Path | None = None) -> Path:
    """Create a short, owned per-run socket dir and validate Linux sun_path."""
    base = base or PRIVATE_TMUX_BASE
    uid = os.getuid()
    for directory in (base,):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != uid):
            raise RuntimeError("private tmux directory ownership check failed")
        os.chmod(directory, 0o700)
    tmux_tmp = base / run_root.name
    try:
        tmux_tmp.mkdir(mode=0o700)
    except FileExistsError:
        raise RuntimeError("private tmux run directory already exists") from None
    info = tmux_tmp.lstat()
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("private tmux run directory ownership check failed")
    socket_path = tmux_tmp / f"tmux-{uid}" / "default"
    # Linux sockaddr_un.sun_path reserves 108 bytes including the NUL.
    if len(os.fsencode(socket_path)) >= 108:
        shutil.rmtree(tmux_tmp)
        raise RuntimeError("private tmux socket path exceeds Linux pathname limit")
    return tmux_tmp


def _private_environment(
    run_root: Path, wolts_dir: Path, harness: str, auth_variable: str,
    auth_path: Path, host: str, port: int, inherited: dict[str, str],
    tmux_tmp: Path,
) -> dict[str, str]:
    """Build a narrow process env; never copy owner API tokens into it."""
    home = run_root / "home"
    xdg = run_root / "xdg"
    temp = run_root / "tmp"
    for directory in (home, xdg, xdg / "config", xdg / "data", xdg / "state",
                      xdg / "cache", tmux_tmp, temp):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)

    tmux = shutil.which("tmux", path=inherited.get("PATH"))
    if not tmux:
        raise RuntimeError("tmux is unavailable on PATH")
    env = {
        "PATH": inherited.get("PATH", os.defpath),
        "HOME": str(home),
        "TMPDIR": str(temp),
        "TMUX_TMPDIR": str(tmux_tmp),
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_DATA_HOME": str(xdg / "data"),
        "XDG_STATE_HOME": str(xdg / "state"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "WOLTSPACE_DIR": str(ROOT),
        "WOLTSPACE_WOLTS_DIR": str(wolts_dir),
        "WOLTS_DIR": str(wolts_dir),
        "WOLTSPACE_WOLT_DIR": str(wolts_dir / TEST_WOLT),
        "WOLT_DIR": str(wolts_dir / TEST_WOLT),
        "WOLTSPACE_WOLT_NAME": TEST_WOLT,
        "WOLT_NAME": TEST_WOLT,
        "WOLTSPACE_ISOLATION": "host",
        "WOLTSPACE_HOST": host,
        "WOLTSPACE_PORT": str(port),
        "PORT": str(port),
        "WOLTSPACE_API": f"http://{host}:{port}",
        "WOLTSPACE_PYTHON": sys.executable,
        "WOLTSPACE_TMUX_BIN": tmux,
        "WOLTSPACE_DEFAULT_HARNESS": harness,
        "BOT_ADAPTER": "whatsapp",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in ("LANG", "LC_ALL"):
        if inherited.get(key):
            env[key] = inherited[key]
    if auth_variable == "CODEX_HOME":
        # Keep Codex rollouts and config writes inside this run. The preflight
        # adds only an opaque symlink to the owner's existing auth.json.
        env[auth_variable] = str(run_root / "codex-home")
    else:
        env[auth_variable] = str(auth_path)
    # Never pass terminal multiplexing state from the owner's current shell.
    for key in ("TMUX", "TMUX_PANE", "WHATSAPP_AGENT_TOKEN"):
        env.pop(key, None)
    return env


def _preflight_harness(
    harness: str, env: dict[str, str], auth_path: Path, wolt_dir: Path,
    diagnostics: RunDiagnostics,
) -> None:
    """Check offline CLI, launch wrapper, notify path and seeded skills."""
    diagnostics.enter("preflight")
    if not auth_path.is_dir() or not os.access(auth_path, os.R_OK | os.X_OK):
        diagnostics.fail("preflight_failed")
        raise RuntimeError("selected harness login directory is unavailable")
    if (harness == "codex" and (
            Path(env.get("CODEX_HOME", "")).resolve() == auth_path.resolve()
            or env.get("WOLTSPACE_ISOLATION") != "host")):
        diagnostics.fail("preflight_failed")
        raise RuntimeError("isolated Codex and existing login paths are not separated")
    if harness == "codex":
        source_auth = auth_path / "auth.json"
        private_codex_home = Path(env["CODEX_HOME"])
        try:
            source_auth_stat = source_auth.stat()
        except OSError:
            diagnostics.fail("auth_or_config")
            raise RuntimeError("Codex auth file is unavailable") from None
        if not stat.S_ISREG(source_auth_stat.st_mode) or not os.access(source_auth, os.R_OK):
            diagnostics.fail("auth_or_config")
            raise RuntimeError("Codex auth file is unavailable")
        if not private_codex_home.resolve().is_relative_to(
                (wolt_dir.parent.parent).resolve()):
            diagnostics.fail("preflight_failed")
            raise RuntimeError("private Codex state is outside the test root")
        private_codex_home.mkdir(mode=0o700)
        os.chmod(private_codex_home, 0o700)
        (private_codex_home / "auth.json").symlink_to(source_auth.resolve())

    old_path = list(sys.path)
    sys.path[:0] = [str(p) for p in _PATHS]
    try:
        from harnesses import get_harness
        from skills_sync import seed_wolt_skills

        entry = get_harness(harness)
        wrapper = Path(entry["wrapper"])
        if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
            diagnostics.fail("executable_missing")
            raise RuntimeError("selected harness wrapper is unavailable")

        bin_dir = ROOT / "container" / "bin"
        required = (bin_dir / "run-session.sh", bin_dir / "session-reg", bin_dir / "notify")
        if (any(not p.is_file() or not os.access(p, os.X_OK) for p in required)
                or not shutil.which("bash", path=env.get("PATH"))):
            diagnostics.fail("bootstrap_missing")
            raise RuntimeError("session bootstrap executable is unavailable")
        session_path = os.pathsep.join((str(bin_dir), env.get("PATH", os.defpath)))
        notify_path = shutil.which("notify", path=session_path)
        if notify_path != str(bin_dir / "notify"):
            diagnostics.fail("bootstrap_missing")
            raise RuntimeError("notify is not resolvable from the session path")

        # Match start_session's platform-skill bootstrap against this private
        # synthetic wolt. It copies only checkout skills into the test root.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            seed_wolt_skills(ROOT, wolt_dir)
        skill_dir = wolt_dir / ".agents" / "skills"
        if not ((skill_dir / "woltspace-start-chat" / "SKILL.md").is_file()
                and (skill_dir / "woltspace-notify" / "SKILL.md").is_file()):
            diagnostics.fail("bootstrap_missing")
            raise RuntimeError("Codex platform skills were not prepared for the test wolt")

        binary = shutil.which(harness, path=env.get("PATH"))
        if not binary:
            diagnostics.fail("executable_missing")
            raise RuntimeError("selected harness executable is unavailable")
        # The preflight home is disposable and CODEX_HOME is deliberately
        # omitted: --version/--help must not inspect the owner's auth directory.
        safe_env = {
            key: env[key] for key in (
                "PATH", "HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_STATE_HOME", "XDG_CACHE_HOME", "LANG", "LC_ALL",
            ) if env.get(key)
        }
        safe_env.pop("WHATSAPP_AGENT_TOKEN", None)
        for args, category in ((("--version",), "version_probe_failed"),
                               (("--help",), "help_probe_failed")):
            try:
                result = subprocess.run(
                    [binary, *args], env=safe_env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=15, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                diagnostics.fail(category)
                raise RuntimeError("selected harness offline preflight failed") from None
            if result.returncode:
                diagnostics.fail(category)
                raise RuntimeError("selected harness offline preflight failed")
        diagnostics.passed("preflight")
    except RuntimeError:
        if not diagnostics.category:
            diagnostics.fail()
        raise
    except Exception:
        diagnostics.fail("bootstrap_missing")
        raise RuntimeError("isolated harness bootstrap preflight failed") from None
    finally:
        sys.path[:] = old_path


def _create_synthetic_wolt(wolts_dir: Path, harness: str) -> Path:
    wolt = wolts_dir / TEST_WOLT
    memory = wolt / "wolt" / "memory"
    memory.mkdir(mode=0o700, parents=True)
    (wolt / ".state").mkdir(mode=0o700)
    (memory / "identity.md").write_text(
        "# wa-test\n\nI am a temporary WhatsApp integration test wolt.\n",
        encoding="utf-8",
    )
    (memory / "context.md").write_text("# Context\n\nTemporary test only.\n", encoding="utf-8")
    (memory / "learnings.md").write_text("# Learnings\n\nNone.\n", encoding="utf-8")
    (wolt / "wolt" / "wolt.json").write_text(json.dumps({
        "name": TEST_WOLT,
        "type": "otter",
        "harness": harness,
        "description": "Temporary, isolated WhatsApp model-session test.",
    }, indent=2) + "\n", encoding="utf-8")
    (wolts_dir / "woltspace.json").write_text(json.dumps({
        "harness": {"default": harness},
        "channels": {
            "whatsapp": {"enabled": False},
            "telegram": {"enabled": False},
            "slack": {"enabled": False},
        },
    }, indent=2) + "\n", encoding="utf-8")
    for path in (wolt / "wolt" / "wolt.json", memory / "identity.md",
                 memory / "context.md", memory / "learnings.md",
                 wolts_dir / "woltspace.json"):
        os.chmod(path, 0o600)
    return wolt


def _bind_local_socket() -> tuple[socket.socket, int]:
    """Bind now and hand the bound socket directly to uvicorn (no port race)."""
    for _ in range(8):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        if port != 7777:
            listener.listen(128)
            listener.set_inheritable(False)
            return listener, port
        listener.close()
    raise RuntimeError("could not reserve an isolated non-7777 loopback port")


def _wait_for_server(
    base_url: str, expected_wolts: Path,
    thread: threading.Thread, server_errors: list[str],
) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server_errors or not thread.is_alive():
            raise RuntimeError("isolated local server did not start")
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as response:
                health = json.loads(response.read())
            if (health.get("ok") is True
                    and health.get("isolation") == "host"
                    and health.get("pid") == os.getpid()
                    and Path(health.get("wolts_dir", "/")).resolve() == expected_wolts.resolve()):
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("isolated local server did not pass its loopback health check")


def _tmux_value(env: dict[str, str], *args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [env["WOLTSPACE_TMUX_BIN"], *args], env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=5, check=False,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _verify_private_tmux(env: dict[str, str], tmux_root: Path, session: str) -> None:
    status, socket_path, _ = _tmux_value(env, "display-message", "-p", "#{socket_path}")
    if status or not socket_path:
        raise RuntimeError("could not verify the test tmux socket")
    try:
        Path(socket_path).resolve().relative_to(tmux_root.resolve())
    except ValueError:
        raise RuntimeError("tmux socket is outside the private socket directory") from None
    status, value, _ = _tmux_value(env, "show-environment", "-g", "WHATSAPP_AGENT_TOKEN")
    if status == 0 and value and not _is_unset_or_empty_token(value):
        raise RuntimeError("WhatsApp credential appeared in the private tmux server environment")
    status, value, _ = _tmux_value(env, "show-environment", "-t", session,
                                  "WHATSAPP_AGENT_TOKEN")
    if status == 0 and value and not _is_unset_or_empty_token(value):
        raise RuntimeError("WhatsApp credential appeared in the model session environment")


def _is_unset_or_empty_token(value: str) -> bool:
    return value.startswith("-WHATSAPP_AGENT_TOKEN") or value == "WHATSAPP_AGENT_TOKEN="


def _remove_stale_private_socket(tmux_root: Path) -> bool:
    """Remove only an owned socket node beneath this private per-run dir."""
    try:
        if tmux_root.is_symlink() or tmux_root.stat().st_uid != os.getuid():
            return False
        path = tmux_root / f"tmux-{os.getuid()}" / "default"
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return False
        path.unlink()
        return not path.exists()
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _kill_private_tmux(env: dict[str, str], tmux_root: Path) -> bool:
    try:
        status, socket_path, error = _tmux_value(
            env, "display-message", "-p", "#{socket_path}",
        )
    except Exception:
        return False
    if status:
        # Tmux's own explicit "no server running" response is the only
        # nonzero result that proves cleanup is already complete.
        return ("no server running" in error.lower()
                and _remove_stale_private_socket(tmux_root))
    try:
        Path(socket_path).resolve().relative_to(tmux_root.resolve())
    except ValueError:
        return False
    try:
        status, _, _ = _tmux_value(env, "kill-server")
    except Exception:
        return False
    if status:
        return False
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            check_status, _, check_error = _tmux_value(
                env, "display-message", "-p", "#{socket_path}",
            )
        except Exception:
            return False
        if check_status and "no server running" in check_error.lower():
            return _remove_stale_private_socket(tmux_root)
        time.sleep(0.05)
    return False


class OneNonceSendGate:
    """Allow one precisely formatted notify send after authoritative checks."""

    def __init__(self, expected_payload: str, verify, dispatch_done: threading.Event,
                 wait_seconds: float = DISPATCH_WAIT_SECONDS,
                 diagnostics: RunDiagnostics | None = None):
        self.expected_payload = expected_payload
        self.verify = verify
        self.dispatch_done = dispatch_done
        self.wait_seconds = wait_seconds
        self._lock = threading.Lock()
        self.status = "ready"
        self.verified_session = ""
        self.failure = ""
        self.diagnostics = diagnostics

    def _set_status(self, state: str) -> None:
        self.status = state
        if self.diagnostics is not None:
            self.diagnostics.set_notify_attempt(state)

    async def send(self, session: str, participant: str, message: str,
                   routing: dict | None, sender):
        # A model can notify while adapter.handle is still returning from
        # start_session and before it atomically saves adapter.ROUTES. Wait on
        # a thread event without blocking this server's asyncio loop or holding
        # the gate/phase locks. The dispatch thread always sets it in finally.
        with self._lock:
            if self.status != "ready":
                raise RuntimeError("the single WhatsApp test send is already reserved")
            self._set_status("dispatch_wait")
        dispatch_finished = await asyncio.to_thread(
            self.dispatch_done.wait, self.wait_seconds,
        )
        with self._lock:
            if self.status != "dispatch_wait":
                raise RuntimeError("the single WhatsApp test send is already reserved")
            # Reserve after bounded dispatch synchronization but before route
            # validation and definitely before any network I/O.
            self._set_status("reserved")
            if not dispatch_finished:
                self._set_status("rejected")
                self.failure = "the inbound dispatch did not finish in time"
                raise RuntimeError(self.failure)
            if message != self.expected_payload:
                self._set_status("rejected")
                self.failure = "the notify payload did not match the exact nonce"
                raise RuntimeError(self.failure)
            try:
                verified = self.verify(session, participant, routing)
            except Exception:
                verified = ""
            if not verified:
                self._set_status("rejected")
                self.failure = "the test session route was not verified"
                raise RuntimeError(self.failure)
            self.verified_session = verified
            self._set_status("route_verified")
        self._set_status("send_in_flight")
        try:
            result = await sender()
        except BaseException:
            with self._lock:
                self._set_status("ambiguous")
                self.failure = "the single WhatsApp send had an ambiguous outcome"
            raise
        with self._lock:
            self._set_status("sent")
        return result


def _cleanup_resources(
    server, server_thread, listener, session_name: str, env: dict[str, str],
    tmux_root: Path, *, stop_session_fn=None, kill_tmux_fn=None,
) -> bool:
    """Attempt every independent cleanup step and report aggregate certainty."""
    ok = True
    if server is not None:
        try:
            server.should_exit = True
        except Exception:
            ok = False
    if server_thread is not None:
        try:
            server_thread.join(timeout=10)
            if server_thread.is_alive():
                ok = False
        except Exception:
            ok = False
    if listener is not None:
        try:
            listener.close()
        except Exception:
            ok = False
    if session_name:
        try:
            if stop_session_fn is None:
                from sessions import stop_session as stop_session_fn
            result = stop_session_fn(session_name)
            if not isinstance(result, dict) or result.get("status") != "stopped":
                ok = False
        except Exception:
            ok = False
    # Do this even if session or API-server cleanup failed; each is independent.
    try:
        if kill_tmux_fn is None:
            kill_tmux_fn = _kill_private_tmux
        if not kill_tmux_fn(env, tmux_root):
            ok = False
    except Exception:
        ok = False
    return ok


def _finalize_run_root(run_root: Path, cleanup_ok: bool,
                       tmux_root: Path | None = None,
                       *, preserve_failure: bool = False, preserve_snapshot: bool = False) -> bool:
    """After verified cleanup keep only a sanitized failure record, if any."""
    if cleanup_ok:
        try:
            if tmux_root is not None:
                expected_parent = PRIVATE_TMUX_BASE.resolve()
                if (tmux_root.is_symlink()
                        or tmux_root.resolve().parent != expected_parent
                        or tmux_root.resolve().name != run_root.name):
                    raise RuntimeError("refusing to remove unexpected tmux directory")
                shutil.rmtree(tmux_root)
            if preserve_failure or preserve_snapshot:
                retained = ({"failure.json"} if preserve_failure else set())
                if preserve_snapshot:
                    retained.add("startup-snapshot.json")
                for name in retained:
                    record = run_root / name
                    if (record.is_symlink() or not record.is_file()
                            or record.stat().st_uid != os.getuid()
                            or stat.S_IMODE(record.stat().st_mode) != 0o600
                            or record.stat().st_size > 32768):
                        raise RuntimeError("private diagnostic artifact validation failed")
                for child in run_root.iterdir():
                    if child.name in retained:
                        continue
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink(missing_ok=True)
                return True
            shutil.rmtree(run_root)
            if not run_root.exists():
                return True
        except Exception:
            pass
    try:
        if run_root.exists():
            (run_root / ".cleanup-required").touch(mode=0o600, exist_ok=True)
    except Exception:
        pass
    return False


def _record_for_test(session: str, wolts_dir: Path) -> dict[str, Any] | None:
    from sessions import SessionRegistry
    return SessionRegistry(wolts_dir).get(session, wolt=TEST_WOLT, check_alive=False)


def _expected_notify_payload(nonce: str) -> str:
    # container/bin/notify prefixes the creature emoji and display name before
    # posting /notify. The synthetic wolt's creature is otter and name is fixed.
    return f"🦦 {TEST_WOLT}: {nonce}"


def _wrap_notify_send(real_send, gate: OneNonceSendGate):
    async def guarded_send(
        adapter_name: str, session: str, message: str, chat_id: str,
        thread_ts: str | None = None, routing: dict | None = None,
        attachments=(), speech=None, session_record: dict | None = None,
    ):
        if adapter_name != "whatsapp":
            return await real_send(
                adapter_name, session, message, chat_id, thread_ts, routing,
                attachments=attachments, speech=speech, session_record=session_record,
            )
        return await gate.send(
            session, str(chat_id), message, routing,
            lambda: real_send(
                adapter_name, session, message, chat_id, thread_ts, routing,
                attachments=attachments, speech=speech, session_record=session_record,
            ),
        )
    return guarded_send


def _confirmed_notify(gate: OneNonceSendGate, session: str,
                      diagnostics: RunDiagnostics) -> bool:
    if gate.status != "sent" or not session or gate.verified_session != session:
        return False
    diagnostics.enter("notify")
    diagnostics.passed("notify")
    return True


def _run_model_test(harness: str, diagnostics: RunDiagnostics | None = None,
                    *, startup_runner=None) -> int:
    diagnostics = diagnostics or RunDiagnostics(harness)
    owner_test._check_private_dir()
    inherited = {key: value for key, value in os.environ.items()
                 if key != "WHATSAPP_AGENT_TOKEN"}
    prefix = "startup-" if startup_runner is not None else "model-"
    run_root = Path(tempfile.mkdtemp(prefix=prefix, dir=owner_test.PRIVATE))
    secret = {"value": ""}
    tmux_root = None
    cleanup_ok = True
    old_env = os.environ.copy()
    old_env.pop("WHATSAPP_AGENT_TOKEN", None)
    old_path = list(sys.path)
    try:
        os.chmod(run_root, 0o700)
        diagnostics.enter("preflight")
        auth_variable, auth_path = _auth_path(harness, inherited)
        # Bound and validate the private tmux socket path before key access,
        # adapter initialization, or any API polling.
        tmux_root = _prepare_private_tmux_dir(run_root)
        wolts_dir = run_root / "wolts"
        wolts_dir.mkdir(mode=0o700)
        wolt_dir = _create_synthetic_wolt(wolts_dir, harness)
        preflight_env = _private_environment(
            run_root, wolts_dir, harness, auth_variable, auth_path,
            "127.0.0.1", 1, inherited, tmux_root,
        )
        _preflight_harness(harness, preflight_env, auth_path, wolt_dir, diagnostics)
        # The executable, host authentication path, skills, wrappers, and
        # notify command are checked before the private WhatsApp key is read.
        if startup_runner is not None:
            result = startup_runner(
                harness, inherited, auth_variable, auth_path, run_root, tmux_root,
                diagnostics,
            )
        else:
            secret["value"] = owner_test._read_token()
            result = _run_model_session(
                harness, inherited, auth_variable, auth_path, run_root, secret, tmux_root,
                diagnostics,
            )
        return 2 if diagnostics.category else result
    except BaseException:
        if not diagnostics.category:
            diagnostics.fail()
        raise
    finally:
        secret["value"] = ""
        os.environ.clear()
        os.environ.update(old_env)
        sys.path[:] = old_path
        if run_root.exists():
            _remove_raw_log(run_root)
            if diagnostics.category:
                try:
                    _write_failure_record(run_root, diagnostics)
                except OSError:
                    cleanup_ok = False
            cleanup_ok = cleanup_ok and diagnostics.checks["cleanup"] != "failed"
            finalized = _finalize_run_root(
                run_root, cleanup_ok, tmux_root,
                preserve_failure=bool(diagnostics.category) and cleanup_ok,
                preserve_snapshot=startup_runner is not None and bool(
                    getattr(diagnostics, "private_snapshot_written", False)),
            )
            if not finalized:
                diagnostics.enter("cleanup")
                diagnostics.fail("cleanup_failed")
                try:
                    _write_failure_record(run_root, diagnostics)
                except OSError:
                    pass


def _run_model_session(
    harness: str, inherited: dict[str, str], auth_variable: str,
    auth_path: Path, run_root: Path, secret: dict[str, str], tmux_root: Path,
    diagnostics: RunDiagnostics,
) -> int:
    wolts_dir = run_root / "wolts"
    wolt_dir = wolts_dir / TEST_WOLT
    listener, port = _bind_local_socket()
    host = "127.0.0.1"
    env = _private_environment(
        run_root, wolts_dir, harness, auth_variable, auth_path,
        host, port, inherited, tmux_root,
    )
    if "WHATSAPP_AGENT_TOKEN" in env:
        raise RuntimeError("refusing to start with WhatsApp credentials in child environment")

    old_env = os.environ.copy()
    old_env.pop("WHATSAPP_AGENT_TOKEN", None)
    old_path = list(sys.path)
    account = hashlib.sha256(secret["value"].encode()).hexdigest()[:20]
    session_name = ""
    adapter = None
    server = None
    server_thread = None
    server_errors: list[str] = []
    verified_session = ""
    send_gate = None
    run_log = run_root / "runner.log"
    fd = os.open(run_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    log_stream = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    os.environ.clear()
    os.environ.update(env)
    sys.path[:0] = [str(p) for p in _PATHS]
    try:
        with contextlib.redirect_stdout(log_stream), contextlib.redirect_stderr(log_stream):
            import uvicorn
            # Import the real control plane only after its environment points
            # at this private root. Lifespan is off: no tunnel, channel runner,
            # watchers, or lodge lifecycle hooks start in this test server.
            import server.app as server_app
            import server.notify as notify

            notify.dotenv_env = lambda name: secret["value"] if name == "WHATSAPP_AGENT_TOKEN" else ""
            config = uvicorn.Config(
                server_app.app, host=host, port=port, log_level="error",
                access_log=False, log_config=None, lifespan="off",
            )
            server = uvicorn.Server(config)

            def serve() -> None:
                try:
                    asyncio.run(server.serve(sockets=[listener]))
                except Exception as exc:
                    server_errors.append(type(exc).__name__)

            server_thread = threading.Thread(target=serve, name="wa-model-test-api", daemon=True)
            server_thread.start()
            diagnostics.enter("server_health")
            _wait_for_server(
                env["WOLTSPACE_API"], wolts_dir, server_thread, server_errors,
            )
            diagnostics.passed("server_health")

            # Import the real adapter against a placeholder so the actual key
            # never enters os.environ, even transiently.
            os.environ["WHATSAPP_AGENT_TOKEN"] = "model-test-import-placeholder"
            try:
                import bot.whatsapp_adapter as adapter_module
            finally:
                os.environ.pop("WHATSAPP_AGENT_TOKEN", None)
            adapter = adapter_module
            adapter.TOKEN = secret["value"]
            adapter.ACCOUNT = account
            adapter.STATE = run_root / "wolts" / ".space" / "whatsapp"
            adapter.STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
            adapter.CURSOR = adapter.STATE / f"cursor-{account}.json"
            adapter.ROUTES = adapter.STATE / f"routes-{account}.json"
            adapter.LOCK = adapter.STATE / f"poll-{account}.lock"
            adapter.api = _track_adapter_replies(adapter.api, diagnostics)
            activation = int(time.time())
            state = {"offset": 0, "activation": activation, "processed": []}
            adapter.atomic_json(adapter.CURSOR, state)
            routes = adapter.load(adapter.ROUTES)

            nonce = "wa-model-check-" + secrets.token_hex(6)
            challenge = f"Reply with exactly {nonce} and do nothing else."
            phase: dict[str, Any] = {"selected": False, "prompted": False,
                                     "participant": "", "unexpected": False}
            phase_lock = threading.RLock()
            dispatch_done = threading.Event()

            def verify_send_route(session: str, participant: str,
                                  routing: dict | None) -> str:
                nonlocal session_name
                with phase_lock:
                    if (not phase["selected"] or not phase["prompted"]
                            or participant != phase["participant"]):
                        return ""
                if not session or not isinstance(routing, dict):
                    return ""
                if (routing.get("adapter") != "whatsapp"
                        or routing.get("chat_id") != participant
                        or routing.get("whatsapp_account") != account
                        or routing.get("wolt") != TEST_WOLT):
                    return ""
                persisted_routes = adapter.load(adapter.ROUTES)
                route = persisted_routes.get(participant, {})
                if route.get("wolt") != TEST_WOLT or route.get("session") != session:
                    return ""
                record = _record_for_test(session, wolts_dir)
                if (not record or record.get("adapter") != "whatsapp"
                        or record.get("whatsapp_account") != account
                        or record.get("chat_id") != participant
                        or record.get("wolt") != TEST_WOLT):
                    return ""
                _verify_private_tmux(env, tmux_root, session)
                session_name = session
                return session

            send_gate = OneNonceSendGate(
                _expected_notify_payload(nonce), verify_send_route, dispatch_done,
                diagnostics=diagnostics,
            )
            notify._send = _wrap_notify_send(notify._send, send_gate)

            def dispatch_test_message(item: dict, current_routes: dict, started_at: int) -> None:
                text = (item.get("text") or {}).get("body", "")
                participant = item.get("from", "")
                if text.strip() == f"wolt {TEST_WOLT}":
                    diagnostics.enter("inbound_selection")
                    with phase_lock:
                        if phase["selected"]:
                            phase["unexpected"] = True
                            return
                        phase["participant"] = participant
                    adapter.handle(item, current_routes, started_at)
                    with phase_lock:
                        if current_routes.get(participant, {}).get("wolt") == TEST_WOLT:
                            phase["selected"] = True
                            diagnostics.passed("inbound_selection")
                        else:
                            phase["unexpected"] = True
                    return
                with phase_lock:
                    accepted_challenge = (
                        phase["selected"] and not phase["prompted"]
                        and participant == phase["participant"]
                        and text.strip() == challenge
                    )
                    if not accepted_challenge:
                        phase["unexpected"] = True
                        return
                    # Mark before dispatch: a thrown/ambiguous start must not
                    # allow another model session from a duplicate phrase.
                    phase["prompted"] = True
                diagnostics.enter("challenge_dispatch")
                try:
                    adapter.handle(item, current_routes, started_at)
                    route = current_routes.get(participant, {})
                    if route.get("session"):
                        diagnostics.passed("challenge_dispatch")
                        diagnostics.enter("session_spawn")
                    else:
                        diagnostics.fail("session_not_started")
                finally:
                    # The notify server may already be waiting for route
                    # persistence. Signal only after handle fully returns.
                    dispatch_done.set()

            _say("Isolated server is healthy on a private 127.0.0.1 port.")
            _say("In WhatsApp, send `wolt wa-test` and wait for the selection confirmation.")
            _say(f"Then send exactly: `{challenge}`")
            _say("The test sends no automatic retry after an ambiguous outbound timeout.")
            diagnostics.enter("inbound_selection")
            deadline = time.monotonic() + TIMEOUT_SECONDS
            delivered = False
            while time.monotonic() < deadline:
                state = adapter.poll_once(
                    state, routes, activation, cursor_path=adapter.CURSOR,
                    dispatch=dispatch_test_message,
                )
                routes = adapter.load(adapter.ROUTES)
                if diagnostics.category:
                    raise RuntimeError("an isolated startup or control reply check failed")
                if phase["unexpected"]:
                    raise RuntimeError("an unexpected WhatsApp message arrived; it was not dispatched")
                if send_gate.failure:
                    diagnostics.enter("notify")
                    diagnostics.fail(
                        "notify_ambiguous" if send_gate.status == "ambiguous"
                        else "notify_rejected",
                    )
                    raise RuntimeError("the single notify attempt was rejected or ambiguous")
                # Participant IDs are opaque; discover them only in memory.
                matching = next((route for route in routes.values()
                                 if route.get("wolt") == TEST_WOLT), None)
                if matching and matching.get("session"):
                    session_name = matching["session"]
                    if session_name != verified_session:
                        diagnostics.enter("session_spawn")
                        record = _record_for_test(session_name, wolts_dir)
                        if (not record or record.get("adapter") != "whatsapp"
                                or not isinstance(record.get("runtime"), dict)
                                or not record.get("runtime")):
                            diagnostics.fail("session_not_started")
                            raise RuntimeError("the test session route was not persisted")
                        if record.get("whatsapp_account") != account:
                            raise RuntimeError("the test session account binding did not match")
                        if record.get("chat_id") != next(
                            participant for participant, route in routes.items()
                            if route is matching
                        ):
                            raise RuntimeError("the test session participant binding did not match")
                        _verify_private_tmux(env, tmux_root, session_name)
                        verified_session = session_name
                        diagnostics.passed("session_spawn")
                    if diagnostics.checks["harness_boot"] != "passed":
                        diagnostics.enter("harness_boot")
                        process_status = _harness_process_status(session_name, harness)
                        diagnostics.set_harness_process(
                            "present" if process_status is True else
                            "absent" if process_status is False else "unknown",
                        )
                        symptom = _capture_boot_symptom(env, session_name)
                        if _handle_boot_symptom(diagnostics, symptom):
                            raise RuntimeError("the isolated harness has an actionable boot prompt")
                        readiness = _harness_readiness_status(session_name, harness)
                        diagnostics.set_harness_readiness(readiness)
                        if readiness == "ready_marker_seen":
                            diagnostics.passed("harness_boot")
                    else:
                        process_status = _harness_process_status(session_name, harness)
                        diagnostics.set_harness_process(
                            "present" if process_status is True else
                            "absent" if process_status is False else "unknown",
                        )
                if send_gate.verified_session:
                    # The send gate may verify first if the model notifies
                    # before this polling loop observes the persisted route.
                    verified_session = send_gate.verified_session
                if (verified_session == send_gate.verified_session
                        and _confirmed_notify(send_gate, session_name, diagnostics)):
                    delivered = True
                    break
                if server_errors:
                    diagnostics.enter("server_health")
                    diagnostics.fail("server_start_failed")
                    raise RuntimeError("the isolated local server stopped unexpectedly")
            if not delivered:
                if not phase["selected"]:
                    diagnostics.enter("inbound_selection")
                    diagnostics.fail("selection_not_confirmed")
                elif not phase["prompted"]:
                    diagnostics.enter("challenge_dispatch")
                    diagnostics.fail("session_not_started")
                elif (diagnostics.checks["session_spawn"] == "passed"
                      and diagnostics.harness_process == "absent"):
                    diagnostics.enter("harness_boot")
                    diagnostics.fail("harness_boot_unconfirmed")
                else:
                    diagnostics.enter("notify")
                    diagnostics.fail("run_timeout")
                raise TimeoutError("timed out waiting for the model reply")
            _say("Verified one real model session, account-bound registry route, and nonce reply.")
            diagnostics.enter("notify")
            diagnostics.passed("notify")
            return 0
    except BaseException:
        if not diagnostics.category:
            diagnostics.fail()
        raise
    finally:
        diagnostics.enter("cleanup")
        cleanup_ok = _cleanup_resources(
            server, server_thread, listener, session_name, env, tmux_root,
        )
        diagnostics.checks["cleanup"] = "passed" if cleanup_ok else "failed"
        if not cleanup_ok and not diagnostics.category:
            diagnostics.category = "cleanup_failed"
        if adapter is not None:
            adapter.TOKEN = ""
        secret["value"] = ""
        os.environ.clear()
        os.environ.update(old_env)
        sys.path[:] = old_path
        sys.stdout, sys.stderr = original_stdout, original_stderr
        log_stream.close()
        _remove_raw_log(run_root)
        if not cleanup_ok:
            _say("Cleanup needs attention; the private test directory was retained.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="run the guarded live model-session test")
    parser.add_argument("--harness", choices=("codex",),
                        help="use this already-authorized harness login path")
    args = parser.parse_args()
    if not args.run or not args.harness:
        parser.error("both --run and --harness are required")
    diagnostics = RunDiagnostics(args.harness)
    try:
        owner_test._require_tty()
        if input("This starts one real model session in private test state. Type MODEL to continue: ").strip() != "MODEL":
            print("Cancelled; no key was read and no process was started.")
            return 1
        result = _run_model_test(args.harness, diagnostics)
        if result and diagnostics.category:
            _say(_diagnostic_line(diagnostics))
        return result
    except KeyboardInterrupt:
        print("Stopped by owner; cleanup was attempted.")
        return 130
    except Exception:
        if not diagnostics.category:
            diagnostics.fail()
        _say(_diagnostic_line(diagnostics))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
