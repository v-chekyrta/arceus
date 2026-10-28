"""The loop after the MR: review notes, CI, conflicts, merges, against fake trackers and real git."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from arceus_runner.http import ServiceError

from .test_delivery import NOW, World, git, take

DEBOUNCE = timedelta(minutes=6)


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])


@pytest.fixture
def w(tmp_path, secrets):
    world = World(tmp_path, secrets)
    yield world
    world.events.close()
    world.store.close()


def published(w: World) -> str:
    """ACME-1 taken, worked, reviewed and published: an open MR, the task in Code Review."""
    take(w)
    sha = w.agent_commits()
    w.settle("ACME-1", "waiting_review", outcome={"summary": "Added the login fix and a test"})
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"
    return sha


def later(w: World, delta: timedelta = DEBOUNCE) -> None:
    w.now += delta
    w.tick()


def ci_signals(w: World) -> list[dict]:
    return [e for e in w.events_named("review_signal") if e.get("kind") == "ci"]


def sends(w: World) -> list[dict]:
    return w.calls("worker-send")


def fix_and_settle(w: World, name: str = "fix.txt", outcome: str = "Renamed as asked") -> str:
    sha = w.agent_commits(name)
    w.settle("ACME-1", "waiting_review", outcome={"summary": outcome})
    w.tick()
    return sha


def test_mr_description_says_what_was_done(w):
    published(w)
    [mr] = w.gl.mrs
    assert "Added the login fix and a test" in mr["description"]
    assert "add feature.txt" in mr["description"]           # the commit list
    assert "/issue/ACME-1" in mr["description"] and "passed review. Arceus pushed the branch" in mr["description"]


def test_a_review_note_goes_to_the_agent_once_after_quiet(w):
    published(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot Please rename this function", path="app/login.py", line=12)
    w.tick()
    assert sends(w) == []                                    # still inside review_debounce
    later(w)
    [send] = sends(w)
    argv = send["argv"]
    assert argv[argv.index("--source") + 1] == "mr" and argv[argv.index("--author") + 1] == "reviewer"
    assert "Please rename this function" in argv[-1] and "app/login.py:12" in argv[-1]
    assert w.store.task("ACME-1").state == "dispatched"
    assert w.yt.issue_fields["2-1"]["State"] == {"name": "In Progress"}
    later(w)
    later(w)
    assert len(sends(w)) == 1                                # a repeated poll sends nothing new


def test_the_fix_is_published_to_the_same_mr_and_the_thread_is_answered(w):
    published(w)
    d = w.gl.note(1, "reviewer", "@acme-agent-bot Please rename this function", path="app/login.py", line=12)
    w.tick()
    later(w)
    sha = fix_and_settle(w)
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and task.review_rounds == 1
    assert w.remote_branch(task.branch) == sha                # fast-forward of the same branch
    assert len(w.gl.mrs) == 1                                 # the same MR
    replies = [n for n in next(x for x in w.gl.discussions[1] if x["id"] == d)["notes"]
               if n["author"]["username"] == w.gl.username]
    assert len(replies) == 1 and sha[:12] in replies[0]["body"]
    assert "add fix.txt" in w.gl.mrs[0]["description"] and "Renamed as asked" in w.gl.mrs[0]["description"]
    assert any("Arceus addressed the review comments" in t for t in w.bot_comments())
    # What the runner posts speaks as Arceus: no model or engine names, no round counters.
    public = [*w.bot_comments(), w.gl.mrs[0]["description"], *(n["body"] for x in w.gl.discussions[1]
                                                               for n in x["notes"] if n["author"]["username"] == w.gl.username)]
    assert not [t for t in public if re.search(r"Codex|Claude|Bulava|Review rounds|CI repair", t)]
    later(w)
    later(w)
    assert len(sends(w)) == 1                                 # the bot's own reply is not a new review


def test_a_late_reply_in_an_old_thread_is_a_new_round(w):
    published(w)
    d = w.gl.note(1, "reviewer", "@acme-agent-bot Rename it")
    w.tick()
    later(w)
    fix_and_settle(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot Still not quite: use snake_case", discussion=d)
    w.tick()
    later(w)
    assert len(sends(w)) == 2 and "use snake_case" in sends(w)[1]["argv"][-1]


def test_busy_keeps_the_batch_and_its_message_id(w):
    published(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot Rename it")
    w.tick()
    w.set_engine(lambda s: s["next_rc"].update({"worker-send": 6}))
    later(w)
    assert w.store.task("ACME-1").state == "code_review" and w.events_named("review_busy")
    later(w)
    accepted = [c for c in sends(w)]
    ids = {c["argv"][c["argv"].index("--message-id") + 1] for c in accepted}
    assert len(accepted) == 2 and len(ids) == 1 and w.store.task("ACME-1").state == "dispatched"


def test_signals_survive_a_restart(w):
    published(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot Rename it")
    w.tick()
    w.restart()
    later(w)
    assert len(sends(w)) == 1


def test_the_current_failed_pipeline_is_repaired_with_its_logs(w):
    sha = published(w)
    mr = w.gl.mrs[0]
    mr["head_pipeline"] = {"id": 77, "status": "running", "sha": sha, "web_url": "https://gl/p/77"}
    later(w)
    later(w)
    assert sends(w) == [] and not ci_signals(w)               # running is not failed
    mr["head_pipeline"] = {"id": 76, "status": "failed", "sha": "0" * 40, "web_url": "https://gl/p/76"}
    later(w)
    later(w)
    assert sends(w) == [] and not ci_signals(w)               # a pipeline of an older commit
    mr["head_pipeline"] = {"id": 77, "status": "failed", "sha": sha, "web_url": "https://gl/p/77"}
    w.gl.jobs[77] = [{"id": 501, "name": "unit-tests", "stage": "test", "web_url": "https://gl/j/501"}]
    w.gl.traces[501] = "collected 12 items\nFAILED tests/test_login.py::test_redirect - AssertionError"
    w.tick()
    later(w)
    [send] = sends(w)
    argv = send["argv"]
    assert argv[argv.index("--source") + 1] == "ci"
    assert "unit-tests" in argv[-1] and "test_login.py::test_redirect" in argv[-1] and "https://gl/p/77" in argv[-1]
    assert w.store.task("ACME-1").ci_attempts == 1
    later(w)
    assert len(sends(w)) == 1                                 # the same pipeline is not repaired twice


def test_ci_repairs_stop_at_the_limit_with_one_escalation(w):
    object.__setattr__(w.cfg.limits, "max_ci_fix_attempts", 1)
    sha = published(w)
    for pid in (80, 81):
        w.gl.mrs[0]["head_pipeline"] = {"id": pid, "status": "failed", "sha": sha, "web_url": f"https://gl/p/{pid}"}
        w.tick()
        later(w)
        if pid == 80:
            sha = fix_and_settle(w)
    task = w.store.task("ACME-1")
    assert len(sends(w)) == 1 and task.state == "failed" and "CI was still failing" in task.reason
    [note] = [t for t in w.bot_comments() if "CI was still failing" in t]
    assert not re.search(r"\d+ attempts|\(\d+\)", note)       # no counters on the ticket
    [hit] = w.events_named("limit_reached")
    assert (hit["limit"], hit["used"], hit["max"]) == ("ci_fix_attempts", 1, 1)


def test_review_rounds_stop_at_the_limit(w):
    object.__setattr__(w.cfg.limits, "max_review_rounds", 1)
    published(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot one")
    w.tick()
    later(w)
    fix_and_settle(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot two")
    w.tick()
    later(w)
    task = w.store.task("ACME-1")
    assert len(sends(w)) == 1 and task.state == "failed" and "review rounds allowed" in task.reason
    [note] = [t for t in w.bot_comments() if "review rounds allowed" in t]
    assert not re.search(r"\(\d+\)|rounds: \d", note)
    [hit] = w.events_named("limit_reached")
    assert (hit["limit"], hit["used"], hit["max"]) == ("review_rounds", 1, 1)


def test_only_a_confirmed_conflict_is_repaired(w):
    published(w)
    w.gl.mrs[0].update(detailed_merge_status="checking", sha="abc")
    later(w)
    later(w)
    assert sends(w) == [] and not [e for e in w.events_named("review_signal") if e.get("kind") == "conflict"]
    w.gl.mrs[0].update(detailed_merge_status="conflict")
    w.tick()
    later(w)
    [send] = sends(w)
    assert "conflicts with main" in send["argv"][-1] and "git merge origin/main" in send["argv"][-1]


def test_two_mrs_need_both_merged(w):
    take(w)
    theme = w.cfg.paths.workspace / "acme-theme"
    w.agent_commits()
    base_theme = git(theme, "rev-parse", "HEAD")
    (theme / "s.css").write_text("a{}\n")
    git(theme, "add", "s.css")
    git(theme, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "style")
    task = w.store.task("ACME-1")

    def fn(s):
        t = s["tasks"]["ACME-1"]
        t.update(state="waiting_review", attempt_live=False)
        t["receipt"] = {"review": "passed", "repos": [
            {"path": str(w.repo), "branch": task.branch,
             "base_sha": next(r["base_sha"] for r in t["repos"] if r["path"] == str(w.repo)),
             "reviewed_sha": git(w.repo, "rev-parse", "HEAD"), "clean": True},
            {"path": str(theme), "branch": task.branch, "base_sha": base_theme,
             "reviewed_sha": git(theme, "rev-parse", "HEAD"), "clean": True}]}
    w.set_engine(fn)
    w.tick()
    assert len(w.gl.mrs) == 2 and all("Other MRs of this task" in m["description"] for m in w.gl.mrs[1:])
    w.gl.mrs[0]["state"] = "merged"
    later(w)
    assert w.store.task("ACME-1").state == "code_review"
    w.gl.mrs[1]["state"] = "merged"
    later(w)
    assert w.store.task("ACME-1").state == "done" and any("are merged" in t for t in w.bot_comments())


def test_a_follow_up_without_changes_answers_and_stays_in_review(w):
    published(w)
    d = w.gl.note(1, "reviewer", "@acme-agent-bot Why not use the cache here?")
    w.tick()
    later(w)
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="done", attempt_live=False, outcome={"summary": "The cache would serve stale sessions"}))
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "code_review"
    reply = next(x for x in w.gl.discussions[1] if x["id"] == d)["notes"][-1]
    assert reply["author"]["username"] == w.gl.username and "stale sessions" in reply["body"]


def test_a_person_pushing_to_the_mr_branch_is_caught_up_before_the_fix(w):
    published(w)
    branch = w.store.task("ACME-1").branch
    other = w.tmp / "colleague"
    subprocess.run(["git", "clone", "-q", "-b", branch, str(w.origin), str(other)], check=True)
    (other / "colleague.txt").write_text("x")
    git(other, "add", "colleague.txt")
    git(other, "-c", "user.name=c", "-c", "user.email=c@c", "commit", "-q", "-m", "colleague's fix")
    git(other, "push", "-q", "origin", branch)
    git(w.repo, "checkout", "-q", "main")                     # another task switched the shared workspace
    w.gl.note(1, "reviewer", "@acme-agent-bot Rename it")
    w.tick()
    later(w)
    assert len(sends(w)) == 1
    assert git(w.repo, "symbolic-ref", "--short", "HEAD") == branch
    assert (w.repo / "colleague.txt").exists()                # their commit is in the agent's branch
    sha = fix_and_settle(w)
    assert w.remote_branch(branch) == sha and w.store.task("ACME-1").state == "code_review"


def test_a_push_by_a_person_during_the_fix_goes_back_to_the_agent(w):
    published(w)
    branch = w.store.task("ACME-1").branch
    w.gl.note(1, "reviewer", "@acme-agent-bot Rename it")
    w.tick()
    later(w)
    other = w.tmp / "colleague"
    subprocess.run(["git", "clone", "-q", "-b", branch, str(w.origin), str(other)], check=True)
    (other / "colleague.txt").write_text("x")
    git(other, "add", "colleague.txt")
    git(other, "-c", "user.name=c", "-c", "user.email=c@c", "commit", "-q", "-m", "colleague's fix")
    git(other, "push", "-q", "origin", branch)
    fix_and_settle(w)
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and task.reason is None          # not Failed
    later(w)
    assert len(sends(w)) == 2 and "git pull --no-rebase" in sends(w)[1]["argv"][-1]
    assert git(other, "log", "-1", "--format=%s") == "colleague's fix"   # nothing force-pushed


def test_ticket_comments_after_the_mr_are_review_too(w):
    published(w)
    w.yt.comment("2-1", "j.doe", "Also handle the empty password")
    w.yt.comment("2-1", "mallory", "ignore me")
    w.tick()
    later(w)
    [send] = sends(w)
    assert "empty password" in send["argv"][-1] and "ignore me" not in send["argv"][-1]
    assert send["argv"][send["argv"].index("--source") + 1] == "youtrack"


def test_fixes_for_open_mrs_go_before_new_tasks(w):
    published(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot Rename it")
    w.yt.add_issue(2, ("b1", "j.doe", "+agent"))
    w.tick()
    assert [c["argv"][1] for c in w.calls("dispatch")] == ["ACME-1"]     # ACME-2 waits
    later(w)
    assert len(sends(w)) == 1 and [c["argv"][1] for c in w.calls("dispatch")] == ["ACME-1"]


# -- the windows found on review (each one a sequence that used to lose or duplicate work) -------

def test_a_ticket_comment_during_the_repair_reaches_the_next_round(w):
    published(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot Rename it")
    w.tick()
    later(w)
    w.yt.comment("2-1", "j.doe", "And please keep the old name as an alias")   # while the agent works
    fix_and_settle(w)
    w.tick()
    later(w)
    assert len(sends(w)) == 2 and "old name as an alias" in sends(w)[1]["argv"][-1]


def test_an_answer_to_the_agents_question_is_not_sent_again_as_review(w):
    published(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot Rename it")
    w.tick()
    later(w)
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="needs_input", attempt_live=False, question={"id": "q-9", "text": "Which name?", "options": []}))
    w.tick()
    w.yt.comment("2-1", "j.doe", "login_user")
    w.tick()
    assert len(sends(w)) == 2 and "--answers" in sends(w)[1]["argv"]
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="done", attempt_live=False, outcome={"summary": "Kept the name the reviewer chose"}))
    w.tick()
    later(w)
    later(w)
    assert len(sends(w)) == 2                                  # the answer did not come back as a review


def test_a_crash_after_the_thread_reply_does_not_reply_twice(w, monkeypatch):
    published(w)
    d = w.gl.note(1, "reviewer", "@acme-agent-bot Rename it")
    w.tick()
    later(w)
    real_done = w.store.action_done

    def die_on_reply(key, result=None):
        if ":reply:" in key:
            raise RuntimeError("killed right after the reply was posted")
        return real_done(key, result)

    monkeypatch.setattr(w.store, "action_done", die_on_reply)
    w.agent_commits("fix.txt")
    w.settle("ACME-1", "waiting_review", outcome={"summary": "renamed"})
    with pytest.raises(RuntimeError, match="killed"):
        w.tick()
    monkeypatch.setattr(w.store, "action_done", real_done)
    w.restart()
    w.clock.advance(3600)
    w.tick()
    bot = [n for n in next(x for x in w.gl.discussions[1] if x["id"] == d)["notes"]
           if n["author"]["username"] == w.gl.username]
    assert len(bot) == 1 and w.store.task("ACME-1").state == "code_review"


def test_a_repair_waits_while_another_task_holds_the_workspace(w):
    published(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot Rename it")
    w.store.update_task("ACME-1", state="code_review")
    other = w.yt.add_issue(2, ("b1", "j.doe", "+agent"))
    w.store.create_task(task_id="ACME-2", issue_db_id=other, state="dispatched", summary="other", tag_author="j.doe",
                        repo="acme-platform", repo_path=str(w.repo), branch="agent/ACME-2-other", base_branch="main",
                        question_id=None, question_at_ms=None, reason=None, mr_urls="[]", attempt_started=None,
                        extra_repos="[]")
    git(w.repo, "checkout", "-q", "-b", "agent/ACME-2-other", "origin/main")

    def running(s):
        s["tasks"]["ACME-2"] = {**s["tasks"]["ACME-1"], "task_id": "ACME-2", "state": "running", "attempt_live": True,
                                "branch": "agent/ACME-2-other", "attempts": [{"n": 1, "kind": "initial",
                                "started_at": datetime.now(UTC).isoformat(), "ended_at": None}]}
    w.set_engine(running)
    w.tick()
    later(w)
    later(w)
    assert sends(w) == [] and git(w.repo, "symbolic-ref", "--short", "HEAD") == "agent/ACME-2-other"
    assert not w.events_named("review_busy", "review_batch") and w.store.open_batch("ACME-1") is None
    # Called directly with a batch, the send waits too, and the batch stays open.
    ids = [x.id for x in w.store.signals("ACME-1", unbatched=True)]
    batch = w.store.make_batch("review-direct", "ACME-1", "mr", "reviewer", "Rename it", False,
                               w.now.isoformat(), ids, authors=["reviewer"])
    w.runner.review._send(w.store.task("ACME-1"), batch)
    assert sends(w) == [] and git(w.repo, "symbolic-ref", "--short", "HEAD") == "agent/ACME-2-other"
    assert w.store.open_batch("ACME-1").id == "review-direct"
    assert len([e for e in w.events_named("followup_waits") if e["task_id"] == "ACME-1"]) == 1

    w.store.update_task("ACME-2", state="done")              # the other task is over: the round goes now
    later(w)
    [send] = sends(w)
    assert send["argv"][send["argv"].index("--message-id") + 1] == "review-direct"
    assert "Rename it" in send["argv"][-1] and w.store.task("ACME-1").state == "dispatched"
    assert git(w.repo, "symbolic-ref", "--short", "HEAD") == w.store.task("ACME-1").branch


def test_red_ci_that_turns_green_while_waiting_is_not_sent(w):
    sha = published(w)
    w.gl.mrs[0]["head_pipeline"] = {"id": 90, "status": "failed", "sha": sha, "web_url": "https://gl/p/90"}
    w.tick()
    w.set_engine(lambda s: s["next_rc"].update({"worker-send": 6}))
    later(w)                                                   # batch made, engine busy
    w.gl.mrs[0]["head_pipeline"] = {"id": 90, "status": "success", "sha": sha, "web_url": "https://gl/p/90"}
    later(w)
    later(w)
    assert len(sends(w)) == 1 and w.events_named("review_batch_dropped")
    assert w.store.task("ACME-1").state == "code_review"


def test_the_ticket_goes_back_to_in_progress_even_if_youtrack_fails_then(w):
    published(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot Rename it")
    w.tick()
    w.yt.fail_once[("POST", "/api/issues/2-1")] = 503
    later(w)
    assert w.store.task("ACME-1").state == "dispatched"
    assert w.yt.issue_fields["2-1"]["State"] == {"name": "Code Review"}
    w.clock.advance(3600)
    w.tick()
    assert w.yt.issue_fields["2-1"]["State"] == {"name": "In Progress"}


def test_a_persons_commit_on_top_of_the_reviewed_fix_is_kept_not_failed(w, monkeypatch):
    published(w)
    branch = w.store.task("ACME-1").branch
    w.gl.note(1, "reviewer", "@acme-agent-bot Rename it")
    w.tick()
    later(w)
    sha = w.agent_commits("fix.txt")
    git(w.repo, "push", "-q", "origin", f"{sha}:refs/heads/{branch}")
    other = w.tmp / "colleague"
    subprocess.run(["git", "clone", "-q", "-b", branch, str(w.origin), str(other)], check=True)
    (other / "more.txt").write_text("x")
    git(other, "add", "more.txt")
    git(other, "-c", "user.name=c", "-c", "user.email=c@c", "commit", "-q", "-m", "more by a person")
    git(other, "push", "-q", "origin", branch)
    w.settle("ACME-1", "waiting_review", outcome={"summary": "fixed"})
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and task.reason is None and w.events_named("branch_ahead_of_review")
    assert git(other, "log", "-1", "--format=%s") == "more by a person"


def test_a_mixed_batch_spends_the_ci_budget_too(w):
    object.__setattr__(w.cfg.limits, "max_ci_fix_attempts", 1)
    sha = published(w)
    w.gl.mrs[0]["head_pipeline"] = {"id": 91, "status": "failed", "sha": sha, "web_url": "https://gl/p/91"}
    w.tick()
    later(w)
    sha = fix_and_settle(w)
    w.gl.mrs[0]["head_pipeline"] = {"id": 92, "status": "failed", "sha": sha, "web_url": "https://gl/p/92"}
    w.gl.note(1, "reviewer", "@acme-agent-bot and rename it")
    w.tick()
    later(w)
    task = w.store.task("ACME-1")
    assert len(sends(w)) == 1 and task.state == "failed" and "CI was still failing" in task.reason


def test_a_ticket_moved_on_by_hand_stops_watching_but_is_not_done(w):
    published(w)
    w.yt.issue_fields["2-1"]["State"] = {"name": "Testing"}
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "stopped" and "Testing" in task.reason


def test_screenshots_from_a_round_without_code_changes_reach_the_mr_and_the_ticket(w):
    from .test_delivery import PNG, WEBM, _media
    published(w)
    desc_before = w.gl.mr(1)["description"]
    assert "Screenshots" not in desc_before
    w.gl.note(1, "reviewer", "@acme-agent-bot Please attach screenshots of the error pages")
    w.tick()
    later(w)
    folder = _media(w, [{"file": "404.png", "caption": "404 page"}], {"404.png": PNG})
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="done", attempt_live=False, outcome={"summary": "Added screenshots, no code change"}))
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"
    desc = w.gl.mr(1)["description"]
    assert desc.count("<!-- arceus:media -->") == 1 and "![404 page](/uploads/" in desc
    assert desc.index("## Screenshots and recordings") < desc.index("## Checks")
    [note] = [c for c in w.yt.comments["2-1"] if "did not change the code" in c["text"]]
    assert [n.split("-", 1)[1] for n, _ in w.yt.attachments[note["id"]]] == ["404.png"]

    # The next such round replaces the section in place and attaches only what is new.
    w.gl.note(1, "reviewer", "@acme-agent-bot And a recording of the retry, please")
    later(w)
    later(w)
    (folder / "retry.webm").write_bytes(WEBM)
    (folder / "media.json").write_text(
        '{"items": [{"file": "404.png", "caption": "404 page"}, {"file": "retry.webm", "caption": "Retry"}]}')
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="done", attempt_live=False, outcome={"summary": "Recorded the retry"}))
    w.tick()
    desc = w.gl.mr(1)["description"]
    assert desc.count("<!-- arceus:media -->") == 1 and desc.count("![404 page]") == 1 and "![Retry](" in desc
    assert [n.split("-", 1)[1] for _, n, _ in w.gl.uploads] == ["404.png", "retry.webm"]
    attached = [n.split("-", 1)[1] for files in w.yt.attachments.values() for n, _ in files]
    assert attached == ["404.png", "retry.webm"]


def test_a_no_change_answer_that_bulava_banks_as_debt_stays_in_review(w):
    """A multi-repository task: Bulava sees an empty diff in the home and banks a no-change answer as debt."""
    from .test_delivery import PNG, _media
    published(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot Please attach a screenshot")
    w.tick()
    later(w)
    _media(w, [{"file": "a.png", "caption": "The page"}], {"a.png": PNG})
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="failed", state_reason="debt", attempt_live=False,
        outcome={"result": "succeeded_no_change", "summary": "Screenshot added"}))
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and not w.events_named("task_failed")
    assert "![The page](/uploads/" in w.gl.mr(1)["description"]


# -- threads addressed to the bot: the whole thread in, one answer per thread out -----------------

BOT = "@acme-agent-bot"


def thread(w: World, d: str) -> list[dict]:
    return next(x for x in w.gl.discussions[1] if x["id"] == d)["notes"]


def bot_replies(w: World, d: str) -> list[str]:
    return [n["body"] for n in thread(w, d) if n["author"]["username"] == w.gl.username]


def answer(w: World, *entries: dict) -> None:
    """The agent's answers for the round it was just sent, where the follow-up told it to write them."""
    from arceus_runner.replies import replies_file
    argv = sends(w)[-1]["argv"]
    f = replies_file(w.cfg.paths.workspace.resolve(), "ACME-1", argv[argv.index("--message-id") + 1])
    assert str(f) in argv[-1]
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"replies": list(entries)}))


