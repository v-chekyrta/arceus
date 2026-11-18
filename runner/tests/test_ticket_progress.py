"""The comment that says the agent took a ticket also says, while it works, what it is doing.

Once the runner took a ticket, people could wait for hours without a sign of what the agent was doing; the steps and the
agent's activity were only in Slack and Loki. The claim comment now carries a Progress section the runner edits in place.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import timedelta

import pytest

from arceus_runner.progress import task_key
from arceus_runner.ticket_progress import MARKER, _now_words

from .test_delivery import World, no_real_tools, take  # noqa: F401  (fixture)

DID = "dispatch-1"


@pytest.fixture
def w(tmp_path, secrets):
    world = World(tmp_path, secrets)
    yield world
    world.events.close()
    world.store.close()


def claim(w: World) -> dict:
    [c] = [c for c in w.yt.comments["2-1"] if MARKER in c["text"]]
    return c


def running(w: World) -> Path:
    """The engine's record of the attempt, with its instance folder, as Bulava keeps it."""
    instance = w.tmp / "instances" / "acme-1"
    instance.mkdir(parents=True, exist_ok=True)
    f = w.cfg.paths.bulava_state / "tasks" / task_key("ACME-1") / "task.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"task_id": "ACME-1", "state": "running",
                             "attempts": [{"n": 1, "dispatch_id": DID, "instance": str(instance)}]}))
    return instance


def step(instance, stage: str, state: str, ms: int, note: str = "") -> None:
    with (instance / "run-events.jsonl").open("a") as f:
        f.write(json.dumps({"v": 1, "ms": ms, "dispatch_id": DID, "stage": stage, "state": state,
                            **({"note": note} if note else {})}, ensure_ascii=False) + "\n")


def tool(w: World, instance, description: str, at: str = "2026-10-20T07:01:00Z") -> None:
    (instance / "claude-session-id").write_text("sess-1\n")
    path = w.tmp / "claude-projects" / "-workspace" / "sess-1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": at, "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "secret stuff", "description": description}}]}})
                + "\n")


def watch(w: World) -> None:
    w.runner.progress.projects_dir = w.tmp / "claude-projects"


def later(w: World, minutes: float) -> None:
    w.now += timedelta(minutes=minutes)
    w.tick()


def test_the_claim_comment_says_at_once_what_happens(w):
    take(w)
    c = claim(w)
    text = c["text"]
    assert text.startswith("The agent has taken this ticket: branch `agent/ACME-1")
    assert "\n\n---\n**Progress** · updated " in text
    assert "**Now:** Preparing the brief for the agent." in text
    assert "taken" in text and "the agent started" in text
    assert text.rstrip().endswith(MARKER) and text.count(MARKER) == 1
    assert len([x for x in w.bot_comments() if "**Progress**" in x]) == 1  # the same comment, not a new one


def test_steps_and_the_agents_actions_reach_the_ticket(w):
    take(w)
    watch(w)
    instance = running(w)
    tool(w, instance, "warm up")  # the watcher starts at the transcript's end: earlier work is history
    later(w, 1)
    edits = w.yt.comment_edits
    step(instance, "deliver", "delivered", 1_792_479_660_000, "Сесія підтвердила, що отримала бриф")
    tool(w, instance, "Read the router of @j.doe's `app`")
    later(w, 1)
    text = claim(w)["text"]
    assert w.yt.comment_edits == edits + 1  # a new step: at once, no waiting for the interval
    assert "the brief reached the agent" in text
    assert "**Now:** The agent is working on the change." in text
    # The agent's words are code: no mention, no markup, never the command itself.
    assert "`Read the router of @j.doe's 'app'`" in text and "secret stuff" not in text
    assert "(1 tool call in this attempt)" in text

    # Only new actions: held to the interval.
    tool(w, instance, "Edit the inspection list")
    later(w, 1)
    assert w.yt.comment_edits == edits + 1 and "Edit the inspection list" not in claim(w)["text"]
    later(w, 5)
    assert w.yt.comment_edits == edits + 2 and "`Edit the inspection list`" in claim(w)["text"]

    # Codex's review and its verdict are steps; nothing new is never written.
    step(instance, "gate.review", "running", 1_792_480_000_000)
    later(w, 1)
    assert "**Now:** Codex is reviewing the change." in claim(w)["text"]
    step(instance, "gate.review", "retrying", 1_792_480_100_000, "Повернуто на доопрацювання · зауважень: 6")
    later(w, 1)
    text = claim(w)["text"]
    assert "Codex review: sent back for rework (6 remarks)" in text
    assert "**Now:** The agent is reworking the change after Codex's review." in text
    n = w.yt.comment_edits
    later(w, 30)
    assert w.yt.comment_edits == n


def test_a_restart_does_not_write_the_same_again(w):
    take(w)
    n = w.yt.comment_edits
    w.restart()
    later(w, 10)
    assert w.yt.comment_edits == n
    assert claim(w)["text"].count("**Progress**") == 1


