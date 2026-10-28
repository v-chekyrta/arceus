"""The Bulava engine's command line, as engine/SERVER.md defines it.

Bulava's processes get their own environment: the engine settings plus
bulava.env (the Claude token). The runner's tracker, code host and Slack tokens
are never in it.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from arceus_runner.config import Config, read_env_file
from arceus_runner.gitops import _workspace_umask
from arceus_runner.screening import Clearance, NotScreened

TIMEOUT = 900
# Chosen in config.yaml (bulava.*_model, *_effort, claude_checks); bulava.env is for the token and
# cannot override them.
MODEL_VARS = ("SUPERVISOR_CLAUDE_MODEL", "SUPERVISOR_CLAUDE_EFFORT", "SUPERVISOR_CODEX_MODEL", "SUPERVISOR_CODEX_EFFORT")
CHECKS_PREFIX = "SUPERVISOR_CLAUDE_CHECKS"


class EngineError(Exception):
    pass


@dataclass(frozen=True)
class Reply:
    rc: int
    tier: str | None  # dispatched, duplicate, busy, conflict
    data: dict = field(default_factory=dict)
    stderr: str = ""

    @property
    def accepted(self) -> bool:
        return self.rc == 0 and self.tier in ("dispatched", "duplicate")

    def why(self) -> str:
        last = [line for line in self.stderr.strip().splitlines() if line.strip()]
        return f"exit {self.rc}" + (f": {last[-1][:300]}" if last else "")


def _require(clearance: Clearance, text: str, task_id: str) -> None:
    """Nothing reaches the agent that the security screen did not clear, or that is not the runner's own."""
    if not isinstance(clearance, Clearance) or not clearance.covers(text):
        raise NotScreened(f"{task_id}: the text for the agent has no security clearance")


def parse_reply(rc: int, stdout: str, stderr: str) -> Reply:
    tier = None
    data: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("TIER="):
            tier = line.removeprefix("TIER=")
        elif line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                pass
    return Reply(rc, tier, data, stderr)