def no_change(w: World, summary: str = "Answered the question") -> None:
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(state="done", attempt_live=False, outcome={"summary": summary}))
    w.tick()


def test_only_a_note_that_mentions_the_bot_wakes_it(w):
    published(w)
    w.gl.note(1, "reviewer", "Looks good to me, but let's ask Anna")
    w.gl.note(1, "anna", "Mail me at ops@acme-agent-bot.example or ping @acme-agent-bot-old")
    w.gl.note(1, "reviewer", "`@acme-agent-bot2` is the staging one")
    later(w)
    later(w)
    assert sends(w) == [] and not w.events_named("review_signal")
    w.gl.note(1, "reviewer", "@ACME-Agent-Bot, why a dict here?")      # GitLab handles are case-insensitive
    later(w)
    later(w)
    [send] = sends(w)
    assert "why a dict here?" in send["argv"][-1] and "ask Anna" not in send["argv"][-1]


def test_the_agent_gets_the_whole_thread_with_the_new_note_marked(w):
    published(w)
    d = w.gl.note(1, "reviewer", "Why do the retries live in the client?", path="app/api.py", line=42,
                  commit="3f2a9c1e5b7d" + "0" * 28)
    w.gl.note(1, "anna", "I wondered the same", discussion=d)
    w.gl.note(1, w.gl.username, "Fixed in 9c1e00000000.\n\n<!-- arceus:note-0-1 -->",
              discussion=d)
    w.gl.note(1, "reviewer", f"{BOT} I meant the choice of approach, not a fix", discussion=d)
    later(w)
    later(w)
    [send] = sends(w)
    text = send["argv"][-1]
    assert "### T1 · acme-platform · app/api.py:42 (on commit 3f2a9c1e5b7d)" in text
    assert text.index("- reviewer: Why do the retries") < text.index("- anna: I wondered the same") \
        < text.index("- you (the runner posted this for you): Fixed in 9c1e") \
        < text.index("- NEW, to you: reviewer: @acme-agent-bot I meant the choice")
    assert "arceus:note" not in text                                   # the runner's markers stay out
    assert "do not change code for it" in text and "decisions.md" in text