def test_the_end_is_said_once(w):
    take(w)
    w.store.update_task("ACME-1", state="failed", reason="the build never passed")
    later(w, 1)
    text = claim(w)["text"]
    assert "**Now:** Stopped with an error: the reason is in the runner's comment below." in text
    assert "the build never passed" not in text  # said once, in its own comment
    assert text.startswith("The agent has taken this ticket") and text.count("**Progress**") == 1
    n = w.yt.comment_edits
    later(w, 10)
    later(w, 10)
    assert w.yt.comment_edits == n


def test_a_removed_comment_is_left_alone(w):
    take(w)
    w.yt.comments["2-1"] = [c for c in w.yt.comments["2-1"] if MARKER not in c["text"]]
    w.store.update_task("ACME-1", state="publishing")
    later(w, 1)
    assert w.events_named("progress_comment_gone")
    n = len(w.events_named("progress_comment_gone"))
    later(w, 10)
    assert len(w.events_named("progress_comment_gone")) == n and w.store.task("ACME-1").state == "publishing"


def test_off_leaves_the_claim_comment_as_posted(tmp_path, secrets):
    w = World(tmp_path, secrets, settings={"tracker": {"progress_comment": False}})
    try:
        take(w)
        later(w, 10)
        assert w.yt.comment_edits == 0 and "**Progress**" not in claim(w)["text"]
    finally:
        w.events.close()
        w.store.close()


@pytest.mark.parametrize("step, words", [
    (None, "Preparing the brief for the agent."),
    (("compose", "running"), "Preparing the brief for the agent."),
    (("deliver", "done"), "The agent is working on the change."),
    (("gate.verify", "running"), "Building and running the project's checks."),
    (("gate.check", "running"), "Independent checks of the code and its security."),
    (("gate.review", "running"), "Codex is reviewing the change."),
    (("agent", "waiting"), "Paused: the agent waits for its usage limit to reset; it goes on by itself."),
    (("outcome", "declared"), "The attempt is over; the runner takes its result."),
])
def test_what_now_says(w, step, words):
    take(w)
    assert _now_words(w.store.task("ACME-1"), step) == words


def test_the_code_reviewer_reads_the_claim_without_its_progress(tmp_path, secrets):
    from .test_code_review import ask, publish, reviews

    w = World(tmp_path, secrets, settings={"review_agent": {"enabled": True}})
    try:
        publish(w)
        assert "**Progress**" in claim(w)["text"]
        ask(w)
        [call] = reviews(w)
        text = call["argv"][-1]
        assert "The agent has taken this ticket" in text
        assert "**Progress**" not in text and "**Steps:**" not in text
    finally:
        w.events.close()
        w.store.close()


def test_steps_told_before_the_comment_is_read_are_kept(w):
    take(w)
    tp = w.runner.ticket_progress
    del tp.followed["ACME-1"]                       # as on the first look at a task already running
    tp.step("ACME-1", 1, "deliver", "delivered", 1_792_479_660_000, "")
    tp.step("ACME-1.review-1", 1, "gate", "running", 1_792_479_660_000, "")  # a review's own engine task
    later(w, 1)
    assert "the brief reached the agent" in claim(w)["text"]
    assert tp.early == {}


def test_a_restart_keeps_the_agents_latest_actions(w):
    take(w)
    watch(w)
    instance = running(w)
    tool(w, instance, "warm up")
    later(w, 1)
    tool(w, instance, "Read the inspection list")
    later(w, 6)
    assert "(1 tool call in this attempt)" in claim(w)["text"]
    w.restart()
    watch(w)
    later(w, 1)
    tool(w, instance, "Edit the inspection list")
    later(w, 6)
    text = claim(w)["text"]
    assert "`Read the inspection list`" in text and "`Edit the inspection list`" in text
    assert "(2 tool calls in this attempt)" in text


# -- a `/continue`: the section follows the attempt into the runner's newest comment -----------------------

def resumed(w: World) -> None:
    """The task failed and someone asked it to continue: the runner posts "picking this up again"."""
    take(w)
    w.settle("ACME-1", "failed", review="needs-user", state_reason="needs-user")
    w.tick()
    later(w, 1)
    assert w.store.task("ACME-1").state == "failed"
    w.yt.clock = w.store.task("ACME-1").failed_at_ms + 1000
    w.yt.comment("2-1", "j.doe", "/continue the build box works again")
    w.tick()
    w.tick()
    later(w, 1)


def again(w: World) -> dict:
    [c] = [c for c in w.yt.comments["2-1"] if "[arceus:continue:" in c["text"]]
    return c


