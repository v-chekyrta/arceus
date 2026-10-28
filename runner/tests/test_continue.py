"""A failed task is not a dead end: `/continue` on the ticket resumes the same task."""

from __future__ import annotations

import json
from datetime import timedelta

from .test_delivery import World, branches, git, no_real_tools, occupy, release, take, w  # noqa: F401  (fixtures)

FAILED = "agent-failed"


def failed(w: World, **settle) -> int:
    """ACME-1 taken, then failed by the engine. Returns the failure's boundary (ms)."""
    take(w)
    w.now += timedelta(minutes=30)
    w.settle("ACME-1", "failed", **({"review": "needs-user", "state_reason": "needs-user"} | settle))
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and FAILED in w.yt.issue_tags["2-1"]
    w.yt.clock = task.failed_at_ms + 1000  # people answer after the failure
    return task.failed_at_ms


def sends(w: World) -> list[dict]:
    return w.calls("worker-send")


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_the_failure_comment_says_how_to_continue(w):
    failed(w)
    [body] = [c for c in w.bot_comments() if "could not take this ticket" in c]
    assert "starts with `/continue`" in body and "branches and commits are kept" in body


def test_continue_resumes_the_same_task_once(w):
    failed(w)
    w.yt.comment("2-1", "j.doe", "/continue The verifier is fixed now, finish the error pages.")
    w.tick()
    w.tick()
    [send] = sends(w)
    assert flag(send["argv"], "--task-id") == "ACME-1" and flag(send["argv"], "--relation") == "continue"
    assert flag(send["argv"], "--message-id").startswith("yt-continue-c-")
    assert flag(send["argv"], "--source") == "youtrack" and flag(send["argv"], "--author") == "j.doe"
    text = send["argv"][-1]
    assert "j.doe asked you to continue this task after it failed" in text
    assert text.endswith("The verifier is fixed now, finish the error pages.") and "/continue" not in text
    assert str(w.cfg.paths.workspace) in send["argv"]

    task = w.store.task("ACME-1")
    assert (task.state, task.reason, task.failed_at_ms) == ("dispatched", None, None)
    assert FAILED not in w.yt.issue_tags["2-1"]
    assert w.yt.issue_fields["2-1"]["State"] == {"name": "In Progress"}
    assert any("picking this up again, as j.doe asked" in c for c in w.bot_comments())
    # The same task in the engine: a new attempt of it, not a new task.
    t = w.engine_state()["tasks"]["ACME-1"]
    assert [a["kind"] for a in t["attempts"]] == ["initial", "continue"] and len(w.calls("dispatch")) == 1

    w.restart()
    for _ in range(3):
        w.tick()
    assert len(sends(w)) == 1  # never twice, across restarts and polls


def test_only_a_new_continue_from_someone_allowed_counts(w):
    boundary = failed(w)
    w.yt.comments["2-1"].append({"id": "c-old", "text": "/continue", "created": boundary - 60_000,
                                 "author": {"login": "j.doe"}})  # before the failure
    w.yt.comment("2-1", "j.doe", "Looks like the verifier again, we'll see")   # talk, not a request
    w.yt.comment("2-1", "mallory", "/continue")                                # not on the list
    w.yt.comment("2-1", w.yt.login, "/continue")                               # the bot itself
    w.yt.comment("2-1", "a.smith", "  ")                                       # empty
    for _ in range(3):
        w.tick()
    assert sends(w) == [] and w.store.task("ACME-1").state == "failed"
    w.yt.comment("2-1", "a.smith", "/CONTINUE")
    w.tick()
    w.tick()
    assert len(sends(w)) == 1 and flag(sends(w)[0]["argv"], "--author") == "a.smith"


def test_a_continue_waits_while_bulava_is_busy_and_is_not_lost(w):
    failed(w)
    w.yt.comment("2-1", "j.doe", "/continue  please retry ")
    w.set_engine(lambda s: s["next_rc"].update({"worker-send": 6}))
    w.tick()  # seen on the YouTrack poll, sent right after: Bulava is busy
    assert w.store.task("ACME-1").state == "failed" and w.store.pending_continues()
    w.restart()
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "dispatched" and not w.store.pending_continues()
    refused, accepted = [s for s in sends(w) if s["argv"]]
    # What was kept across the restart is the request itself: its words, its author and when it was made.
    assert accepted["argv"][-1].endswith("please retry") and flag(accepted["argv"], "--author") == "j.doe"
    [comment] = [c for c in w.yt.comments["2-1"] if c["text"].startswith("/continue")]
    assert task.published_at_ms == comment["created"]