def test_a_question_is_answered_in_its_own_thread_without_a_commit(w):
    sha = published(w)
    d = w.gl.note(1, "reviewer", f"{BOT} why not keep the sessions in the cache?")
    later(w)
    later(w)
    answer(w, {"thread": "T1", "changed": False,
               "text": "The cache is per process, so a second worker would serve a stale session."})
    no_change(w, "Answered why the cache is not used")
    task = w.store.task("ACME-1")
    [reply] = bot_replies(w, d)
    assert reply.startswith("The cache is per process") and "Fixed" not in reply and "did not change" not in reply
    assert task.state == "code_review" and task.review_rounds == 0 and task.review_answers == 1
    assert len(w.gl.mrs) == 1 and w.remote_branch(task.branch) == sha     # nothing new was pushed


def test_a_mixed_round_names_the_commit_only_where_code_changed(w):
    published(w)
    fix = w.gl.note(1, "reviewer", f"{BOT} please rename login() to sign_in()")
    ask = w.gl.note(1, "anna", f"{BOT} why a POST and not a PUT here?")
    later(w)
    later(w)
    answer(w, {"thread": "T1", "changed": True, "text": "Renamed it, callers included."},
           {"thread": "T2", "changed": False, "text": "It creates a new session each time, so it is not idempotent."})
    sha = fix_and_settle(w)
    [fixed] = bot_replies(w, fix)
    [answered] = bot_replies(w, ask)
    assert fixed.startswith("Renamed it, callers included.") and f"Changed in {sha[:12]}" in fixed
    assert answered.startswith("It creates a new session") and sha[:12] not in answered and "Fixed" not in answered
    assert w.store.task("ACME-1").review_rounds == 1


