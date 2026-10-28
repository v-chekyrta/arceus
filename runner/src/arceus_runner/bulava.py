"""What the installed Bulava engine offers.

What Bulava's author has promised is not proof that a feature is installed.
The probe reads the pinned engine on disk and reports which contracts its
scripts advertise. Live work is allowed only when every
contract it needs is advertised; contract tests on the server confirm them.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_FILES = ("bin/dispatch.sh", "bin/worker-send.sh", "bin/night-shift.sh")


@dataclass(frozen=True)
class Capability:
    name: str
    wave: int
    files: tuple[str, ...]
    pattern: str
    meaning: str


CAPABILITIES: tuple[Capability, ...] = (
    Capability("dispatch.task_id", 1, ("bin/dispatch.sh",), r"--task-id\b", "dispatch.sh --task-id"),
    Capability("dispatch.branch", 1, ("bin/dispatch.sh",), r"--branch\b", "dispatch.sh --branch"),
    Capability("dispatch.base", 1, ("bin/dispatch.sh",), r"--base\b", "dispatch.sh --base"),
    Capability("dispatch.dirty", 1, ("bin/dispatch.sh",), r"--dirty\b", "dispatch.sh --dirty=fail"),
    Capability("profile.server", 1, ("bin/supervisor-lib.sh", "bin/night-shift.sh", "bin/dispatch.sh"),
               r"SUPERVISOR_PROFILE", "SUPERVISOR_PROFILE=server"),
    Capability("night_shift.trust", 1, ("bin/night-shift.sh", "bin/ns-cli.py"), r"(^\s*trust\)|\btrust <dir>|[\"']trust[\"'])",
               "night-shift trust <dir>"),
    Capability("receipt.reviewed_sha", 1, ("bin/supervisor-lib.sh", "bin/worker-outcome.sh", "bin/receipt-render.py"),
               r"reviewed_sha", "receipt.json repos[].reviewed_sha"),
    Capability("night_shift.task_show", 2, ("bin/night-shift.sh", "bin/ns-cli.py"), r"task show",
               "night-shift task show --task-id --json"),
    Capability("worker_send.relation", 2, ("bin/worker-send.sh",), r"--relation\b", "worker-send.sh --relation"),
    Capability("hook.on_outcome", 2, ("bin/supervisor-lib.sh", "bin/lib/tasks.sh", "bin/worker-outcome.sh"),
               r"SUPERVISOR_ON_OUTCOME", "SUPERVISOR_ON_OUTCOME hook"),
    Capability("doctor.server", 2, ("bin/ns-cli.py", "bin/night-shift.sh"), r"[\"']--server[\"']",
               "night-shift doctor --server --json"),
    # Not needed for live work: needed only when config.yaml chooses a model or a depth.
    Capability("settings.models", 3, ("supervisor/config.sh",), r"SUPERVISOR_CLAUDE_MODEL",
               "SUPERVISOR_{CLAUDE,CODEX}_{MODEL,EFFORT}"),
    Capability("settings.file_off", 3, ("bin/night-shift.sh",), r"NIGHT_SHIFT_SETTINGS",
               "NIGHT_SHIFT_SETTINGS=off"),
    # Needed when config.yaml turns the independent checks on (bulava.claude_checks.enabled, the default).
    Capability("gate.claude_checks", 3, ("hooks/review-gate.sh",), r"^\s*if .*claude_checks_enabled\b",
               "the independent Claude checks before Codex (SUPERVISOR_CLAUDE_CHECKS)"),
    # On a server a worker inherits the environment of the engine's tmux server (bulava-engine.service),
    # not the dispatch's. An engine that carries these on the worker's launch line, as it does for the
    # names in _RUN_ENV_VARS, takes them from the dispatch; until then the unit is the way in.
    Capability("settings.claude_checks", 3, ("bin/supervisor-lib.sh",), r'_RUN_ENV_VARS="[^"]*SUPERVISOR_CLAUDE_CHECKS',
               "SUPERVISOR_CLAUDE_CHECKS_* carried to the worker's review gate"),
)


@dataclass
class Probe:
    root: Path
    installed: bool = False
    version: str | None = None
    commit: str | None = None
    advertised: dict[str, bool] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def wave_ready(self, wave: int) -> bool:
        need = [c.name for c in CAPABILITIES if c.wave <= wave]
        return self.installed and not self.problems and all(self.advertised.get(n) for n in need)

    def missing_for_live(self) -> list[str]:
        """What `mode: live` uses: every wave 1 contract, `task show` and `worker-send --relation`."""
        need = {c.name for c in CAPABILITIES if c.wave == 1} | {"night_shift.task_show", "worker_send.relation"}
        return [c.meaning for c in CAPABILITIES if c.name in need and not self.advertised.get(c.name)]

    def missing_for_models(self) -> list[str]:
        """What a model or depth chosen in config.yaml needs, so the choice is applied and nothing outranks it."""
        need = ("settings.models", "settings.file_off")
        return [c.meaning for c in CAPABILITIES if c.name in need and not self.advertised.get(c.name)]

    def missing_for_checks(self) -> list[str]:
        """What bulava.claude_checks.enabled needs: a review gate that runs the checks."""
        return [c.meaning for c in CAPABILITIES if c.name == "gate.claude_checks" and not self.advertised.get(c.name)]

    def missing(self, wave: int) -> list[str]:
        return [c.meaning for c in CAPABILITIES if c.wave <= wave and not self.advertised.get(c.name)]

    def as_dict(self) -> dict:
        return {
            "root": str(self.root),
            "installed": self.installed,
            "version": self.version,
            "commit": self.commit,
            "advertised": self.advertised,
            "wave1_ready": self.wave_ready(1),
            "wave2_ready": self.wave_ready(2),
            "problems": self.problems,
        }


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _code(text: str) -> str:
    """Drop whole-line comments: a flag mentioned in a comment is not a flag the script accepts."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _writable_by_us(root: Path) -> list[str]:
    """The engine must be read-only for the user the runner and the agent run as."""
    hits = []
    for p in [root, root / "bin", root / "hooks", *sorted((root / "bin").glob("*"))]:
        if p.exists() and os.access(p, os.W_OK):
            hits.append(str(p))
    return hits


def probe(root: Path) -> Probe:
    p = Probe(root=root)
    if not root.is_dir():
        p.problems.append(f"{root} does not exist")
        return p
    for rel in REQUIRED_FILES:
        f = root / rel
        if not f.is_file():
            p.problems.append(f"{rel} is missing")
        elif not os.access(f, os.X_OK):
            p.problems.append(f"{rel} is not executable")
    if p.problems and (root / "engine" / "bin" / "dispatch.sh").is_file():
        p.problems = [f"{root} is the repository, the engine is {root / 'engine'}: set paths.bulava to it"]
    p.installed = not p.problems
    p.version = _read(root / ".engine-version").strip() or None
    # setup.sh writes the pinned source and commit here; the root-owned copy has no .git.
    for line in _read(root / ".arceus-source").splitlines():
        if line.startswith("commit "):
            p.commit = line.removeprefix("commit ").strip()
    if not p.commit and (root / ".git").exists():
        try:
            p.commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            p.problems.append("cannot read the engine's git commit")
    writable = _writable_by_us(root)
    if writable:
        p.problems.append(f"engine is writable by this user: {', '.join(writable[:3])}")
    texts = {}
    for cap in CAPABILITIES:
        found = False
        for rel in cap.files:
            if rel not in texts:
                texts[rel] = _code(_read(root / rel))
            if re.search(cap.pattern, texts[rel], re.MULTILINE):
                found = True
                break
        p.advertised[cap.name] = found
    return p