def test_a_pending_continue_goes_before_a_new_ticket(w):
    failed(w)
    w.yt.comment("2-1", "j.doe", "/continue")
    w.yt.add_issue(2, ("b1", "j.doe", "+agent"))
    w.set_engine(lambda s: s["next_rc"].update({"worker-send": 6}))
    w.tick()
    w.tick()
    assert w.store.task("ACME-2") is None  # the queue waits for the resumed task
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched" and w.store.task("ACME-2") is None


def test_the_failed_tag_comes_off_even_if_youtrack_fails_then_and_does_not_come_back(w, monkeypatch):
    from arceus_runner.http import ServiceError
    take(w)
    yt = w.runner.delivery.tracker
    real, added = yt.add_tag, []

    def refuse(db_id, tag):
        raise ServiceError("youtrack", "tag write refused for now")

    monkeypatch.setattr(yt, "add_tag", refuse)  # the failure's tag cannot be set for a while
    w.now += timedelta(minutes=30)
    w.settle("ACME-1", "failed", review="needs-user", state_reason="needs-user")
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and FAILED not in w.yt.issue_tags["2-1"]  # still waiting to be tagged
    w.yt.clock = task.failed_at_ms + 1000
    w.yt.comment("2-1", "j.doe", "/continue")
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    monkeypatch.setattr(yt, "add_tag", lambda db_id, tag: (added.append(tag), real(db_id, tag)))
    for _ in range(3):
        w.tick()
    # The failure's own "add the tag", still pending, was dropped: it is never sent after the continue.
    assert added == [] and FAILED not in w.yt.issue_tags["2-1"]


def test_a_second_failure_after_a_continue_is_tagged_and_said_again(w):
    failed(w)
    w.yt.comment("2-1", "j.doe", "/continue")
    w.tick()
    w.tick()
    assert FAILED not in w.yt.issue_tags["2-1"]
    w.now += timedelta(minutes=30)
    w.settle("ACME-1", "failed", review="needs-user", state_reason="needs-user")
    w.tick()
    assert w.store.task("ACME-1").state == "failed" and FAILED in w.yt.issue_tags["2-1"]
    assert len([c for c in w.bot_comments() if "could not take this ticket" in c]) == 2


def test_uncommitted_work_survives_another_task_and_comes_back(w):
    take(w)
    theme = w.cfg.paths.workspace / "acme-theme"
    w.agent_commits("committed.txt")
    (w.repo / "committed.txt").write_text("changed, not committed\n")
    (w.repo / "untracked.txt").write_text("new file\n")
    (theme / "style.css").write_text("a{}\n")
    w.now += timedelta(minutes=30)
    w.settle("ACME-1", "failed", review="needs-user", state_reason="needs-user")
    w.tick()
    branch1 = w.store.task("ACME-1").branch
    for repo in (w.repo, theme):
        assert git(repo, "status", "--porcelain") == ""
        assert git(repo, "log", "-1", "--format=%s").startswith("WIP [ACME-1]: uncommitted work")
    assert {e["repo"] for e in w.events_named("progress_saved")} == {"acme-platform", "acme-theme"}

    # Another ticket runs in the same workspace meanwhile; the clean trees let it start.
    take(w, 2, summary="Other work")
    task2 = w.store.task("ACME-2")
    assert task2.state == "dispatched"
    (w.repo / "other.txt").write_text("ACME-2 work\n")
    git(w.repo, "add", "other.txt")
    git(w.repo, "commit", "-q", "-m", "ACME-2 work")
    w.settle("ACME-2", "failed", review="needs-user", state_reason="needs-user")
    w.tick()

    w.yt.clock = w.store.task("ACME-1").failed_at_ms + 1000
    w.yt.comment("2-1", "j.doe", "/continue")
    w.tick()
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    for repo in (w.repo, theme, w.cfg.paths.workspace):
        assert git(repo, "symbolic-ref", "--short", "HEAD") == branch1
    assert (w.repo / "untracked.txt").read_text() == "new file\n"
    assert (w.repo / "committed.txt").read_text() == "changed, not committed\n"
    assert (theme / "style.css").read_text() == "a{}\n"
    assert not (w.repo / "other.txt").exists()  # ACME-2's work stays on its own branch


def test_a_task_bulava_never_had_starts_again_on_the_same_branches(w):
    failed(w)
    git(w.repo, "commit", "-q", "--allow-empty", "-m", "kept on the branch")
    w.set_engine(lambda s: s["tasks"].pop("ACME-1"))  # the engine's record is gone
    w.yt.comment("2-1", "j.doe", "/continue")
    w.tick()
    w.tick()
    assert sends(w) == [] and len(w.calls("dispatch")) == 2  # started again from the ticket
    task = w.store.task("ACME-1")
    assert task.state == "dispatched" and FAILED not in w.yt.issue_tags["2-1"]
    assert git(w.repo, "log", "-1", "--format=%s") == "kept on the branch"
    assert any("starting this again from the ticket" in c for c in w.bot_comments())