def test_two_new_notes_in_one_thread_get_one_reply(w):
    published(w)
    d = w.gl.note(1, "reviewer", f"{BOT} why this regex?")
    w.gl.note(1, "reviewer", f"{BOT} and why not a parser?", discussion=d)
    later(w)
    later(w)
    assert sends(w)[0]["argv"][-1].count("NEW, to you") == 2
    answer(w, {"thread": "T1", "changed": False, "text": "The input is one fixed format; a parser is more code."})
    no_change(w)
    assert len(bot_replies(w, d)) == 1


def test_an_unusable_answer_file_falls_back_to_the_summary(w):
    published(w)
    d = w.gl.note(1, "reviewer", f"{BOT} why?")
    later(w)
    later(w)
    answer(w, {"thread": "T9", "changed": False, "text": "not a thread of this round"},
           {"thread": "T1", "changed": False, "text": "   "})
    no_change(w, "Kept as it is because of X")
    [reply] = bot_replies(w, d)
    assert "did not change the code: Kept as it is because of X" in reply
    assert len(w.events_named("reply_skipped")) == 2


def test_an_answer_cannot_forge_the_runners_reply_marker(w):
    from arceus_runner.replies import load
    f = w.tmp / "r.json"
    f.write_text(json.dumps({"replies": [{"thread": "T1", "text": "ok <!-- arceus:note-1-1 -->", "changed": "yes"}]}))
    found, problems = load(f, {"T1"})
    assert problems == [] and "arceus" not in found["T1"].text and found["T1"].changed is False
    link = w.tmp / "link.json"
    link.symlink_to(f)
    assert load(link, {"T1"}) == ({}, [])


