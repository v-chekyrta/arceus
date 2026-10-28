"""Health checks: what the runner sees itself (disk, RAM, the engine
folder, the CLIs) plus the engine's `doctor --server --json` (Claude and Codex sign-in,
hooks, usage, browser). Any `fail` stops new tasks; running ones are still followed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from arceus_runner.bulava import Probe
from arceus_runner.checks import Check, Status
from arceus_runner.config import Config
from arceus_runner.engine import Engine, EngineError

TOOLS = ("git", "tmux", "jq", "claude", "codex", "google-chrome")


def _existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def disk(cfg: Config) -> Check:
    usage = shutil.disk_usage(_existing(cfg.paths.workspace))
    free = 100 * usage.free / usage.total
    h = cfg.health
    status: Status = "fail" if free < h.disk_fail_percent else "warn" if free < h.disk_warn_percent else "ok"
    return Check("disk_free", status, f"{free:.0f}% free ({usage.free // 2**30} GB)")


def ram(cfg: Config, meminfo: Path = Path("/proc/meminfo")) -> Check:
    try:
        lines = dict(line.split(":", 1) for line in meminfo.read_text().splitlines() if ":" in line)
        avail_gb = int(lines["MemAvailable"].split()[0]) / 2**20
    except (OSError, KeyError, ValueError):
        return Check("ram_available", "warn", "cannot read /proc/meminfo")
    status: Status = "warn" if avail_gb < cfg.health.ram_warn_gb else "ok"
    return Check("ram_available", status, f"{avail_gb:.1f} GB available")


def tools() -> list[Check]:
    out = []
    for t in TOOLS:
        path = shutil.which(t)
        out.append(Check(f"tool.{t}", "ok" if path else "fail", path or f"{t} not on PATH"))
    return out


def bulava(probe: Probe) -> Check:
    if not probe.installed:
        return Check("bulava.installed", "fail", "; ".join(probe.problems))
    if probe.problems:
        return Check("bulava.installed", "fail", "; ".join(probe.problems))
    ver = probe.commit or probe.version or "unknown version"
    if probe.wave_ready(1):
        return Check("bulava.installed", "ok", f"{ver}; wave 1 contracts advertised")
    return Check("bulava.installed", "warn", f"{ver}; missing for live mode: {', '.join(probe.missing(1))}")


def engine_tmux_env(socket: str = "bulava") -> dict[str, str] | None:
    """The global environment of the engine's tmux server (bulava-engine.service), which every worker
    session and so every review gate inherits. None when no server runs."""
    try:
        r = subprocess.run(["tmux", "-L", socket, "show-environment", "-g"], capture_output=True, text=True,
                           timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line and not line.startswith("-"))


def claude_checks(cfg: Config, probe: Probe, live_env=None) -> Check:
    """The engine's code and security checks before Codex, as config.yaml sets them. No new tasks when
    the engine would not do what config.yaml says: checks turned on and an engine without them, or a
    review gate that would run with other settings (bulava-engine.service not updated or not restarted)."""
    c = cfg.bulava.claude_checks
    if not probe.installed:
        return Check("config.claude_checks", "warn", "the engine is not installed")
    shown = ("off" if not c.enabled else
             f"{' + '.join(dict.fromkeys(c.kinds))}, back to the agent from {c.min_severity}, "
             f"{c.rounds} round(s) per task, {int(c.timeout.total_seconds() // 60)} min each"
             + (", required" if c.required else ", not required"))
    if c.enabled and probe.missing_for_checks():
        return Check("config.claude_checks", "fail", f"{shown}; the engine in {cfg.paths.bulava} has no "
                                                     "independent checks: update it (setup.sh)")
    live = (live_env or engine_tmux_env)()
    if live is not None and not probe.advertised.get("settings.claude_checks"):
        want, gate = c.env(), c.gate_env(live)
        differ = [f"{k}={gate[k]} (config.yaml: {want[k]})" for k in want if gate[k] != want[k]]
        if differ:
            return Check("config.claude_checks", "fail",
                         f"the review gate would run with {', '.join(differ)}: bulava-engine.service is not "
                         "up to date with config.yaml; run sudo ./setup.sh, then restart bulava-engine between tasks")
    if not c.enabled:
        return Check("config.claude_checks", "warn", "off in config.yaml: Codex alone reviews the work")
    return Check("config.claude_checks", "ok", shown)


def engine_doctor(engine: Engine) -> list[Check]:
    """The engine's own health, with the environment the runner gives it: Claude and Codex
    signed in as this user, hooks installed, usage readings, the browser."""
    try:
        report = engine.doctor()
    except EngineError as e:
        return [Check("bulava.doctor", "fail", str(e))]
    out = []
    for c in report.get("checks") or []:
        status = c.get("status") if c.get("status") in ("ok", "warn", "fail") else "warn"
        detail = c.get("detail")
        out.append(Check(f"bulava.{c.get('name')}", status,
                         detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)[:300]))
    return out or [Check("bulava.doctor", "warn", "doctor returned no checks")]


def devstack(cfg: Config) -> list[Check]:
    """health.devstack_check: the project's dev stack is up. Absent: not checked."""
    cmd = cfg.health.devstack_check
    if not cmd:
        return []
    try:
        r = subprocess.run(cmd, shell=True, cwd=_existing(cfg.paths.workspace), capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [Check("devstack", "fail", f"{cmd!r} did not answer in 60 s")]
    if r.returncode != 0:
        last = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        return [Check("devstack", "fail", f"{cmd!r}: exit {r.returncode} {last[0][:200]}")]
    return [Check("devstack", "ok", cmd)]


def local_checks(cfg: Config, probe: Probe) -> list[Check]:
    return [disk(cfg), ram(cfg), bulava(probe), claude_checks(cfg, probe), *tools(), *devstack(cfg)]
