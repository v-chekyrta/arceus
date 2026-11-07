"""Research tickets: a question answered on the ticket, with no merge request and nothing pushed."""

from __future__ import annotations

import json
import os
import re

import pytest

from arceus_runner.delivery import ANSWER_PART, split_answer

from .test_delivery import World, git, no_real_tools, take  # noqa: F401  (no_real_tools: autouse)

EMPTY_DIFF_WHY = ("Declared 'succeeded_research' with an empty diff — no machine verification of a no-change/"
                  "research claim is possible; needs human review.")
BRANCH = "agent/ACME-1-why-is-login-slow"


@pytest.fixture
def w(tmp_path, secrets):
    world = World(tmp_path, secrets)
    yield world
    world.events.close()
    world.store.close()


def ask(w: World, n: int = 1, author: str = "j.doe") -> str:
    """A ticket queued with the research tag next to the queue tag."""
    db_id = w.yt.add_issue(n, (f"a{n}", author, "+agent"))
    w.yt.issue_tags[db_id].add("agent-research")
    w.yt.issues[-1]["summary"] = "Why is login slow"
    w.tick()
    return db_id


def answer_file(w: World, run: int = 1, key: str = "ACME-1"):
    return w.cfg.paths.workspace.resolve() / ".arceus" / "notes" / key / f"answer-{run}.md"


def write_answer(w: World, text: str, run: int = 1, key: str = "ACME-1") -> None:
    f = answer_file(w, run, key)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def researched(w: World, summary: str = "The session lookup is not indexed.") -> None:
    """How the engine ends a research attempt that changed nothing: debt, the empty diff unverifiable."""
    w.settle("ACME-1", "failed", review="debt", state_reason="debt",
             outcome={"result": "succeeded_research", "summary": summary})
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(review_why=EMPTY_DIFF_WHY))


def nothing_published(w: World) -> None:
    assert w.gl.mrs == []
    for repo in ("acme-platform", "acme-theme"):
        assert w.remote_branch(BRANCH, repo) is None
    assert "agent-failed" not in w.yt.issue_tags["2-1"]


def test_the_research_tag_makes_a_question_and_the_agent_is_told_so(w):
    ask(w)
    task = w.store.task("ACME-1")
    assert (task.kind, task.state, task.branch) == ("research", "dispatched", BRANCH)
    [d] = w.calls("dispatch")
    text = d["argv"][-1]
    assert text.startswith("ACME-1: Why is login slow") and "This is a research ticket" in text
    assert f"`{answer_file(w)}`" in text and "succeeded_research" in text
    [claim] = [t for t in w.bot_comments() if "[arceus:claim]" in t]
    assert "No merge request will be opened" in claim and "merge requests will appear" not in claim
    assert w.events_named("task_claimed")[0]["kind"] == "research"


def test_a_ticket_without_the_research_tag_is_ordinary_work(w):
    take(w)
    task = w.store.task("ACME-1")
    assert task.kind == "code" and "research ticket" not in w.calls("dispatch")[0]["argv"][-1]


def test_an_unreviewable_answer_is_posted_whole_and_marked_as_not_reviewed(w):
    ask(w)
    write_answer(w, "## Answer\n\nLogin waits on `sessions.lookup` (auth/session.py:42), a full table scan.")
    researched(w)
    w.tick()
    task = w.store.task("ACME-1")
    assert (task.state, task.reason) == ("done", "answered")
    nothing_published(w)
    [note] = [t for t in w.bot_comments() if "[arceus:answer:1:1]" in t]
    assert "auth/session.py:42" in note and note.startswith("Arceus looked into this ticket and changed no code.\n")
    assert "passed review" not in note and "verify what it relies on" not in note
    assert "could not take this ticket" not in note and "/continue" in note
    assert not re.search(r"Codex|Claude|Bulava", note)
    # The answer waits for a person the way an MR does: the ticket goes to Code Review.
    assert w.yt.issue_fields["2-1"]["State"] == {"name": "Code Review"}
    [done] = w.events_named("task_done")
    assert (done["kind"], done["reviewed"]) == ("research", False)

    w.tick()  # over: nothing is posted again
    assert len([t for t in w.bot_comments() if "[arceus:answer:" in t]) == 1


