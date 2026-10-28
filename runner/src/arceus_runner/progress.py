"""What a running task is doing between `task_dispatched` and its verdict.

The engine's durable task stream (tasks/<key>/events.jsonl) says only when an attempt starts and
ends. The steps in between (preparation, the brief reaching the session, the gate, the verifier,
Codex's review, a pause for a usage limit) are written to the run's own journal,
<instance>/run-events.jsonl, which goes away with the instance and is shared by every run in that
folder. This module reads that journal for the current attempt, only the lines of its dispatch, and
writes each new step into the runner's events.jsonl as `engine_step`, so it reaches Loki with the
same schema and the same secret redaction as everything else.

The agent's own activity comes from its Claude Code transcript: when it last wrote anything, and
which tools it used since the last look (tool names and the agent's one-line descriptions, never
commands or their output). An `agent_activity` line at most every `activity_every`, and one
`agent_quiet` when the agent has written nothing for `quiet_after` while the step is its own.

Everything here only reads files. A missing or unreadable file is "unknown", never an error: the
task itself is followed by delivery.py through `task show`, whatever happens here.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arceus_runner.db import TaskStore
from arceus_runner.events import EventLog

NOTE_LIMIT = 300
# Steps where the agent is expected to be silent: the gate, the verifier and Codex are working, or the
# engine waits for a usage window. A quiet transcript then is not a stuck agent.
NOT_AGENT_STAGES = ("gate", "run", "outcome")
WAITING_STATES = ("waiting", "parked")


def task_key(task_id: str) -> str:
    """The engine's folder name for a task (engine/bin/lib/tasks.sh: task_key)."""
    return "t-" + hashlib.sha256(task_id.encode()).hexdigest()[:20]