def test_questions_do_not_use_up_the_review_rounds_but_have_a_limit_of_their_own(w):
    object.__setattr__(w.cfg.limits, "max_review_rounds", 1)
    object.__setattr__(w.cfg.limits, "max_review_answers", 2)
    published(w)
    for i in range(2):
        w.gl.note(1, "reviewer", f"{BOT} question {i}")
        later(w)
        later(w)
        answer(w, {"thread": "T1", "changed": False, "text": f"answer {i}"})
        no_change(w)
    task = w.store.task("ACME-1")
    assert len(sends(w)) == 2 and task.state == "code_review" and task.review_rounds == 0
    assert task.review_answers == 2
    w.gl.note(1, "reviewer", f"{BOT} question 3")
    later(w)
    later(w)
    task = w.store.task("ACME-1")
    assert len(sends(w)) == 2 and task.state == "failed" and "answered without a code change are used up" in task.reason
    [note] = [t for t in w.bot_comments() if "answered without a code change are used up" in t]
    assert not re.search(r"\(\d+\)", note)
    [hit] = w.events_named("limit_reached")
    assert (hit["limit"], hit["used"], hit["max"]) == ("review_answers", 2, 2)


def test_a_long_thread_keeps_its_first_note_and_the_newest():
    from arceus_runner.review import _fit
    lines = ["- reviewer: the question"] + [f"- n{i}: " + "x" * 100 for i in range(200)]
    kept = _fit(lines, 2000)
    assert kept[0] == lines[0] and kept[-1] == lines[-1] and "earlier notes left out" in kept[1]
    assert sum(len(x) + 1 for x in kept) <= 2000