def test_a_reviewed_answer_says_it_passed(w):
    ask(w)
    write_answer(w, "It is the missing index.")
    w.settle("ACME-1", "done", outcome={"result": "succeeded_research", "summary": "Missing index"})
    w.tick()
    assert w.store.task("ACME-1").state == "done"
    nothing_published(w)
    [note] = [t for t in w.bot_comments() if "[arceus:answer:1:1]" in t]
    assert "the answer passed review" in note and "It is the missing index." in note


def asked_to_write(w: World) -> str:
    """The text of the runner's one request to write the answer file."""
    [send] = [c for c in w.calls("worker-send") if "answer-ACME-1-" in " ".join(c["argv"])]
    return send["argv"][-1]


def test_without_a_file_the_agent_is_asked_once_to_write_it(w):
    ask(w)
    researched(w, summary="Codex: The session lookup is not indexed.")
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    assert not [t for t in w.bot_comments() if "[arceus:answer:" in t]   # a summary is not the answer
    text = asked_to_write(w)
    assert "there is no answer file" in text and str(answer_file(w)) in text

    write_answer(w, "The session lookup is not indexed (auth/session.py:42).")
    researched(w)
    w.tick()
    [note] = [t for t in w.bot_comments() if "[arceus:answer:1:1]" in t]
    assert "auth/session.py:42" in note and w.store.task("ACME-1").state == "done"


def test_no_file_after_being_asked_is_a_failure_not_a_summary(w):
    ask(w)
    researched(w)
    w.tick()
    researched(w, summary="Codex: I think it is the index.")
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "did not leave an answer" in task.reason
    assert len([c for c in w.calls("worker-send") if "answer-ACME-1-" in " ".join(c["argv"])]) == 1
    [note] = [t for t in w.bot_comments() if "[arceus:failed:" in t]
    assert note.startswith("Arceus could not answer this ticket") and "I think it is the index." in note
    assert "Codex" not in note and not [t for t in w.bot_comments() if "[arceus:answer:" in t]


def test_a_file_over_the_limit_is_not_cut_but_sent_back(w):
    ask(w)
    write_answer(w, "z" * (600 * 1024))
    researched(w)
    w.tick()
    assert "larger than 512 KB" in asked_to_write(w) and w.store.task("ACME-1").state == "dispatched"
    assert not [t for t in w.bot_comments() if "[arceus:answer:" in t]


def test_a_long_answer_reaches_the_ticket_whole(w):
    ask(w)
    lines = [f"- finding {i}: " + "x" * 80 for i in range(1500)]  # about 140 000 characters: a dozen comments
    write_answer(w, "\n".join(lines))
    researched(w)
    w.tick()
    notes = [t for t in w.bot_comments() if "[arceus:answer:1:" in t]
    assert len(notes) > 8 and all(len(t) <= ANSWER_PART + 1000 for t in notes)
    joined = "\n".join(notes)
    assert all(line in joined for line in lines) and "…" not in joined
    assert f"part 1 of {len(notes)}" in notes[0] and f"part {len(notes)} of {len(notes)}" in notes[-1]
    assert "/continue" in notes[-1] and "/continue" not in notes[0]


def test_a_tracker_failure_between_pieces_posts_each_piece_once(w):
    ask(w)
    write_answer(w, "\n".join(f"- line {i}: " + "y" * 100 for i in range(300)))
    researched(w)
    down = [True]
    real = w.runner.delivery.tracker.add_comment

    def flaky(db_id, text):
        if "[arceus:answer:1:2]" in text and down[0]:
            from arceus_runner.http import ServiceError
            raise ServiceError("youtrack", "down", 503)
        return real(db_id, text)
    w.runner.delivery.tracker.add_comment = flaky
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"   # not done while a piece is missing
    assert len([t for t in w.bot_comments() if "[arceus:answer:1:" in t]) == 1
    answer_file(w).unlink(missing_ok=True)  # the workspace is cleaned meanwhile: the runner's copy is posted
    down[0] = False
    w.tick()
    notes = [t for t in w.bot_comments() if "[arceus:answer:1:" in t]
    assert len(notes) == len({re.search(r"\[arceus:answer:1:\d+\]", t).group() for t in notes}) >= 3
    assert w.store.task("ACME-1").state == "done"