def test_continue_resets_the_budgets_a_person_overrides(w):
    failed(w)
    w.store.update_task("ACME-1", review_rounds=5, ci_attempts=3)
    w.yt.comment("2-1", "j.doe", "/continue")
    w.tick()
    w.tick()
    task = w.store.task("ACME-1")
    assert (task.state, task.review_rounds, task.ci_attempts) == ("dispatched", 0, 0)


def test_continue_window_is_honoured(w):
    failed(w)
    w.now += w.cfg.limits.continue_window + timedelta(hours=1)
    w.yt.comment("2-1", "j.doe", "/continue")
    for _ in range(3):
        w.tick()
    assert sends(w) == [] and w.store.task("ACME-1").state == "failed"


def test_a_continue_bulava_refuses_is_said_once_and_not_retried(w):
    failed(w)
    w.yt.comment("2-1", "j.doe", "/continue")
    w.set_engine(lambda s: s["next_rc"].update({"worker-send": 3}))
    for _ in range(4):
        w.tick()
    assert len(sends(w)) == 1 and w.store.task("ACME-1").state == "failed"
    assert len([c for c in w.bot_comments() if "the execution engine did not accept the request to continue" in c]) == 1
    assert not w.store.pending_continues()


# -- stalled: the watchdog parked the session; one nudge, then a person ----------------------------

def stall(w: World) -> None:
    w.now += timedelta(minutes=20)
    w.settle("ACME-1", "failed", review="stalled", state_reason="stalled")
    w.tick()


def test_a_stalled_attempt_is_asked_once_to_finish(w):
    take(w)
    stall(w)
    [send] = sends(w)
    assert flag(send["argv"], "--relation") == "continue" and flag(send["argv"], "--message-id") == "stalled-ACME-1-1"
    assert "stopped at its prompt without finishing" in send["argv"][-1]
    task = w.store.task("ACME-1")
    assert task.state == "dispatched" and FAILED not in w.yt.issue_tags["2-1"]
    assert any("asked it once to pick the work up" in c for c in w.bot_comments())
    w.restart()
    w.tick()
    assert len(sends(w)) == 1


def test_a_second_stall_in_a_row_goes_to_a_person(w):
    take(w)
    stall(w)
    stall(w)
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "again after being asked to finish" in task.reason
    assert len(sends(w)) == 1 and FAILED in w.yt.issue_tags["2-1"]
    # A person's /continue, then a stall of that attempt: one nudge again, it is a new streak.
    w.yt.clock = task.failed_at_ms + 1000
    w.yt.comment("2-1", "j.doe", "/continue")
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    stall(w)
    assert w.store.task("ACME-1").state == "dispatched" and len(sends(w)) == 3


def test_a_stall_nudge_waits_while_bulava_is_busy(w):
    take(w)
    w.set_engine(lambda s: s["next_rc"].update({"worker-send": 6}))
    stall(w)
    assert w.store.task("ACME-1").state == "dispatched" and w.engine_state()["tasks"]["ACME-1"]["state"] == "failed"
    w.tick()
    assert w.engine_state()["tasks"]["ACME-1"]["state"] == "running" and len(sends(w)) == 2


def test_a_stall_nudge_waits_while_another_task_holds_the_workspace(w):
    take(w)
    mine = w.store.task("ACME-1").branch
    other = occupy(w)
    stall(w)
    w.tick()
    assert sends(w) == [] and set(branches(w).values()) == {other}
    task = w.store.task("ACME-1")
    assert task.state == "dispatched" and w.store.action("ACME-1:stalled:1") == ("intent", None)
    release(w)
    w.tick()
    [send] = sends(w)
    assert flag(send["argv"], "--message-id") == "stalled-ACME-1-1" and set(branches(w).values()) == {mine}
    assert w.store.task("ACME-1").state == "dispatched"


def test_a_continue_waits_while_another_task_holds_the_workspace(w):
    failed(w)
    mine = w.store.task("ACME-1").branch
    w.yt.comment("2-1", "j.doe", "/continue")
    other = occupy(w)
    w.tick()
    w.tick()
    assert sends(w) == [] and set(branches(w).values()) == {other}
    assert w.store.task("ACME-1").state == "failed" and w.store.pending_continues()
    # Called directly, for a task Bulava lost: it is not taken again either while the workspace is held.
    w.set_engine(lambda s: s["tasks"].pop("ACME-1"))
    [(key, _, payload)] = w.store.pending_continues()
    w.runner.delivery._send_continue(w.store.task("ACME-1"), key, json.loads(payload))
    assert w.store.task("ACME-1").state == "failed" and len(w.calls("dispatch")) == 1
    release(w)
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched" and len(w.calls("dispatch")) == 2
    assert set(branches(w).values()) == {mine}