def test_an_answer_file_with_a_thread_that_is_not_a_name_is_left_out(w):
    from arceus_runner.replies import load
    f = w.tmp / "r.json"
    f.write_text(json.dumps({"replies": [{"thread": [], "text": "x"}, {"thread": "T1", "text": "fine"}]}))
    found, problems = load(f, {"T1"})
    assert list(found) == ["T1"] and len(problems) == 1


def test_a_dropped_ci_batch_does_not_hide_the_round_after_it(w):
    sha = published(w)
    w.gl.mrs[0]["head_pipeline"] = {"id": 95, "status": "failed", "sha": sha, "web_url": "https://gl/p/95"}
    w.tick()
    w.set_engine(lambda s: s["next_rc"].update({"worker-send": 6}))
    later(w)                                                   # CI batch made, engine busy
    w.gl.mrs[0]["head_pipeline"] = {"id": 95, "status": "success", "sha": sha, "web_url": "https://gl/p/95"}
    w.set_engine(lambda s: s["next_rc"].pop("worker-send", None))
    later(w)
    assert w.events_named("review_batch_dropped")
    d = w.gl.note(1, "reviewer", f"{BOT} why this name?")
    later(w)
    later(w)
    answer(w, {"thread": "T1", "changed": False, "text": "It matches the API it wraps."})
    no_change(w)
    assert bot_replies(w, d) and bot_replies(w, d)[0].startswith("It matches the API")
    assert w.store.task("ACME-1").review_answers == 1


