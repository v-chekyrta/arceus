"""The running task's steps and the agent's activity, carried from the engine's files into events.jsonl."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from arceus_runner.db import ReviewStore
from arceus_runner.delivery import Delivery
from arceus_runner.events import EventLog
from arceus_runner.metrics import Metrics
from arceus_runner.progress import ProgressWatcher, task_key

from .conftest import GL_TOKEN, read_events

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
DID = "dispatch-1"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self) -> datetime:
        return self.t


class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.state = tmp / "state"
        self.workspace = tmp / "workspace"
        self.workspace.mkdir()
        self.projects = tmp / "claude-projects"
        self.store = ReviewStore(tmp / "runner.db")
        self.events_path = tmp / "events.jsonl"
        self.events = EventLog(self.events_path, "acme", redact=[GL_TOKEN])
        self.clock = Clock()
        self.instance = tmp / "instances" / "acme-1"
        self.instance.mkdir(parents=True)
        self.store.create_task(task_id="ACME-1", issue_db_id="2-1", state="dispatched", summary="health", tag_author="j",
                               repo="", repo_path=str(self.workspace), branch="agent/ACME-1", base_branch="main",
                               question_id=None, question_at_ms=None, reason=None, mr_urls="[]",
                               attempt_started=None, extra_repos="[]")
        self.attempts: list[dict] = []
        self.attempt(DID)

    def watcher(self, **kw) -> ProgressWatcher:
        return ProgressWatcher(self.state, self.workspace, self.store, self.events, self.clock,
                               projects_dir=self.projects, **kw)

    def attempt(self, did: str, instance: Path | None = None) -> None:
        self.attempts.append({"n": len(self.attempts) + 1, "dispatch_id": did, "instance": str(instance or self.instance)})
        d = self.state / "tasks" / task_key("ACME-1")
        d.mkdir(parents=True, exist_ok=True)
        (d / "task.json").write_text(json.dumps({"task_id": "ACME-1", "state": "running", "attempts": self.attempts}))

    def step(self, stage: str, state: str, ms: int, note: str = "", did: str = DID, file: str = "run-events.jsonl",
             **extra) -> None:
        line = {"v": 1, "at": "2026-10-25T12:00:00Z", "ms": ms, "dispatch_id": did, "stage": stage, "state": state,
                **({"note": note} if note else {}), **extra}
        with (self.instance / file).open("a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def transcript(self, *entries: dict, mtime: datetime | None = None) -> Path:
        (self.instance / "claude-session-id").write_text("sess-1\n")
        path = self.projects / "-workspace" / "sess-1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        if mtime:
            os.utime(path, (mtime.timestamp(), mtime.timestamp()))
        return path

    def emitted(self, event: str | None = None) -> list[dict]:
        return [e for e in read_events(self.events_path) if event is None or e["event"] == event]


def tool(name: str, **args) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": args}]}}


@pytest.fixture
def w(tmp_path) -> World:
    return World(tmp_path)


def test_each_engine_step_is_written_once_and_other_dispatches_are_left_out(w):
    p = w.watcher()
    w.step("triage", "running", 1000)
    w.step("triage", "done", 2000, ms_taken=1000)
    w.step("deliver", "delivered", 3000, did="someone-elses-chat")
    p.poll()
    p.poll()
    p.poll()
    steps = w.emitted("engine_step")
    assert [(s["stage"], s["state"]) for s in steps] == [("triage", "running"), ("triage", "done")]
    assert steps[1]["ms_taken"] == 1000 and steps[0]["task_id"] == "ACME-1" and steps[0]["attempt"] == 1
    w.step("deliver", "delivered", 3000, note="Сесія підтвердила, що отримала бриф")
    w.step("gate.review", "running", 3000)  # the same millisecond as the line before
    p.poll()
    assert [(s["stage"], s["state"]) for s in w.emitted("engine_step")][2:] == [("deliver", "delivered"),
                                                                                ("gate.review", "running")]
    cur = p.snapshot()[0].step
    assert (cur.stage, cur.state, cur.since) == ("gate.review", "running", 3.0)


def test_a_restart_goes_on_from_the_saved_cursor_and_still_knows_the_step(w):
    w.step("deliver", "delivered", 1000)
    w.step("gate", "running", 2000)
    w.watcher().poll()
    again = w.watcher()  # a new process: nothing in memory
    again.poll()
    assert len(w.emitted("engine_step")) == 2
    assert again.snapshot()[0].step.stage == "gate"
    w.step("gate.verify", "running", 3000)
    again.poll()
    assert [s["stage"] for s in w.emitted("engine_step")] == ["deliver", "gate", "gate.verify"]


def test_a_rotated_journal_is_read_with_its_older_half(w):
    p = w.watcher()
    w.step("deliver", "delivered", 1000)
    p.poll()
    (w.instance / "run-events.jsonl").rename(w.instance / "run-events.jsonl.1")
    w.step("gate", "running", 2000)
    p.poll()
    assert [s["stage"] for s in w.emitted("engine_step")] == ["deliver", "gate"]


def test_a_new_attempt_starts_from_its_own_journal(w):
    p = w.watcher()
    w.step("deliver", "delivered", 5000)
    p.poll()
    second = w.tmp / "instances" / "acme-2"
    second.mkdir()
    w.attempt("dispatch-2", second)
    w.instance = second
    w.step("deliver", "delivered", 1000, did="dispatch-2")  # earlier ms than the old cursor: still new
    p.poll()
    steps = w.emitted("engine_step")
    assert [(s["stage"], s["attempt"]) for s in steps] == [("deliver", 1), ("deliver", 2)]


def test_missing_engine_files_are_unknown_not_errors(w):
    (w.state / "tasks" / task_key("ACME-1") / "task.json").unlink()
    p = w.watcher()
    p.poll()
    assert w.emitted() == [] and p.snapshot()[0].step is None


def test_a_secret_in_a_note_is_redacted(w):
    w.step("gate.verify", "failed", 1000, note=f"push failed for https://bot:{GL_TOKEN}@gitlab")
    w.watcher().poll()
    assert GL_TOKEN not in w.events_path.read_text()
    assert "<redacted>" in w.emitted("engine_step")[0]["note"]


def test_agent_activity_names_tools_and_descriptions_never_commands(w):
    w.transcript(*[tool("Read", file_path="old")] * 50)  # before the runner looked: history, not activity
    w.step("deliver", "delivered", 1000)
    p = w.watcher()
    p.poll()
    assert w.emitted("agent_activity") == [] and p.snapshot()[0].agent_active_at is not None
    w.transcript(tool("Bash", command=f"curl -H 'PRIVATE-TOKEN: {GL_TOKEN}' https://x", description="Check the API"),
                 tool("Edit", file_path=str(w.workspace / "api" / "views.py"), old_string="a", new_string="b"),
                 {"type": "user", "message": {"content": "tool output"}})
    p.poll()
    [a] = w.emitted("agent_activity")
    assert a["tools"] == 2 and a["by_tool"] == {"Bash": 1, "Edit": 1}
    assert (a["last_tool"], a["last_target"], a["step"]) == ("Edit", "api/views.py", "deliver")
    assert "curl" not in w.events_path.read_text() and GL_TOKEN not in w.events_path.read_text()
    p.poll()  # nothing new in the transcript: no line
    w.transcript(tool("Bash", command="pytest", description="Run the tests"))
    p.poll()  # new, but within a minute of the last line
    assert len(w.emitted("agent_activity")) == 1
    w.clock.t += timedelta(minutes=1)
    p.poll()
    last = w.emitted("agent_activity")[-1]
    assert (last["tools"], last["last_target"]) == (1, "Run the tests")


def test_a_quiet_agent_is_reported_once_and_only_on_its_own_turn(w):
    w.transcript(tool("Read", file_path="x"), mtime=T0)
    w.step("deliver", "delivered", int(T0.timestamp() * 1000))
    p = w.watcher(quiet_after=timedelta(minutes=20))
    p.poll()
    w.clock.t = T0 + timedelta(minutes=25)
    p.poll()
    p.poll()
    [q] = w.emitted("agent_quiet")
    assert (q["minutes"], q["step"]) == (25, "deliver")
    assert p.quiet_seconds(p.snapshot()[0]) == 25 * 60

    # Codex reviewing: the agent is silent by design, nothing to report and nothing to colour red.
    w.step("gate.review", "running", int((T0 + timedelta(minutes=26)).timestamp() * 1000))
    w.clock.t = T0 + timedelta(minutes=60)
    p.poll()
    assert len(w.emitted("agent_quiet")) == 1
    assert p.quiet_seconds(p.snapshot()[0]) is None

    # Sent back to the agent: the quiet clock starts at the new step, not at the old transcript time.
    w.step("gate.review", "retrying", int((T0 + timedelta(minutes=60)).timestamp() * 1000))
    w.step("agent", "nudged", int((T0 + timedelta(minutes=60)).timestamp() * 1000))
    p.poll()
    assert p.quiet_seconds(p.snapshot()[0]) == 0


def test_a_task_that_stops_running_is_dropped(w):
    w.step("deliver", "delivered", 1000)
    p = w.watcher()
    p.poll()
    w.store.update_task("ACME-1", state="needs_input")
    p.poll()
    assert p.snapshot() == []


def test_metrics_show_the_step_activity_and_the_last_tick(w):
    w.transcript(tool("Read", file_path="x"), mtime=T0)
    w.step("gate.verify", "running", 1_792_929_000_000)
    p = w.watcher()
    p.poll()

    class R:
        progress = p
        last_tick = T0.timestamp()

    m = Metrics()
    m.update_progress(R)
    value = m.registry.get_sample_value
    assert value("runner_task_step_since_seconds",
                 {"task_id": "ACME-1", "stage": "gate.verify", "state": "running"}) == 1_792_929_000
    assert value("runner_agent_last_activity_seconds", {"task_id": "ACME-1"}) == T0.timestamp()
    assert value("runner_agent_quiet_seconds", {"task_id": "ACME-1"}) is None  # the verifier has the turn
    assert value("runner_engine_observed_seconds", {"task_id": "ACME-1"}) == T0.timestamp()
    assert value("runner_last_tick_seconds") == T0.timestamp()


def test_engine_state_is_written_when_it_changes_and_not_on_every_tick(w, cfg):
    d = Delivery(cfg, w.store, w.events, None, None, None, None, None)
    task = w.store.task("ACME-1")
    running = {"state": "running", "attempt_live": True, "attempts": [{"n": 1, "kind": "initial"}]}
    for _ in range(3):
        d._note_engine_state(task, running)
    d._note_engine_state(task, {**running, "attempt_live": False})
    d._note_engine_state(task, {"state": "waiting_review", "attempts": running["attempts"]})
    seen = [(e["state"], e["attempt_live"]) for e in w.emitted("engine_state")]
    assert seen == [("running", True), ("running", False), ("waiting_review", False)]