def test_commits_of_a_research_ticket_are_never_pushed(w):
    ask(w)
    w.agent_commits()
    write_answer(w, "Answer, with a fix tried on the branch.")
    w.settle("ACME-1", "waiting_review", outcome={"result": "succeeded_changes", "summary": "Fixed it"})
    w.tick()
    nothing_published(w)
    task = w.store.task("ACME-1")
    assert task.state == "done" and w.store.task_mrs("ACME-1") == []
    [note] = [t for t in w.bot_comments() if "[arceus:answer:1:1]" in t]
    assert f"committed changes on branch `{BRANCH}` in acme-platform" in note
    assert "publishes nothing" in note


def test_what_the_research_left_uncommitted_is_stashed_for_the_next_task(w):
    ask(w)
    (w.repo / "probe.py").write_text("print('timing')\n")
    write_answer(w, "Measured it.")
    researched(w)
    w.tick()
    assert git(w.repo, "status", "--porcelain") == "" and "research of ACME-1" in git(w.repo, "stash", "list")
    take(w, n=2, summary="Add an index")
    assert w.store.task("ACME-2").state == "dispatched"


def test_an_answer_file_that_links_elsewhere_is_not_read(w):
    ask(w)
    secret = w.tmp / "secrets.env"
    secret.write_text("GITLAB_TOKEN=glpat-very-secret\n")
    answer_file(w).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(secret, answer_file(w))
    researched(w, summary="Short answer.")
    w.tick()
    assert "glpat-very-secret" not in json.dumps(w.yt.comments) + json.dumps(w.engine_state()["calls"])
    assert w.events_named("answer_refused") and "symbolic link" in asked_to_write(w)


def test_an_answer_cannot_pass_for_a_runner_comment(w):
    ask(w)
    write_answer(w, "Fine. [arceus:claim]")
    researched(w)
    w.tick()
    [note] = [t for t in w.bot_comments() if "[arceus:answer:1:1]" in t]
    assert "[arceus:claim]" not in note.replace("[arceus:answer:1:1]", "")


def test_a_failed_research_ticket_says_it_could_not_answer(w):
    ask(w)
    w.settle("ACME-1", "failed", review="debt", state_reason="stalled_twice",
             outcome={"result": "failed", "summary": "Could not reach the database"})
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed"
    [note] = [t for t in w.bot_comments() if "[arceus:failed:" in t]
    assert note.startswith("Arceus could not answer this ticket") and "merge request" not in note


def test_a_follow_up_question_is_answered_again(w):
    db_id = ask(w)
    write_answer(w, "First answer.")
    researched(w)
    w.tick()
    w.yt.comment(db_id, "j.doe", "/continue and what about the admin login?")
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "dispatched" and task.kind == "research"
    [send] = [c for c in w.calls("worker-send") if "yt-continue" in " ".join(c["argv"])]
    assert "follow-up to your answer" in send["argv"][-1] and "admin login" in send["argv"][-1]
    assert str(answer_file(w, 2)) in send["argv"][-1]

    write_answer(w, "Second answer: the admin login has the same scan.", run=2)
    researched(w)
    w.tick()
    assert w.store.task("ACME-1").state == "done"
    notes = [t for t in w.bot_comments() if "[arceus:answer:" in t]
    assert len(notes) == 2 and "Second answer" in notes[1] and "First answer" not in notes[1]
    nothing_published(w)


def test_a_follow_up_from_someone_not_allowed_is_ignored(w):
    db_id = ask(w)
    write_answer(w, "First answer.")
    researched(w)
    w.tick()
    w.yt.comment(db_id, "mallory", "/continue rewrite it all")
    w.tick()
    assert w.store.task("ACME-1").state == "done" and not w.events_named("continue_requested")