def test_opp_346_a_continue_writes_progress_where_people_read(w):
    """The third attempt's progress went on in the claim comment, a whole discussion above: nobody found it."""
    resumed(w)
    assert w.store.task("ACME-1").state == "dispatched"
    text = again(w)["text"]
    assert text.startswith("The agent is picking this up again, as j.doe asked.")
    assert "\n\n---\n**Progress** · updated " in text and "picked up again" in text
    assert "taken" not in text.split("**Steps:**", 1)[1]  # this attempt's steps, not the first one's
    assert text.rstrip().endswith("]") and text.count("[arceus:continue:") == 1 and MARKER not in text
    # The claim comment is back as the runner posted it: one Progress on the ticket.
    first = claim(w)["text"]
    assert "**Progress**" not in first and first.rstrip().endswith(MARKER)
    assert len([x for x in w.bot_comments() if "**Progress**" in x]) == 1


def test_an_older_runners_entry_moves_to_the_current_attempt(w):
    """An entry kept before the section followed attempts (no attempt_started) is moved on at the first look."""
    resumed(w)
    tp = w.runner.ticket_progress
    entry = tp.followed["ACME-1"]
    claimed = json.loads(w.store.action("ACME-1:comment:claim")[1])[0]
    # As the deployed runner left it: following the claim comment, which carries the Progress section.
    w.yt.comments["2-1"] = [dict(c, text=c["text"] + "\n\n---\n**Progress** · old") if c["id"] == claimed else c
                            for c in w.yt.comments["2-1"]]
    tp.followed["ACME-1"] = {"comment": claimed, "base": "The agent has taken this ticket", "steps": [],
                             "since": entry["since"], "state": "dispatched"}
    later(w, 1)
    assert tp.followed["ACME-1"]["comment"] == again(w)["id"]
    assert "**Progress**" not in claim(w)["text"] and "**Progress**" in again(w)["text"]


def test_a_continue_says_which_attempt(w):
    resumed(w)
    tp = w.runner.ticket_progress
    tp.followed["ACME-1"]["attempt"] = 2
    tp.followed["ACME-1"]["digest"] = None
    later(w, 6)
    assert "Attempt 2 started " in again(w)["text"]


@pytest.mark.parametrize("step", [None, ("pipeline", "done"), ("compose", "running")])
def test_now_is_the_agents_work_once_it_uses_tools(w, step):
    """The engine says `pipeline done` once the brief is out and nothing more until the gate: the section
    would say "Preparing the brief" for an hour while the agent built and drove an iOS app."""
    take(w)
    assert _now_words(w.store.task("ACME-1"), step, working=True) == "The agent is working on the change."


def test_a_continue_comment_the_tracker_does_not_list_yet_is_waited_for(w, monkeypatch):
    take(w)
    tp = w.runner.ticket_progress
    real = tp.tracker.comments
    lag = {"on": True}
    monkeypatch.setattr(tp.tracker, "comments", lambda db_id: [
        c for c in real(db_id) if not (lag["on"] and "[arceus:continue:" in c.text)])
    w.settle("ACME-1", "failed", review="needs-user", state_reason="needs-user")
    w.tick()
    later(w, 1)
    w.yt.clock = w.store.task("ACME-1").failed_at_ms + 1000
    w.yt.comment("2-1", "j.doe", "/continue")
    w.tick()
    w.tick()
    later(w, 1)
    assert w.store.task("ACME-1").state == "dispatched"
    assert "**Progress**" not in again(w)["text"]  # posted, but the tracker's list lags: not followed yet
    lag["on"] = False
    later(w, 1)
    assert tp.followed["ACME-1"]["comment"] == again(w)["id"]
    assert "**Progress**" in again(w)["text"] and "**Progress**" not in claim(w)["text"]


def test_the_old_comment_is_put_back_later_when_the_tracker_failed_and_keeps_a_persons_edit(w, monkeypatch):
    from arceus_runner.http import ServiceError

    take(w)
    cid = claim(w)["id"]
    w.yt.comments["2-1"] = [dict(c, text="Edited by a person. " + c["text"]) if c["id"] == cid else c
                            for c in w.yt.comments["2-1"]]
    tp = w.runner.ticket_progress
    real = tp.tracker.edit_comment
    down = {"on": True}

    def edit(db_id, comment_id, text):
        if down["on"] and comment_id == cid:
            raise ServiceError("youtrack", "unavailable", 503)
        return real(db_id, comment_id, text)

    monkeypatch.setattr(tp.tracker, "edit_comment", edit)
    w.settle("ACME-1", "failed", review="needs-user", state_reason="needs-user")
    w.tick()
    w.yt.clock = w.store.task("ACME-1").failed_at_ms + 1000
    w.yt.comment("2-1", "j.doe", "/continue")
    w.tick()
    w.tick()
    later(w, 1)
    assert "**Progress**" in again(w)["text"] and "**Progress**" in claim(w)["text"]
    down["on"] = False
    later(w, 1)
    text = claim(w)["text"]
    assert "**Progress**" not in text and text.startswith("Edited by a person. The agent has taken this ticket")
    assert text.rstrip().endswith(MARKER) and "leftover" not in tp.followed["ACME-1"]