def _clip(text: str, limit: int = NOTE_LIMIT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _jsonl(data: bytes) -> list[dict]:
    """Whole lines only: the last one may still be being written."""
    out = []
    for raw in data.split(b"\n")[:-1]:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


@dataclass
class Step:
    stage: str
    state: str
    since: float  # epoch seconds
    note: str = ""


@dataclass
class TaskProgress:
    """The picture of one task, kept between looks; the metrics read it."""
    task_id: str
    dispatch_id: str = ""
    attempt: int = 0
    step: Step | None = None
    last_ms: int = 0          # the newest engine line already written out …
    at_last_ms: int = 0       # … and how many of the lines with exactly that time
    observed_at: float = 0.0  # when the engine's files were last read successfully
    transcript: Path | None = None
    transcript_offset: int = 0
    agent_active_at: float | None = None
    tools: Counter = field(default_factory=Counter)
    last_tool: str = ""
    last_target: str = ""
    activity_emitted_at: float = 0.0
    quiet_reported: bool = False
    journal_sig: tuple = ()   # size and mtime of the journal files at the last read
    problem: str = ""         # the last read error, said once


def tool_target(name: str, args: dict, workspace: Path) -> str:
    """A short, safe word on what a tool call was about: the agent's own description of a command,
    a file path below the workspace, a search pattern. Never the command itself."""
    if not isinstance(args, dict):
        return ""
    if name == "Bash":
        return _clip(args.get("description") or "", 120)
    for key in ("file_path", "notebook_path", "path"):
        value = args.get(key)
        if isinstance(value, str) and value:
            p = Path(value)
            try:
                return str(p.resolve().relative_to(workspace))
            except (ValueError, OSError):
                return p.name
    for key in ("description", "skill", "pattern", "query", "url"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return _clip(value, 120)
    return ""


class ProgressWatcher:
    def __init__(self, state_dir: Path, workspace: Path, store: TaskStore, events: EventLog,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC), *,
                 activity_every: timedelta = timedelta(minutes=1), quiet_after: timedelta = timedelta(minutes=20),
                 projects_dir: Path | None = None):
        self.state_dir = state_dir
        self.workspace = workspace.resolve()
        self.store = store
        self.events = events
        self.now = now
        self.activity_every = activity_every.total_seconds()
        self.quiet_after = quiet_after.total_seconds()
        self.projects_dir = projects_dir or Path.home() / ".claude" / "projects"
        self.tasks: dict[str, TaskProgress] = {}

    # -- one look ---------------------------------------------------------------------------------

    def poll(self) -> None:
        active = {t.task_id for t in self.store.tasks("dispatched")}
        for gone in set(self.tasks) - active:
            # Over, or waiting on a person: nothing runs, nothing more to show. The steps the engine wrote
            # since the last look (the gate's verdict that ended the attempt) are read once more first.
            try:
                self._last_steps(self.tasks[gone])
            except OSError as e:
                self.events.emit("progress_unreadable", task_id=gone, reason=str(e))
            del self.tasks[gone]
        for task_id in sorted(active):
            try:
                self._look(self.tasks.setdefault(task_id, TaskProgress(task_id)))
                why = ""
            except OSError as e:  # a file vanished between two reads: the next look sees the new picture
                why = str(e)
                if why != self.tasks[task_id].problem:
                    self.events.emit("progress_unreadable", task_id=task_id, reason=why)
            self.tasks[task_id].problem = why

    def _look(self, p: TaskProgress) -> None:
        record = self._task_record(p.task_id)
        attempts = (record or {}).get("attempts") or []
        if not attempts:
            return
        last = attempts[-1]
        did = last.get("dispatch_id") or ""
        if did != p.dispatch_id:
            # A new attempt: a fresh cursor and transcript. A restarted runner starts here too, and goes
            # on from the cursor it saved, so the attempt's earlier steps are not written twice.
            fresh = TaskProgress(p.task_id, dispatch_id=did, attempt=last.get("n") or len(attempts))
            self._restore(fresh)
            self.tasks[p.task_id] = p = fresh
        instance = Path(last.get("instance") or "")
        if last.get("instance") and instance.is_dir():
            self._read_steps(p, instance)
            self._read_transcript(p, instance)
            p.observed_at = self.now().timestamp()
        self._maybe_report_activity(p)

    def _last_steps(self, p: TaskProgress) -> None:
        """The steps of the attempt being watched that came after the last look; no activity, no new attempt."""
        attempts = (self._task_record(p.task_id) or {}).get("attempts") or []
        last = attempts[-1] if attempts else {}
        instance = Path(last.get("instance") or "")
        if p.dispatch_id and last.get("dispatch_id") == p.dispatch_id and last.get("instance") and instance.is_dir():
            self._read_steps(p, instance)

    def _restore(self, p: TaskProgress) -> None:
        """After a restart: go on from the last step written, never write the attempt's steps again."""
        saved = self.store.progress_cursor(p.task_id)
        if saved and saved[0] == p.dispatch_id:
            p.last_ms, p.at_last_ms = saved[1], saved[2]

    def _task_record(self, task_id: str) -> dict | None:
        f = self.state_dir / "tasks" / task_key(task_id) / "task.json"
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("task_id") == task_id else None

    # -- the engine's steps -----------------------------------------------------------------------

    def _read_steps(self, p: TaskProgress, instance: Path) -> None:
        files = [f for f in (instance / "run-events.jsonl.1", instance / "run-events.jsonl") if f.exists()]
        sig = tuple((f.name, st.st_size, st.st_mtime_ns) for f in files for st in [f.stat()])
        if sig == p.journal_sig:
            return
        lines: list[dict] = []
        for f in files:  # the rotated half first: its lines are older
            lines += [x for x in _jsonl(f.read_bytes()) if x.get("dispatch_id") == p.dispatch_id]
        lines.sort(key=lambda x: x.get("ms") or 0)  # stable: lines of one millisecond keep their order
        written = 0
        seen_at_ms = 0
        for x in lines:
            ms = x.get("ms") or 0
            if ms == p.last_ms:
                seen_at_ms += 1
            if ms < p.last_ms or (ms == p.last_ms and seen_at_ms <= p.at_last_ms):
                self._set_step(p, x)  # written before (a restart): it still says where the attempt is
                continue
            if ms != p.last_ms:
                p.last_ms, p.at_last_ms = ms, 0
            p.at_last_ms += 1
            seen_at_ms = p.at_last_ms
            self._step(p, x)
            written += 1
        if written:
            self.store.save_progress_cursor(p.task_id, p.dispatch_id, p.last_ms, p.at_last_ms)
        p.journal_sig = sig

    def _step(self, p: TaskProgress, x: dict) -> None:
        extra = {k: x.get(k) for k in ("node", "reason", "ms_taken", "resume_at", "result", "disposition", "exit")
                 if x.get(k) not in (None, "")}
        self.events.emit("engine_step", task_id=p.task_id, attempt=p.attempt, stage=str(x.get("stage") or "?"),
                         state=str(x.get("state") or "?"), note=_clip(str(x.get("note") or "")) or None,
                         at=x.get("at"), **extra)
        self._set_step(p, x)

    def _set_step(self, p: TaskProgress, x: dict) -> None:
        stage, state = str(x.get("stage") or "?"), str(x.get("state") or "?")
        if p.step and (p.step.stage, p.step.state) == (stage, state):
            return
        since = (x.get("ms") or 0) / 1000 or self.now().timestamp()
        p.step = Step(stage, state, since, _clip(str(x.get("note") or "")))
        p.quiet_reported = False

    # -- the agent's transcript -------------------------------------------------------------------

    def _find_transcript(self, instance: Path) -> Path | None:
        try:
            sid = (instance / "claude-session-id").read_text().strip()
        except OSError:
            return None
        if not sid or "/" in sid:
            return None
        try:  # the engine remembers where it found it: "<session id>\t<path>"
            csid, cpath = (instance / ".transcript-path").read_text().strip().split("\t", 1)
            if csid == sid and Path(cpath).is_file():
                return Path(cpath)
        except (OSError, ValueError):
            pass
        return next(iter(self.projects_dir.glob(f"*/{sid}.jsonl")), None)

    def _read_transcript(self, p: TaskProgress, instance: Path) -> None:
        path = self._find_transcript(instance)
        if path is None:
            return
        if path != p.transcript:
            # First sight (a new session, or a restarted runner): from its end. What came before is
            # history, not activity, and counting it would report hours of calls as the last minute.
            p.transcript = path
            p.transcript_offset = path.stat().st_size
        st = path.stat()
        p.agent_active_at = st.st_mtime
        if st.st_size < p.transcript_offset:
            p.transcript_offset = 0
        if st.st_size == p.transcript_offset:
            return
        with path.open("rb") as f:
            f.seek(p.transcript_offset)
            data = f.read()
        whole = data[:data.rfind(b"\n") + 1]
        p.transcript_offset += len(whole)
        for entry in _jsonl(whole):
            if entry.get("type") != "assistant":
                continue
            for part in (entry.get("message") or {}).get("content") or []:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    name = str(part.get("name") or "?")
                    p.tools[name] += 1
                    p.last_tool, p.last_target = name, tool_target(name, part.get("input") or {}, self.workspace)

    def _agent_turn(self, p: TaskProgress) -> bool:
        """Whether the agent should be the one producing output now."""
        if not p.step:
            return True
        if p.step.state in WAITING_STATES:
            return False
        return not p.step.stage.startswith(NOT_AGENT_STAGES)

    def _maybe_report_activity(self, p: TaskProgress) -> None:
        now = self.now().timestamp()
        if p.tools and now - p.activity_emitted_at >= self.activity_every:
            self.events.emit("agent_activity", task_id=p.task_id, attempt=p.attempt, tools=sum(p.tools.values()),
                             by_tool=dict(p.tools.most_common(6)), last_tool=p.last_tool,
                             last_target=p.last_target or None, step=p.step.stage if p.step else None)
            p.tools.clear()
            p.activity_emitted_at = now
            p.quiet_reported = False
        quiet = self.quiet_seconds(p)
        if quiet is None:
            return
        if quiet >= self.quiet_after and not p.quiet_reported:
            self.events.emit("agent_quiet", task_id=p.task_id, attempt=p.attempt, minutes=round(quiet / 60),
                             step=p.step.stage if p.step else None)
            p.quiet_reported = True
        elif quiet < self.quiet_after:
            p.quiet_reported = False

    def quiet_seconds(self, p: TaskProgress) -> float | None:
        """How long the agent has written nothing while the step is its own; None when it is not its turn
        or its transcript is unknown. Counted from the step's start at most: a new step is a new chance."""
        if p.agent_active_at is None or not self._agent_turn(p):
            return None
        return max(0.0, self.now().timestamp() - max(p.agent_active_at, p.step.since if p.step else 0))

    # -- for the metrics --------------------------------------------------------------------------

    def snapshot(self) -> list[TaskProgress]:
        return list(self.tasks.values())