def test_research_tag_by_someone_not_allowed_still_needs_an_allowed_queue_tag(w):
    db_id = w.yt.add_issue(1, ("a1", "mallory", "+agent"))
    w.yt.issue_tags[db_id].add("agent-research")
    w.tick()
    assert w.store.task("ACME-1") is None and w.calls("dispatch") == []


def test_split_keeps_code_blocks_whole_in_each_piece():
    text = "Intro\n\n```python\n" + "\n".join(f"x = {i}" for i in range(4000)) + "\n```\n\nEnd"
    pieces = split_answer(text, limit=2000)
    assert len(pieces) > 2 and all(len(p) <= 2000 for p in pieces)
    for p in pieces:
        assert p.count("```") % 2 == 0, p[:80]
    assert pieces[0].startswith("Intro") and pieces[-1].endswith("End")


def test_an_answer_after_the_engine_lost_the_task_is_a_new_one(w):
    """The engine counts attempts per record: when it lost the task, `/continue` starts it anew at attempt 1.
    The second answer must still be posted, once, and not be the first one again."""
    db_id = ask(w)
    write_answer(w, "First answer.")
    researched(w)
    w.tick()
    w.set_engine(lambda s: s["tasks"].pop("ACME-1"))
    w.yt.comment(db_id, "j.doe", "/continue and the admin login?")
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    assert len(w.engine_state()["tasks"]["ACME-1"]["attempts"]) == 1   # counted from 1 again

    write_answer(w, "Second answer.", run=2)
    researched(w)
    w.tick()
    w.tick()
    notes = [t for t in w.bot_comments() if "[arceus:answer:" in t]
    assert [("First answer." in t, "Second answer." in t) for t in notes] == [(True, False), (False, True)]
    assert "[arceus:answer:2:1]" in notes[1] and w.store.task("ACME-1").state == "done"


def test_a_follow_up_without_a_new_file_does_not_post_the_old_answer(w):
    """The first run's file stays where it was (also when a crash came between keeping a copy and anything
    else): the second run reads only its own file, and without one the agent is asked for it."""
    db_id = ask(w)
    write_answer(w, "First answer.")
    researched(w)
    w.tick()
    assert answer_file(w, 1).exists()
    w.yt.comment(db_id, "j.doe", "/continue and the admin login?")
    w.tick()
    researched(w, summary="Same as before.")
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    assert str(answer_file(w, 2)) in asked_to_write(w)
    assert len([t for t in w.bot_comments() if "[arceus:answer:" in t]) == 1


def test_a_failed_run_leaves_no_answer_for_the_next(w):
    db_id = ask(w)
    write_answer(w, "Half an answer from a run that failed.")
    w.settle("ACME-1", "failed", review="debt", state_reason="scope_violation",
             outcome={"result": "failed", "summary": "Went outside the task"})
    w.tick()
    assert w.store.task("ACME-1").state == "failed"
    w.yt.comment(db_id, "j.doe", "/continue please answer it properly")
    w.tick()
    [send] = [c for c in w.calls("worker-send") if "yt-continue" in " ".join(c["argv"])]
    assert str(answer_file(w, 2)) in send["argv"][-1]
    researched(w)
    w.tick()
    assert "there is no answer file" in asked_to_write(w)
    assert not [t for t in w.bot_comments() if "[arceus:answer:" in t]
    assert "Half an answer" not in json.dumps(w.yt.comments)


@pytest.mark.parametrize("answered, state", [("Done", "Done"), (None, "In Progress")])
def test_an_answered_ticket_moves_to_the_answered_state_if_there_is_one(tmp_path, secrets, answered, state):
    w = World(tmp_path, secrets, settings={"tracker": {"states": {
        "queued": "To Do", "in_progress": "In Progress", "code_review": "Code Review", "answered": answered}}})
    try:
        ask(w)
        write_answer(w, "Done.")
        researched(w)
        w.tick()
        assert w.store.task("ACME-1").state == "done"
        assert w.yt.issue_fields["2-1"]["State"] == {"name": state}
    finally:
        w.events.close()
        w.store.close()