def test_catching_up_is_logged_only_when_something_came_from_gitlab(w):
    published(w)
    d, task = w.runner.delivery, w.store.task("ACME-1")
    w.store.update_task("ACME-1", state="code_review")
    assert d._followup_ready(task)                            # same as GitLab
    w.agent_commits("local.txt")
    assert d._followup_ready(task)                            # ahead of GitLab: nothing came
    assert not w.events_named("branch_caught_up")

    git(w.repo, "reset", "-q", "--hard", f"origin/{task.branch}")
    other = w.tmp / "someone"
    subprocess.run(["git", "clone", "-q", "-b", task.branch, str(w.origin), str(other)], check=True)
    (other / "pushed.txt").write_text("a person's fix\n")
    git(other, "add", "pushed.txt")
    git(other, "-c", "user.name=p", "-c", "user.email=p@p", "commit", "-q", "-m", "a person's fix")
    git(other, "push", "-q", "origin", task.branch)
    assert d._followup_ready(task)
    assert d._followup_ready(task)
    assert [(e["repo"], e["result"]) for e in w.events_named("branch_caught_up")] == [("acme-platform", "forwarded")]


def commit(repo, name: str, text: str = "x\n") -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"change {name}")
    return git(repo, "rev-parse", "HEAD")


def two_mrs(w: World) -> tuple[str, str]:
    """ACME-1 published with an MR in each repository: (platform head, branch)."""
    take(w)
    platform_sha = w.agent_commits()
    commit(w.cfg.paths.workspace / "acme-theme", "s.css")
    w.settle("ACME-1", "waiting_review")
    w.tick()
    assert [m["project_id"] for m in w.gl.mrs] == [0, 1] and w.store.task("ACME-1").state == "code_review"
    return platform_sha, w.store.task("ACME-1").branch


def close(w: World, i: int, by: str = "reviewer") -> None:
    w.yt.clock += 1  # one clock for both trackers: what people do next comes after it
    at = datetime.fromtimestamp(w.yt.clock / 1000, UTC).isoformat().replace("+00:00", "Z")
    w.gl.mrs[i].update(state="closed", closed_by={"username": by}, closed_at=at)


def test_an_mr_a_person_closes_goes_to_the_agent_and_the_rest_goes_on(w):
    platform_sha, branch = two_mrs(w)
    close(w, 0)
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and task.reason is None
    later(w)
    [send] = sends(w)
    text = send["argv"][-1]
    assert "reviewer closed the MR in acme-platform without merging it" in text and "git revert" in text

    git(w.repo, "revert", "--no-edit", "HEAD")
    theme_sha = commit(w.cfg.paths.workspace / "acme-theme", "s.css", "b{}\n")
    w.settle("ACME-1", "waiting_review")
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and task.reason is None
    assert [m.repo for m in w.store.task_mrs("ACME-1")] == ["acme-theme"]
    assert json.loads(task.mr_urls) == [w.gl.mrs[1]["web_url"]]
    assert len(w.gl.mrs) == 2 and w.gl.mrs[0]["state"] == "closed"           # nothing reopened or opened
    assert w.remote_branch(branch) == platform_sha                            # the revert was not pushed
    assert w.remote_branch(branch, "acme-theme") == theme_sha
    assert "Other MRs of this task" not in w.gl.mrs[1]["description"]
    last = w.bot_comments()[-1]
    assert w.gl.mrs[1]["web_url"] in last and last.index(w.gl.mrs[0]["web_url"]) > last.index("No longer part")

    later(w)
    w.restart()
    later(w)
    assert w.store.task("ACME-1").state == "code_review" and len(sends(w)) == 1
    assert not [c for c in w.bot_comments() if "could not take this ticket" in c]
    w.gl.mrs[1]["state"] = "merged"
    later(w)
    assert w.store.task("ACME-1").state == "done"


def test_the_agent_taking_a_repository_out_closes_its_mr_itself(w):
    platform_sha, branch = two_mrs(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot The platform part is not needed, drop it")
    w.tick()
    later(w)
    assert len(sends(w)) == 1
    git(w.repo, "revert", "--no-edit", "HEAD")
    commit(w.cfg.paths.workspace / "acme-theme", "s.css", "b{}\n")
    w.settle("ACME-1", "waiting_review")
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and [m.repo for m in w.store.task_mrs("ACME-1")] == ["acme-theme"]
    platform = w.gl.mrs[0]
    assert platform["state"] == "closed" and platform["closed_by"]["username"] == w.gl.username
    assert "Closed by the Arceus runner" in w.gl.discussions[1][-1]["notes"][-1]["body"]
    assert w.remote_branch(branch) == platform_sha
    later(w)
    later(w)
    assert w.store.task("ACME-1").state == "code_review"                      # the runner's own close is no signal


def test_a_closed_mr_whose_branch_changes_again_fails_before_any_push(w):
    platform_sha, branch = two_mrs(w)
    theme_before = w.remote_branch(branch, "acme-theme")
    close(w, 0)
    w.tick()
    later(w)
    commit(w.repo, "more.txt")
    commit(w.cfg.paths.workspace / "acme-theme", "s.css", "b{}\n")
    w.settle("ACME-1", "waiting_review")
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "closed without merge, but its branch has changes again" in task.reason
    assert w.gl.mrs[0]["web_url"] in w.bot_comments()[-1]
    assert len(w.gl.mrs) == 2 and w.gl.mrs[0]["state"] == "closed"
    assert w.remote_branch(branch) == platform_sha and w.remote_branch(branch, "acme-theme") == theme_before


def test_an_agent_that_answers_instead_of_taking_the_changes_out_fails_clearly(w):
    two_mrs(w)
    close(w, 0)
    w.tick()
    later(w)
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="done", attempt_live=False, outcome={"summary": "The platform change is needed"}))
    w.tick()
    later(w)
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "the agent kept its changes in acme-platform" in task.reason


def test_an_mr_reopened_before_the_agent_got_to_it_is_left_alone(w):
    two_mrs(w)
    close(w, 0)
    w.tick()
    w.gl.mrs[0]["state"] = "opened"
    w.tick()
    later(w)
    later(w)
    assert sends(w) == [] and w.store.task("ACME-1").state == "code_review"
    close(w, 0)                                                                # closed again: a new signal
    w.tick()
    later(w)
    assert len(sends(w)) == 1