class Engine:
    def __init__(self, cfg: Config, extra_env: dict[str, str] | None = None, events=None):
        self.cfg = cfg
        self.bin = cfg.paths.bulava / "bin"
        self.extra_env = extra_env or {}  # tests: a stub worker instead of Claude
        self.events = events  # an EventLog: dispatch and send can take minutes, and say so in events.jsonl

    def env(self) -> dict[str, str]:
        b = self.cfg.bulava
        home = os.environ.get("HOME", str(Path.home()))
        env = {
            "HOME": home,
            "USER": os.environ.get("USER", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "SUPERVISOR_PROFILE": "server",
            "SUPERVISOR_REPORT_LANGUAGE": b.report_language,
            "SUPERVISOR_CODEX_UNAVAILABLE": b.codex_unavailable,
            "SUPERVISOR_CODEX_UNAVAILABLE_AFTER_HOURS": str(b.codex_unavailable_after_hours),
            "SUPERVISOR_HANDSHAKE_WAIT": str(int(b.start_handshake_wait.total_seconds())),
            "SUPERVISOR_SERVER_EXTRA_SECRET_PATHS": " ".join(str(p) for p in self.secret_paths()),
        }
        if b.mcp_config:
            env["SUPERVISOR_AUTOMATION_MCP_CONFIG"] = str(b.mcp_config)
        env["SUPERVISOR_STATE_DIR"] = str(self.cfg.paths.bulava_state)
        # config.yaml is the only place the models are chosen: never ~/.config/night-shift/config.json,
        # which the engine would otherwise let outrank a choice equal to its own default.
        env["NIGHT_SHIFT_SETTINGS"] = "off"
        env.update(self.token_env())
        env.update(b.model_env())
        # The engine's checks before Codex. doctor reports them from here; the review gate on a server
        # reads them from the worker's environment, which comes from bulava-engine.service, so only the
        # engine's defaults reach it until the engine carries these (health.claude_checks).
        env.update(b.claude_checks.env())
        env.update(self.extra_env)
        return env

    def token_env(self) -> dict[str, str]:
        """bulava.env, if there is anything real in it. Without it Claude uses its own sign-in
        (~/.claude/.credentials.json). An empty or example value must never reach the engine:
        it would override a working sign-in with a broken token."""
        path = self.cfg.paths.bulava_env
        if not path.exists():
            return {}
        return {k: v for k, v in read_env_file(path).items()
                if v.strip() and "replace-me" not in v and k not in MODEL_VARS and not k.startswith(CHECKS_PREFIX)}

    def refresh_usage(self) -> None:
        """Take fresh subscription readings with the engine's own scripts (Bulava takes them only during
        runs, so an idle machine's readings go stale). Failures are left to doctor to report."""
        for script in ("claude-usage.sh", "codex-usage.sh"):
            try:
                self._run([str(self.bin / script)], timeout=60)
            except EngineError:
                pass

    def doctor(self) -> dict:
        """`night-shift doctor --server --json`: sign-ins, hooks, usage, browser (engine/SERVER.md)."""
        r = self._run([str(self.bin / "night-shift.sh"), "doctor", "--server", "--json"], timeout=120)
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise EngineError(f"doctor --server: exit {r.returncode}, no JSON: {(r.stderr or r.stdout).strip()[-300:]}") from None

    def secret_paths(self) -> list[Path]:
        p = self.cfg.paths
        return [p.secrets, p.bulava_env, p.db, p.db.with_suffix(".lock")]

    def _run(self, args: list[str], cwd: Path | None = None, timeout: int = TIMEOUT,
             extra_dirs: Path | None = None) -> subprocess.CompletedProcess:
        env = self.env()
        if extra_dirs:
            # The other repositories of the task, one path per line, writable-directories.txt beside it.
            env["SUPERVISOR_EXTRA_DIRS_FILE"] = str(extra_dirs)
        try:
            # The engine starts the agent's sessions and its stack from here: the project's ordinary
            # umask, not the runner's private one (see gitops.WORKSPACE_UMASK).
            return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, preexec_fn=_workspace_umask)
        except subprocess.TimeoutExpired:
            raise EngineError(f"{Path(args[0]).name}: timed out after {timeout}s") from None
        except OSError as e:
            raise EngineError(f"{Path(args[0]).name}: {e}") from None

    def dispatch(self, *, task_id: str, message_id: str, author: str, branch: str, base: str, repo: Path,
                 text: str, clearance: Clearance, source: str = "tracker", extra_dirs: Path | None = None) -> Reply:
        _require(clearance, text, task_id)
        return self._call("dispatch", task_id, message_id, [
            str(self.bin / "dispatch.sh"), "--task-id", task_id, "--message-id", message_id, "--source", source,
            "--author", author, "--branch", branch, "--base", base, "--dirty", "fail", str(repo), text], extra_dirs)

    def send(self, *, task_id: str, message_id: str, source: str, author: str, repo: Path, text: str,
             clearance: Clearance, answers: str | None = None, extra_dirs: Path | None = None) -> Reply:
        _require(clearance, text, task_id)
        args = [str(self.bin / "worker-send.sh"), "--task-id", task_id, "--relation", "continue",
                "--message-id", message_id, "--source", source, "--author", author]
        if answers:
            args += ["--answers", answers]
        return self._call("send", task_id, message_id, [*args, str(repo), "-", "-", "-", text], extra_dirs)

    def _call(self, call: str, task_id: str, message_id: str, args: list[str], extra_dirs: Path | None) -> Reply:
        """dispatch.sh or worker-send.sh, bracketed by events: the runner's loop waits for it (up to TIMEOUT),
        and without them that wait looks like nothing happening."""
        emit = self.events.emit if self.events else lambda *a, **k: None
        emit("engine_call_started", call=call, task_id=task_id, message_id=message_id)
        started = time.monotonic()
        try:
            r = self._run(args, extra_dirs=extra_dirs)
        except EngineError as e:
            emit("engine_call_finished", call=call, task_id=task_id, result="error", reason=str(e),
                 seconds=round(time.monotonic() - started, 1))
            raise
        reply = parse_reply(r.returncode, r.stdout, r.stderr)
        emit("engine_call_finished", call=call, task_id=task_id, result=reply.tier or f"exit {reply.rc}",
             seconds=round(time.monotonic() - started, 1))
        return reply

    def show(self, task_id: str) -> dict | None:
        """The task record, or None if the engine does not know the task."""
        r = self._run([str(self.bin / "night-shift.sh"), "task", "show", "--task-id", task_id, "--json"], timeout=60)
        if r.returncode != 0:
            # The engine's own words for an unknown id (night-shift.sh: «задачі X немає», exit 1).
            # Anything else is a failure to retry, never a reason to treat the task as gone.
            if r.returncode == 1 and f"задачі {task_id} немає" in r.stderr:
                return None
            raise EngineError(f"task show {task_id}: exit {r.returncode}: {r.stderr.strip()[-300:]}")
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise EngineError(f"task show {task_id}: not JSON: {r.stdout[:200]}") from None

    def prepare_repo(self, repo: Path) -> None:
        """Once per repository: trust it for Claude and decide its MCP servers (skip)."""
        for args in (["trust", str(repo)], ["mcp-decide", str(repo), "skip"]):
            r = self._run([str(self.bin / "night-shift.sh"), *args], timeout=120)
            if r.returncode != 0:
                raise EngineError(f"night-shift {args[0]} {repo}: exit {r.returncode}: {r.stderr.strip()[-300:]}")

    def stop(self, repo: Path) -> None:
        self._run([str(self.bin / "night-shift.sh"), "stop", str(repo)], timeout=120)