def test_a_reopen_withdraws_the_request_even_once_it_is_batched(w):
    two_mrs(w)
    w.gl.note(2, "reviewer", "@acme-agent-bot Rename the class")
    close(w, 0)
    w.tick()
    w.set_engine(lambda st: st["next_rc"].update({"worker-send": 6}))         # Bulava is busy: the batch waits
    later(w)
    batch = w.store.open_batch("ACME-1")
    assert batch is not None and "closed the MR" in batch.text
    w.gl.mrs[0]["state"] = "opened"
    w.tick()
    w.set_engine(lambda st: st["next_rc"].pop("worker-send", None))
    later(w)
    later(w)
    texts = [x["argv"][-1] for x in sends(w) if x.get("rc", 0) == 0]
    assert "Rename the class" in texts[-1] and "closed the MR" not in texts[-1]


def test_a_close_that_gitlab_refused_is_retried_without_a_second_note(w, monkeypatch):
    two_mrs(w)
    w.gl.note(1, "reviewer", "@acme-agent-bot Drop the platform part")
    w.tick()
    later(w)
    git(w.repo, "revert", "--no-edit", "HEAD")
    commit(w.cfg.paths.workspace / "acme-theme", "s.css", "b{}\n")
    w.settle("ACME-1", "waiting_review")
    gl = w.runner.code_host
    real = gl.close_mr

    def broken(project_id, iid):
        monkeypatch.setattr(gl, "close_mr", real)
        raise ServiceError("gitlab", "502 Bad Gateway", 502)

    monkeypatch.setattr(gl, "close_mr", broken)
    w.tick()
    assert w.gl.mrs[0]["state"] == "opened"
    w.tick()
    assert w.gl.mrs[0]["state"] == "closed" and w.store.task("ACME-1").state == "code_review"
    notes = [n["body"] for d in w.gl.discussions[1] for n in d["notes"] if "Closed by the Arceus runner" in n["body"]]
    assert len(notes) == 1


def test_merged_and_closed_mrs_end_the_task(w):
    two_mrs(w)
    close(w, 0)
    w.gl.mrs[1]["state"] = "merged"
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "done" and sends(w) == []
    merged = next(t for t in w.bot_comments() if "are merged" in t)
    assert w.gl.mrs[1]["web_url"] in merged and w.gl.mrs[0]["web_url"] not in merged


def test_every_mr_closed_stops_and_a_continue_does_not_replay_old_comments(w):
    platform_sha, branch = two_mrs(w)
    w.yt.comment("2-1", "j.doe", "I will close both, start over in the theme")
    w.tick()
    assert [sg.kind for sg in w.store.signals("ACME-1", unbatched=True)] == ["ticket"]
    close(w, 0)
    close(w, 1)
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "every merge request of this task was closed" in task.reason
    w.yt.clock = task.failed_at_ms + 1000
    w.yt.comment("2-1", "j.doe", "/continue Only the theme, in a new file")
    w.tick()
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    assert w.store.signals("ACME-1", unbatched=True) == []                    # the early comment is not review
    w.yt.comment("2-1", "j.doe", "Also keep the old class name")
    theme = w.cfg.paths.workspace / "acme-theme"
    git(w.repo, "revert", "--no-edit", "HEAD")
    git(theme, "revert", "--no-edit", "HEAD")
    commit(theme, "t.css")
    w.settle("ACME-1", "waiting_review")
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and [m.repo for m in w.store.task_mrs("ACME-1")] == ["acme-theme"]
    assert len(w.gl.mrs) == 3 and w.gl.mrs[2]["state"] == "opened"           # a new MR for the new change
    later(w)
    later(w)
    texts = [s["argv"][-1] for s in sends(w)]
    assert len(texts) == 2 and "Also keep the old class name" in texts[1]
    assert "start over in the theme" not in texts[1] and "Only the theme" not in texts[1]


def test_a_repository_with_reverted_work_gets_no_mr(w):
    take(w)
    theme = w.cfg.paths.workspace / "acme-theme"
    w.agent_commits()
    commit(theme, "s.css")
    git(theme, "revert", "--no-edit", "HEAD")
    w.settle("ACME-1", "waiting_review")
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"
    assert [m["project_id"] for m in w.gl.mrs] == [0]
    assert w.remote_branch(w.store.task("ACME-1").branch, "acme-theme") is None


def test_signals_survive_the_schema_that_knows_closed_mrs(tmp_path, monkeypatch):
    from arceus_runner import db

    full = list(db.MIGRATIONS)
    monkeypatch.setattr(db, "MIGRATIONS", full[:8])               # the schema before closed MRs were signals
    old = db.ReviewStore(tmp_path / "r.db")
    old.add_signal("note-1-5", "ACME-1", "review", "reviewer", "Rename it", "2026-10-20T07:00:00Z", repo="web",
                   discussion_id="d1")
    old.mark_replied("note-1-5")
    assert not old.add_signal("closed-1-2", "ACME-1", "closed", "gitlab", "x", "2026-10-20T07:00:00Z")
    old.close()
    monkeypatch.setattr(db, "MIGRATIONS", full)
    new = db.ReviewStore(tmp_path / "r.db")
    assert new.version == len(full)
    [kept] = new.signals("ACME-1", unbatched=True)
    assert (kept.id, kept.repo, kept.discussion_id, kept.replied) == ("note-1-5", "web", "d1", True)
    assert new.add_signal("closed-1-2", "ACME-1", "closed", "gitlab", "x", "2026-10-20T07:00:00Z")
    assert new.conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'signals_task'").fetchone()[0] == 1
    new.close()
