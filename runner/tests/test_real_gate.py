"""What the runner does with each way a real attempt can end, decided by the real Bulava review gate.

Runs where the engine is installed (ARCEUS_REAL_ENGINE=/opt/bulava/engine, `sandbox/sandbox.sh test`). The worker is
`cat`, as in test_real_engine.py, and the test plays the agent: it commits, declares its outcome with the run's own
`report-outcome`, asks with the engine's own question hook, and stops the way Claude Code stops, by running
`hooks/review-gate.sh`. `codex` is a script on the gate's PATH that answers what the scenario sets. So the outcome,
the receipt, `blocked.md` and the evidence the runner reads are written by the engine, not by the test, and a Bulava
update that changes any of them fails here rather than on a server.

What a test asserts is only what a person sees: the ticket's state, comments and tags, the commit on the remote
branch, and how many merge requests there are.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from .test_delivery import git
from .test_real_engine import ENGINE, _world, no_real_tools, pytestmark, wait_state  # noqa: F401

# Stands in for the Codex CLI wherever the engine calls it: the review gate (a team of reviewers, then a fix check
# by one) and the question hook. It answers what the test last set. In a fix check a PASS answers every open id as
# FIXED and a FAIL leaves them open (or closes them too, `close`: the old findings fixed, new ones found; or only the
# first, `close="first"`), as the gate asks; the ids come from the prompt itself.
CODEX = r'''#!/bin/bash
if [ "${1:-}" = login ]; then echo "Logged in using ChatGPT"; exit 0; fi
if [ "${@: -1}" = - ]; then _p="$(cat)"; else _p="${@: -1}"; fi
printf '%s\n' "$_p" > "$FAKE_CODEX_DIR/last-prompt"
echo x >> "$FAKE_CODEX_DIR/calls"
. "$FAKE_CODEX_DIR/answer"
if printf '%s' "$_p" | grep -q '^FIX CHECK'; then
  echo "STATE: $STATE"; echo "VERDICT: $VERDICT"; echo "DUPLICATES: NONE"
  printf '%s\n' "$_p" | awk '/^OPEN ITEMS/{f=1; next} f && /^[[:space:]]*$/{exit} f' | grep -oE '^[A-Z][0-9]+' \
    | { n=0; while read -r id; do n=$((n + 1))
        if [ "$VERDICT" = PASS ] || [ "$CLOSE" = 1 ] || { [ "$CLOSE" = first ] && [ "$n" = 1 ]; }; then echo "$id: FIXED"
        else echo "$id: OPEN — not fixed"; fi; done; }
else
  echo "STATE: $STATE"; echo "VERDICT: $VERDICT"
fi
printf '%s\n' "$BODY"
exit "$RC"
'''


class Agent:
    """The test's hands in the attempt the real engine is running."""

    def __init__(self, w, tmp: Path):
        self.w = w
        self.dir = tmp / "fakecodex"
        self.dir.mkdir()
        (self.dir / "codex").write_text(CODEX)
        (self.dir / "codex").chmod(0o755)
        self.gate_env: dict[str, str] = {}   # the gate's own test hooks, for the stops the test sets up
        self.transcript = tmp / "transcript.jsonl"
        self.transcript.write_text(json.dumps({"message": {"role": "assistant", "content": [
            {"type": "text", "text": "Done, the outcome is declared."}]}}) + "\n")
        self.codex("PASS")

    @property
    def engine(self):
        return self.w.runner.delivery.engine

    def idir(self, task_id: str = "ACME-1") -> Path:
        repo = self.w.store.task(task_id).repo_path
        out = subprocess.run(["bash", "-c", f'. "{ENGINE}/bin/supervisor-lib.sh"; instance_dir "$(slug_for "$1")"',
                              "-", repo], env=self.engine.env(), capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())

    def codex(self, verdict: str, body: str = "", state: str = "COMPLETE", rc: int = 0, raw: str | None = None,
              close: bool | str = False) -> None:
        """What Codex answers from now on: a review's header and body, or `raw` as it is (the question hook)."""
        if raw is not None:
            (self.dir / "answer").write_text(f"cat <<'EOF'\n{raw}\nEOF\nexit {rc}\n")
            return
        # The body as a file of its own: a review of several numbered lines reaches the gate as several lines.
        (self.dir / "body").write_text(body)
        (self.dir / "answer").write_text(f"STATE={state}\nVERDICT={verdict!r}\nRC={rc}\nCLOSE={close if isinstance(close, str) else int(close)}\n"
                                         'BODY="$(cat "$FAKE_CODEX_DIR/body")"\n')

    def env(self, task_id: str = "ACME-1") -> dict:
        env = self.engine.env() | self.gate_env
        return {**env, "ORCHESTRATOR_RUN_ID": (self.idir(task_id) / "run-id").read_text().strip(),
                "PATH": f"{self.dir}:{env['PATH']}", "FAKE_CODEX_DIR": str(self.dir),
                "SUPERVISOR_CODEX_USAGE_CMD": "/usr/bin/true", "SUPERVISOR_CLAUDE_USAGE_CMD": "/usr/bin/true"}

    def _cwd(self, task_id: str) -> str:
        return self.w.store.task(task_id).repo_path

    def declare(self, result: str, summary: str, task_id: str = "ACME-1") -> None:
        r = subprocess.run([str(self.idir(task_id) / "report-outcome"), result, summary], cwd=self._cwd(task_id),
                           env=self.env(task_id), capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def add_check(self, what: str, *argv: str, task_id: str = "ACME-1", final: bool = False) -> None:
        r = subprocess.run([str(self.idir(task_id) / "add-check"), *(["--final"] if final else []), what, "--", *argv],
                           cwd=self._cwd(task_id),
                           env=self.env(task_id), capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr

    def blocker(self, text: str, task_id: str = "ACME-1") -> None:
        """`report-finding blocker`: what the agent cannot go on without, for a person to give or do."""
        r = subprocess.run([str(self.idir(task_id) / "report-finding"), "blocker", text], cwd=self._cwd(task_id),
                           env=self.env(task_id), capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr

    def ask(self, question: str, options: list[str], task_id: str = "ACME-1") -> str:
        """AskUserQuestion, as Claude Code hands it to the engine's PreToolUse hook; Codex judges it a person's call."""
        self.codex("", raw=json.dumps({"decision": "ask", "reason_code": "product_fork",
                                       "headline": "Only the ticket's author can choose this.",
                                       "recommendation": options[0]}))
        hook = {"tool_name": "AskUserQuestion", "tool_use_id": "toolu_1", "cwd": self._cwd(task_id),
                "tool_input": {"questions": [{"question": question, "header": "Choice",
                                              "options": [{"label": o, "description": o} for o in options]}]}}
        r = subprocess.run([f"{ENGINE}/hooks/answer-question.sh"], input=json.dumps(hook), cwd=self._cwd(task_id),
                           env=self.env(task_id), capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-2000:]
        return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]

    def stop(self, task_id: str = "ACME-1") -> str | None:
        """The worker's turn ends: the engine's Stop hook. The reason it sends the worker back with, or None when the
        gate let the attempt end."""
        hook = {"session_id": "sess-1", "transcript_path": str(self.transcript), "cwd": self._cwd(task_id),
                "stop_hook_active": False}
        r = subprocess.run([f"{ENGINE}/hooks/review-gate.sh"], input=json.dumps(hook), cwd=self._cwd(task_id),
                           env=self.env(task_id), capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stderr[-2000:]
        out = json.loads(r.stdout) if r.stdout.strip() else {}
        return out.get("reason") if out.get("decision") == "block" else None


@pytest.fixture
def wa(tmp_path, secrets, request):
    """ACME-1 taken by the real engine, and the hands to play its agent. A test's parameter is the engine's own
    settings for the task, given at dispatch as on a server (the engine keeps them in the run's run-env)."""
    gen = _world(tmp_path, secrets, **getattr(request, "param", {}))
    w = next(gen)
    w.yt.add_issue(1, ("a1", "j.doe", "+agent"))
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched", w.events_named("task_failed")
    yield w, Agent(w, tmp_path)
    next(gen, None)


def tags(w) -> set[str]:
    return w.yt.issue_tags.get("2-1", set())


def last_comment(w) -> str:
    return w.bot_comments()[-1]


def settled(w, a, state: str) -> dict:
    """The engine says the attempt ended in `state`; then the runner's next tick reacts to it."""
    show = wait_state(w, "ACME-1", state)
    w.tick()
    return show


def nothing_published(w) -> None:
    assert w.remote_branch(w.store.task("ACME-1").branch) is None and w.gl.mrs == []


def in_code_review(w, a) -> str:
    """ACME-1 through one passing review: its MR open, the ticket in Code Review."""
    sha = w.agent_commits("health.txt")
    a.declare("succeeded_changes", "Added the health endpoint")
    assert a.stop() is None
    settled(w, a, "waiting_review")
    assert w.store.task("ACME-1").state == "code_review" and len(w.gl.mrs) == 1
    return sha


def questions(w) -> list[str]:
    return [t for t in w.bot_comments() if "[arceus:question:" in t]


def review_comment(w, a, text: str = "@acme-agent-bot Rename it") -> None:
    """A reviewer's comment on the MR, taken by the runner as a new attempt of the same task."""
    w.gl.note(1, "reviewer", text, path="health.txt", line=1)
    w.tick()
    w.now += timedelta(minutes=6)
    w.tick()
    wait_state(w, "ACME-1", "running")
    assert w.store.task("ACME-1").state == "dispatched"


FINDING = "1. [acceptance_failure] health.txt: the endpoint does not return the version the ticket asks for"


def test_a_failed_review_and_a_fix_publish_exactly_the_fixed_commit(wa):
    w, a = wa
    first = w.agent_commits("health.txt")
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("FAIL", FINDING)
    back = a.stop()
    assert back and "health.txt: the endpoint does not return the version" in back
    assert a.engine.show("ACME-1")["state"] == "running"     # still the same attempt, the worker fixes it
    w.tick()
    nothing_published(w)

    fixed = w.agent_commits("version.txt")
    a.declare("succeeded_changes", "The endpoint returns the version")
    a.codex("PASS")                                          # the fix check: every open item FIXED
    assert a.stop() is None
    settled(w, a, "waiting_review")
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and fixed != first
    assert w.remote_branch(task.branch) == fixed and len(w.gl.mrs) == 1
    assert "agent-failed" not in tags(w)


def test_codex_blocked_before_any_mr_is_a_question_and_the_answer_is_reviewed_again(wa):
    w, a = wa
    w.agent_commits("health.txt")
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("N/A", "Which of the two health formats in the ticket is the contract: JSON or plain text?",
            state="BLOCKED")
    assert a.stop() is None
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "needs_input" and "agent-needs-input" in tags(w) and "agent-failed" not in tags(w)
    note = last_comment(w)
    assert "@j.doe" in note and "JSON or plain text?" in note and "Nothing is published yet" in note
    nothing_published(w)

    assert len(questions(w)) == 1

    w.yt.comment("2-1", "a.smith", "JSON")
    w.tick()
    w.tick()                                                  # the answer is sent once, however often the runner looks
    show = wait_state(w, "ACME-1", "running")
    assert [x["kind"] for x in show["attempts"]] == ["initial", "continue"]
    fixed = w.agent_commits("json.txt")
    a.declare("succeeded_changes", "The endpoint answers JSON")
    a.codex("PASS")
    assert a.stop() is None
    settled(w, a, "waiting_review")
    assert w.remote_branch(w.store.task("ACME-1").branch) == fixed and len(w.gl.mrs) == 1
    assert len(questions(w)) == 1 and len(a.engine.show("ACME-1")["attempts"]) == 2


def test_codex_blocked_in_a_later_round_keeps_the_mr_and_the_answer_updates_it(wa):
    w, a = wa
    published = in_code_review(w, a)
    mr = (w.gl.mrs[0]["iid"], w.gl.mrs[0]["web_url"])
    review_comment(w, a)
    w.agent_commits("renamed.txt")
    a.declare("succeeded_changes", "Renamed as asked")
    a.codex("N/A", "The rename breaks the public URL: keep a redirect or not?", state="BLOCKED")
    assert a.stop() is None
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "needs_input" and "agent-needs-input" in tags(w)
    note = last_comment(w)
    assert "keep a redirect or not?" in note and w.gl.mrs[0]["web_url"] in note
    assert w.remote_branch(task.branch) == published and len(w.gl.mrs) == 1   # this round is not published

    assert len(questions(w)) == 1

    w.yt.comment("2-1", "a.smith", "Keep a redirect")
    w.tick()
    w.tick()
    show = wait_state(w, "ACME-1", "running")
    assert [x["kind"] for x in show["attempts"]] == ["initial", "continue", "continue"]   # one for the answer
    fixed = w.agent_commits("redirect.txt")
    a.declare("succeeded_changes", "Renamed, with a redirect")
    a.codex("PASS")
    assert a.stop() is None
    settled(w, a, "waiting_review")
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and w.remote_branch(task.branch) == fixed
    assert [(m["iid"], m["web_url"]) for m in w.gl.mrs] == [mr]           # the same MR, now at the fixed commit
    assert len(questions(w)) == 1 and len(a.engine.show("ACME-1")["attempts"]) == 3


def test_a_reviewer_that_cannot_be_reached_is_a_failure_not_a_question(wa):
    w, a = wa
    w.agent_commits("health.txt")
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("PASS", rc=1)                                    # Codex prints a verdict and dies: no verdict at all
    for _ in range(5):                                       # the engine parks and retries; the fifth time it gives up
        assert a.stop() is None
        w.tick()
        if a.engine.show("ACME-1")["state"] != "running":
            break
        nothing_published(w)
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "agent-failed" in tags(w) and "agent-needs-input" not in tags(w)
    assert "could not be reached" in task.reason
    assert "could not be reached" in last_comment(w) and "/continue" in last_comment(w)
    nothing_published(w)


def test_findings_that_keep_coming_back_end_the_task_with_them_on_the_ticket(wa):
    w, a = wa
    w.agent_commits("health.txt")
    for i in range(10):
        a.declare("succeeded_changes", f"Attempt {i} at the version")
        a.codex("FAIL", FINDING if i == 0 else "")              # later rounds: "T1: OPEN", nothing new
        if a.stop() is None:
            break
        w.agent_commits(f"try-{i}.txt")
    else:
        raise AssertionError("the gate never stopped sending the work back")
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "agent-failed" in tags(w) and "agent-needs-input" not in tags(w)
    assert "same problems" in task.reason
    assert "does not return the version the ticket asks for" in "\n".join(w.bot_comments())
    nothing_published(w)


@pytest.mark.parametrize("wa", [{"SUPERVISOR_MAX_ROUNDS_HARD": "3"}], indirect=True)   # 20 by default: same exit
def test_the_hard_round_limit_with_findings_still_open_ends_the_task_with_them(wa):
    w, a = wa
    w.agent_commits("health.txt")
    first = "\n".join(f"{n}. [acceptance_failure] health.txt: defect number {n} in the version check" for n in range(1, 6))
    for i in range(6):                                        # every round closes one finding of five: progress, not done
        a.declare("succeeded_changes", f"Round {i}")
        a.codex("FAIL", first if i == 0 else "", close="first")
        if a.stop() is None:
            break
        w.agent_commits(f"round-{i}.txt")
    else:
        raise AssertionError("the gate never stopped at its round limit")
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "agent-failed" in tags(w) and "agent-needs-input" not in tags(w)
    assert questions(w) == []
    assert task.reason == "the review still found problems when its rounds ran out"
    assert "defect number 5 in the version check" in "\n".join(w.bot_comments())   # the last of them still open
    nothing_published(w)


def test_a_fix_that_keeps_bringing_new_blockers_stops_for_a_person_and_says_so(wa):
    """Bulava 1.20.5: the open count, not a closed id, is progress. Each round here closes the last finding and finds a
    new one, so the count never falls, and the gate parks the run long before its round ceiling."""
    w, a = wa
    w.agent_commits("health.txt")
    for i in range(10):
        a.declare("succeeded_changes", f"Round {i}")
        a.codex("FAIL", f"1. [acceptance_failure] health.txt: defect number {i} in the version check "
                        "REACH: ordinary", close=True)
        if a.stop() is None:
            break
        w.agent_commits(f"round-{i}.txt")
    else:
        raise AssertionError("the gate never stopped a fix that kept bringing new blockers")
    assert i < 6
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "agent-failed" in tags(w) and questions(w) == []
    assert task.reason == ("the review stopped because every fix brought a new blocking problem, so a person has to "
                           "decide how to go on")
    assert f"defect number {i} in the version check" in "\n".join(w.bot_comments())
    nothing_published(w)


def test_a_rare_case_does_not_hold_the_work_back(wa):
    w, a = wa
    w.agent_commits("health.txt")
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("FAIL", "1. [acceptance_failure] health.txt: a caller that catches the error and goes on could see a "
                    "stale value REACH: rare")
    assert a.stop() is None
    settled(w, a, "waiting_review")
    assert w.store.task("ACME-1").state == "code_review" and len(w.gl.mrs) == 1


def test_a_review_that_disputes_the_task_s_own_decision_asks_its_author_and_the_answer_goes_on(wa):
    w, a = wa
    w.agent_commits("health.txt")
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("FAIL", '1. [spec_conflict] QUOTE: "no migration" — the seed needs one on existing databases '
                    "REACH: ordinary")
    assert a.stop() is None                                   # no FAIL goes back to the worker
    show = settled(w, a, "needs_input")
    assert show["state_reason"] == "spec_conflict"
    task = w.store.task("ACME-1")
    assert task.state == "needs_input" and "agent-needs-input" in tags(w) and "agent-failed" not in tags(w)
    [q] = questions(w)
    assert 'QUOTE: "no migration"' in q and "1. Keep the task as written" in q
    nothing_published(w)
    w.yt.comment("2-1", "a.smith", "1")
    w.tick()
    wait_state(w, "ACME-1", "running")
    assert w.store.task("ACME-1").state == "dispatched"
    # The brief the agent gets has the question's text, not its options: the number goes on with the option's words.
    briefs = sorted(a.idir().glob("messages/*/*.txt"), key=lambda f: f.stat().st_mtime)
    assert briefs and "1. Keep the task as written" in briefs[-1].read_text()
    a.declare("succeeded_changes", "Kept it as the ticket says")
    a.codex("PASS")
    assert a.stop() is None
    settled(w, a, "waiting_review")
    assert w.store.task("ACME-1").state == "code_review" and len(w.gl.mrs) == 1


def test_the_other_answer_to_a_disputed_decision_reaches_the_agent_in_words(wa):
    w, a = wa
    w.agent_commits("health.txt")
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("FAIL", '1. [spec_conflict] QUOTE: "no migration" — the seed needs one on existing databases '
                    "REACH: ordinary")
    assert a.stop() is None
    settled(w, a, "needs_input")
    w.yt.comment("2-1", "a.smith", "2")
    w.tick()
    wait_state(w, "ACME-1", "running")
    briefs = sorted(a.idir().glob("messages/*/*.txt"), key=lambda f: f.stat().st_mtime)
    assert briefs and "2. Change it as the review suggests" in briefs[-1].read_text()


def test_a_rare_dispute_of_the_task_s_decision_does_not_stop_it_and_the_round_s_defect_goes_back(wa):
    """The server incident behind Bulava 1.20.10: one reviewer disputes the author's decision for a legacy record that
    needs a 130-letter word, another calls it covered by that decision, and the round has an ordinary defect too. The
    defect goes back to the agent, nobody is asked, and the fixed commit is published."""
    w, a = wa
    w.agent_commits("health.txt")
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("FAIL", "1. [acceptance_failure] health.txt: the endpoint does not return the version REACH: ordinary\n"
                    '2. [spec_conflict] QUOTE: "each part 125 characters at most" — a legacy 150-character surname '
                    "plus a new 125-character first name gives 276, which the LMS rejects REACH: rare\n"
                    "3. [related_improvement] a legacy 150-character surname plus 125 gives 276; this stays under "
                    "the task's per-field decision")
    back = a.stop()
    assert back and "the endpoint does not return the version" in back
    assert a.engine.show("ACME-1")["state"] == "running"
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched" and questions(w) == []
    fixed = w.agent_commits("version.txt")
    a.declare("succeeded_changes", "Returned the version")
    a.codex("PASS")
    assert a.stop() is None
    settled(w, a, "waiting_review")
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and w.remote_branch(task.branch) == fixed and len(w.gl.mrs) == 1
    assert questions(w) == [] and "agent-needs-input" not in tags(w)


def test_a_dispute_beside_an_ordinary_defect_waits_and_is_asked_once_before_anything_is_published(wa):
    w, a = wa
    w.agent_commits("health.txt")
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("FAIL", "1. [acceptance_failure] health.txt: the endpoint does not return the version REACH: ordinary\n"
                    '2. [spec_conflict] QUOTE: "no migration" — the seed needs one on existing databases '
                    "REACH: ordinary")
    back = a.stop()
    assert back and "the endpoint does not return the version" in back and "WAITS FOR THE TASK" in back
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched" and questions(w) == []
    w.agent_commits("version.txt")
    a.declare("succeeded_changes", "Returned the version")
    a.codex("PASS")
    assert a.stop() is None                                   # the round with no defect asks the question
    show = settled(w, a, "needs_input")
    assert show["state_reason"] == "spec_conflict"
    task = w.store.task("ACME-1")
    assert task.state == "needs_input" and "agent-needs-input" in tags(w) and "agent-failed" not in tags(w)
    [q] = questions(w)
    assert q.count('QUOTE: "no migration"') == 1             # a team's three readings of it are one question
    nothing_published(w)
    w.tick()
    assert len(questions(w)) == 1


def answered(w, a, quote_line: str, answer: str) -> None:
    """ACME-1 parked on a disputed decision, and its author's answer on the ticket taken on as the next attempt."""
    w.agent_commits("health.txt")
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("FAIL", quote_line)
    assert a.stop() is None
    settled(w, a, "needs_input")
    w.yt.comment("2-1", "a.smith", answer)
    w.tick()
    wait_state(w, "ACME-1", "running")
    assert w.store.task("ACME-1").state == "dispatched"


NO_MIGRATION = '1. [spec_conflict] QUOTE: "no migration" — the seed needs one on existing databases REACH: ordinary'


def test_a_new_angle_on_a_decision_its_author_answered_does_not_ask_again(wa):
    w, a = wa
    answered(w, a, NO_MIGRATION, "Keep no migration: old databases are rebuilt from the seed on deploy.")
    w.agent_commits("seed.txt")
    a.declare("succeeded_changes", "Kept it as the ticket says")
    a.codex("FAIL", '1. [spec_conflict] QUOTE: "no migration" — a database restored from a year-old backup would '
                    "miss the new column")
    assert a.stop() is None
    assert "DECISIONS THE TASK'S AUTHOR ALREADY MADE" in (a.dir / "last-prompt").read_text()
    assert "rebuilt from the seed on deploy" in (a.dir / "last-prompt").read_text()
    settled(w, a, "waiting_review")
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and len(w.gl.mrs) == 1 and len(questions(w)) == 1


def test_an_answered_decision_is_asked_again_only_on_a_path_a_person_meets(wa):
    w, a = wa
    answered(w, a, NO_MIGRATION, "Keep no migration: old databases are rebuilt from the seed on deploy.")
    w.agent_commits("seed.txt")
    a.declare("succeeded_changes", "Kept it as the ticket says")
    a.codex("FAIL", '1. [spec_conflict] QUOTE: "no migration" — every deploy to staging keeps its database, so every '
                    "tester loses the new column REACH: ordinary")
    assert a.stop() is None
    show = settled(w, a, "needs_input")
    assert show["state_reason"] == "spec_conflict"
    assert len(questions(w)) == 2 and "every tester loses the new column" in questions(w)[-1]
    nothing_published(w)


def test_work_that_keeps_changing_after_each_review_stops_with_its_own_reason_and_nothing_is_published(wa):
    """Bulava 1.20.10: a branch that moved after the review read it goes back to be read again, and the third time in a
    row it parks the attempt. The ticket says so in its own words, and a `/continue` reviews the work again."""
    w, a = wa
    w.agent_commits("health.txt")
    repo = str(w.repo)
    # A review long enough that the gate's line falls past the 4000 bytes the receipt keeps of it.
    notes = "\n".join(f"{i}. [related_improvement] health.txt: note number {i} about the endpoint's wording"
                      for i in range(1, 80))
    # Something commits after the reviewer's answer, before its verdict is acted on: each time the gate holds it.
    hook = Path(repo).parent.parent / "move.sh"
    hook.write_text(f'echo moved >> "{repo}/health.txt" && git -C "{repo}" -c user.email=t@t -c user.name=t '
                    'commit -qam moved\n')
    a.gate_env["SUPERVISOR_TEST_HOOK_BEFORE_HOLD"] = str(hook)
    a.codex("PASS", notes)
    a.declare("succeeded_changes", "Added the health endpoint")
    for _ in range(3):
        if a.stop() is None:
            break
        w.tick()
        assert w.store.task("ACME-1").state == "dispatched" and "could not take" not in "\n".join(w.bot_comments())
        nothing_published(w)
        a.declare("succeeded_changes", "Added the health endpoint")
    else:
        raise AssertionError("the gate never stopped work that kept moving after its review")
    show = settled(w, a, "failed")
    assert len(show["receipt"]["review_why"]) <= 4000 and "[GATE]" not in show["receipt"]["review_why"]
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "agent-failed" in tags(w) and questions(w) == []
    assert task.reason == "the work kept changing after the review read it, so nothing was published"
    text = last_comment(w)
    assert "3 times in a row" in text and "/continue" in text and "[GATE]" not in text and repo not in text
    nothing_published(w)
    w.tick()
    assert text == last_comment(w)                            # said once
    a.gate_env.clear()
    a.codex("PASS")
    w.yt.comment("2-1", "a.smith", "/continue the stray watcher is stopped")
    w.tick()
    w.now += timedelta(minutes=6)
    w.tick()
    wait_state(w, "ACME-1", "running")
    assert w.store.task("ACME-1").state == "dispatched" and "agent-failed" not in tags(w)
    a.declare("succeeded_changes", "Stopped the watcher")
    assert a.stop() is None
    settled(w, a, "waiting_review")
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and w.remote_branch(task.branch) == git(Path(repo), "rev-parse", "HEAD")


def test_a_state_that_cannot_be_read_after_the_review_stops_once_with_its_own_reason(wa):
    """Bulava 1.20.10: after a PASS the gate reads the work again before accepting it; a repository it cannot read
    (here a file nobody may read, as a clone whose borrowed objects its user cannot open on a server) parks the attempt
    at once, never sent back to the agent, who cannot fix it."""
    w, a = wa
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads a file whatever its mode")
    w.agent_commits("health.txt")
    repo = str(w.repo)
    # The file goes unreadable after the reviewer's answer, before its verdict is acted on.
    hook = Path(repo).parent.parent / "unreadable.sh"
    hook.write_text(f'chmod 000 "{repo}/health.txt"\n')
    a.gate_env |= {"SUPERVISOR_TEST_HOOK_BEFORE_HOLD": str(hook), "SUPERVISOR_GUARD_RETRY_SECONDS": "0"}
    a.codex("PASS")
    a.declare("succeeded_changes", "Added the health endpoint")
    try:
        assert a.stop() is None
    finally:
        os.chmod(Path(repo) / "health.txt", 0o644)
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "agent-failed" in tags(w) and questions(w) == []
    assert task.reason == "the state of the work could not be read after the review, so nothing was published"
    text = last_comment(w)
    assert "Someone with access to the server" in text and "/continue" in text
    assert "[GATE]" not in text and repo not in text
    nothing_published(w)


def test_a_final_check_runs_once_after_the_review_and_holds_the_work_until_it_passes(wa, tmp_path):
    w, a = wa
    w.agent_commits("health.txt")
    runs, flag = tmp_path / "final-runs", tmp_path / "final-passes"
    a.add_check("the full suite passes", "sh", "-c",
                f'echo x >> "{runs}"; [ -f "{flag}" ] || {{ echo "FAILED test_version"; exit 4; }}', final=True)
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("FAIL", "1. [acceptance_failure] health.txt: no version in the answer REACH: ordinary")
    assert a.stop()                                           # a FAIL round: the final check does not run
    assert not runs.exists()
    a.declare("succeeded_changes", "Added the version")
    a.codex("PASS")
    back = a.stop()                                           # accepted by the review: now it runs, and fails
    assert back and "FINAL CHECKS" in back and runs.read_text().count("x") == 1
    nothing_published(w)
    flag.write_text("ok")
    a.declare("succeeded_changes", "Fixed the suite")
    assert a.stop() is None
    settled(w, a, "waiting_review")
    assert w.store.task("ACME-1").state == "code_review" and len(w.gl.mrs) == 1


def test_a_final_check_that_cannot_run_parks_the_work_and_says_it_was_the_final_check(wa):
    """A `--final` check whose tool is missing exits 127: the verifier records a `fail`, and Bulava parks the run for a
    person. The ticket names the final check, not "the project's checks kept failing"."""
    w, a = wa
    w.agent_commits("health.txt")
    a.add_check("the full suite passes", "sh", "-c", "arceus-test-no-such-suite-runner", final=True)
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("PASS")
    for _ in range(4):
        if a.stop() is None:
            break
        a.declare("succeeded_changes", "Added the health endpoint")
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "agent-failed" in tags(w) and questions(w) == []
    assert task.reason == ("the review accepted the work, but its final checks did not pass or left no readable "
                           "result, so nothing was published")
    assert "kept failing" not in "\n".join(w.bot_comments())
    nothing_published(w)


def test_work_left_uncommitted_is_never_published_and_is_kept_as_a_wip_commit(wa):
    w, a = wa
    (w.repo / "loose.txt").write_text("not committed\n")
    a.declare("succeeded_changes", "Wrote loose.txt")
    for _ in range(5):                                        # the gate asks to commit, then gives up
        back = a.stop()
        if back is None:
            break
        assert "закоміть" in back
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "agent-failed" in tags(w)
    assert "uncommitted" in task.reason and "acme-platform" in last_comment(w)
    nothing_published(w)
    assert git(w.repo, "log", "-1", "--format=%s").startswith("WIP [ACME-1]")
    assert git(w.repo, "status", "--porcelain") == ""


def no_change_asked(w, a) -> str:
    """ACME-1's first attempt concluded that nothing needs changing: a question for a person, never a failure."""
    a.declare("succeeded_no_change", "The health endpoint already exists at /healthz; nothing to change.")
    assert a.stop() is None
    show = settled(w, a, "failed")
    assert show["state_reason"] == "debt"                     # the engine reviews no claim that nothing changed
    task = w.store.task("ACME-1")
    assert task.state == "needs_input" and "agent-needs-input" in tags(w) and "agent-failed" not in tags(w)
    [q] = questions(w)
    assert "already exists at /healthz" in q and "close the ticket" in q and "could not take" not in q
    nothing_published(w)
    return q


def test_no_code_change_on_a_new_ticket_ends_as_done_when_a_person_closes_the_ticket(wa):
    w, a = wa
    no_change_asked(w, a)
    w.now += timedelta(days=30)                               # past the answer deadline: still not a failure
    w.tick()
    assert w.store.task("ACME-1").state == "needs_input" and "agent-failed" not in tags(w)
    w.yt.issue_fields["2-1"]["State"] = {"name": "Done"}
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "done" and tags(w).isdisjoint({"agent-needs-input", "agent-failed"})
    nothing_published(w)
    w.tick()
    assert [x["kind"] for x in a.engine.show("ACME-1")["attempts"]] == ["initial"]   # nothing sent to the agent


def test_a_reply_to_a_no_change_conclusion_takes_the_work_on_and_its_change_is_reviewed(wa):
    w, a = wa
    no_change_asked(w, a)
    w.yt.comment("2-1", "a.smith", "It exists, but it must also report the database status.")
    w.tick()
    w.tick()
    show = wait_state(w, "ACME-1", "running")
    assert [x["kind"] for x in show["attempts"]] == ["initial", "continue"]
    assert w.store.task("ACME-1").state == "dispatched" and "agent-needs-input" not in tags(w)
    sha = w.agent_commits("health.txt")
    a.declare("succeeded_changes", "The health endpoint reports the database status")
    assert a.stop() is None
    settled(w, a, "waiting_review")
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and w.remote_branch(task.branch) == sha and len(w.gl.mrs) == 1
    assert "agent-failed" not in tags(w) and len(questions(w)) == 1


def test_no_code_change_in_a_later_round_is_an_answer_and_the_mr_stays(wa):
    w, a = wa
    published = in_code_review(w, a)
    review_comment(w, a, "@acme-agent-bot Why not cache it?")
    a.declare("succeeded_no_change", "Caching would serve a stale status; the endpoint must be live.")
    assert a.stop() is None
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and "agent-failed" not in tags(w)
    assert w.remote_branch(task.branch) == published and len(w.gl.mrs) == 1
    replies = [n["body"] for d in w.gl.discussions[1] for n in d["notes"] if n["author"]["username"] != "reviewer"]
    assert any("stale status" in r for r in replies), replies


def test_an_agent_that_gives_up_fails_the_ticket_in_its_own_words(wa):
    w, a = wa
    w.agent_commits("half.txt")
    a.declare("failed", "The health check needs a database driver this project does not ship.")
    assert a.stop() is None
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "failed" and task.reason == "the agent reported that it could not finish the task"
    assert "needs a database driver" in last_comment(w) and "agent-failed" in tags(w)
    nothing_published(w)


def test_a_pass_the_verifier_could_not_confirm_is_not_published(wa):
    w, a = wa
    w.agent_commits("health.txt")
    a.add_check("the service builds", "arceus-test-no-such-build-tool")   # its tool is not on this machine
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("PASS")
    assert a.stop() is None
    show = settled(w, a, "failed")
    assert show["state_reason"] == "debt"
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "agent-failed" in tags(w)
    assert task.reason == ("The change passed review, but the automated checks were inconclusive, "
                           "so it was not published")
    assert "`the service builds` was inconclusive" in last_comment(w)
    nothing_published(w)


@pytest.mark.parametrize("wa", [{"SUPERVISOR_MAX_ROUNDS_HARD": "3"}], indirect=True)
def test_a_check_that_fails_outranks_a_pass_from_codex(wa):
    w, a = wa
    w.agent_commits("health.txt")
    a.add_check("the unit tests pass", "sh", "-c", "echo 'FAILED test_version'; exit 3")
    a.declare("succeeded_changes", "Added the health endpoint")
    a.codex("PASS")
    back = a.stop()
    assert back and "VERIFIER OVERRIDE" in back                 # Bulava turns Codex's PASS into a FAIL
    w.tick()
    nothing_published(w)
    for _ in range(4):                                           # the agent cannot make the check pass
        a.declare("succeeded_changes", "Added the health endpoint")
        if a.stop() is None:
            break
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "agent-failed" in tags(w) and questions(w) == []
    assert task.reason == "the project's automated checks kept failing, so the work was not accepted"
    text = "\n".join(w.bot_comments())
    assert "- check 1 failed (exit 3)" in text and "the unit tests pass" not in text   # the name stays on the server
    assert [(e["check"], e["status"], e["exit_code"]) for e in w.events_named("verifier_gap")][-1] == (1, "fail", 3)
    assert "the unit tests pass" not in w.cfg.paths.events.read_text()                  # nor in the stream to Loki
    nothing_published(w)


def test_the_agents_own_question_reaches_the_ticket_and_the_answer_goes_back_to_it(wa):
    w, a = wa
    told = a.ask("Should the endpoint require a token?", ["Yes, a token", "No, public"])
    assert "report-outcome needs_input" in told                # the hook tells the worker to end the attempt
    a.declare("needs_input", "Should the endpoint require a token? Yes, a token / No, public")
    assert a.stop() is None
    settled(w, a, "needs_input")
    task = w.store.task("ACME-1")
    assert task.state == "needs_input" and task.question_id and task.question_id.startswith("q-")
    note = last_comment(w)
    assert "Should the endpoint require a token?" in note and "No, public" in note and "agent-needs-input" in tags(w)

    w.yt.comment("2-1", "a.smith", "No, public")
    w.tick()
    show = wait_state(w, "ACME-1", "running")
    assert [x["kind"] for x in show["attempts"]] == ["initial", "continue"]
    assert w.store.task("ACME-1").state == "dispatched"


def test_a_blocker_the_agent_files_is_a_question_and_the_reply_takes_the_work_on(wa):
    """Seen in a live run: CI failed on a test that exists in no commit, and the agent, with no access to GitLab,
    filed a blocker asking for the job's log. That waits for a person; it is not a failure."""
    w, a = wa
    published = in_code_review(w, a)
    w.gl.mrs[0]["head_pipeline"] = {"id": 77, "status": "failed", "sha": published, "web_url": "https://gl/p/77"}
    w.gl.jobs[77] = [{"id": 501, "name": "unit-tests", "stage": "test", "web_url": "https://gl/j/501"}]
    w.gl.traces[501] = "FAILED tests/test_health.py::test_gone - AssertionError"
    w.tick()
    w.now += timedelta(minutes=6)
    w.tick()
    wait_state(w, "ACME-1", "running")
    a.blocker("CI job 501 fails in test_gone, which exists in no commit: need the job's checkout SHA or a re-run.")
    w.agent_commits("test_gone_guard.txt")                  # as in the live run: a regression test, then the wall
    a.declare("succeeded_changes", "Added a guard test; the suite passes locally.")
    assert a.stop() is None
    settled(w, a, "failed")
    task = w.store.task("ACME-1")
    assert task.state == "needs_input" and "agent-needs-input" in tags(w) and "agent-failed" not in tags(w)
    [q] = questions(w)
    assert "need the job's checkout SHA or a re-run" in q and w.gl.mrs[0]["web_url"] in q
    assert w.remote_branch(task.branch) == published and len(w.gl.mrs) == 1

    w.yt.comment("2-1", "a.smith", "It was a stale runner cache; re-run, it is green now.")
    w.tick()
    w.tick()
    show = wait_state(w, "ACME-1", "running")
    assert [x["kind"] for x in show["attempts"]] == ["initial", "continue", "continue"]
    assert w.store.task("ACME-1").state == "dispatched" and len(questions(w)) == 1


def test_a_red_pipeline_of_the_published_commit_is_fixed_in_the_same_mr(wa):
    w, a = wa
    published = in_code_review(w, a)
    w.gl.mrs[0]["head_pipeline"] = {"id": 77, "status": "failed", "sha": published, "web_url": "https://gl/p/77"}
    w.gl.jobs[77] = [{"id": 501, "name": "unit-tests", "stage": "test", "web_url": "https://gl/j/501"}]
    w.gl.traces[501] = "FAILED tests/test_health.py::test_version - AssertionError"
    w.tick()
    w.now += timedelta(minutes=6)
    w.tick()
    show = wait_state(w, "ACME-1", "running")
    assert [x["kind"] for x in show["attempts"]] == ["initial", "continue"]
    sent = (a.idir() / "dispatch.json").read_text()
    assert "test_health.py::test_version" in sent               # the job's log reached the agent

    fixed = w.agent_commits("fix.txt")
    a.declare("succeeded_changes", "Fixed the version test")
    a.codex("PASS")
    assert a.stop() is None
    settled(w, a, "waiting_review")
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and w.remote_branch(task.branch) == fixed and len(w.gl.mrs) == 1
    w.now += timedelta(minutes=6)
    w.tick()
    assert len(a.engine.show("ACME-1")["attempts"]) == 2       # the old pipeline is not repaired again


def test_a_worker_session_killed_mid_attempt_is_continued_without_a_second_mr(wa):
    w, a = wa
    w.agent_commits("health.txt")
    session = a.engine.folder_session(Path(w.store.task("ACME-1").repo_path))
    subprocess.run(["tmux", "-L", "bulava", "kill-session", "-t", session], env=a.engine.env(), check=True)
    w.tick()
    show = wait_state(w, "ACME-1", "running")
    assert [x["kind"] for x in show["attempts"]] == ["initial", "continue"] and show.get("attempt_live")
    fixed = w.agent_commits("rest.txt")
    a.declare("succeeded_changes", "Added the health endpoint")
    assert a.stop() is None
    settled(w, a, "waiting_review")
    assert w.remote_branch(w.store.task("ACME-1").branch) == fixed and len(w.gl.mrs) == 1


def test_a_runner_stopped_right_after_its_first_mr_publishes_each_repository_once(wa, monkeypatch):
    """Two repositories passed review; the runner dies right after the code host made the first MR and before the
    runner wrote it down. After a restart on the same database and the same engine state each repository has one MR
    at its reviewed commit and the ticket one note, and a second ticket waits until the shared workspace is free."""
    w, a = wa
    shas = {"acme-platform": w.agent_commits("health.txt")}
    theme = w.cfg.paths.workspace / "acme-theme"
    (theme / "theme.css").write_text("body {}\n")
    git(theme, "add", "theme.css")
    git(theme, "commit", "-q", "-m", "theme")
    shas["acme-theme"] = git(theme, "rev-parse", "HEAD")
    a.declare("succeeded_changes", "Health endpoint and its style")
    assert a.stop() is None
    reviewed = {Path(r["path"]).name: r["reviewed_sha"] for r in wait_state(w, "ACME-1", "waiting_review")["repos"]}
    assert {k: reviewed[k] for k in shas} == shas

    host = type(w.runner.code_host)
    real_create = host.create_mr

    def create(self, *args, **kwargs):
        mr = real_create(self, *args, **kwargs)
        if len(w.gl.mrs) == 1:
            raise KeyboardInterrupt                               # the MR exists, the runner never wrote it down
        return mr

    monkeypatch.setattr(host, "create_mr", create)
    with pytest.raises(KeyboardInterrupt):
        w.tick()
    monkeypatch.setattr(host, "create_mr", real_create)
    assert w.store.task("ACME-1").state == "publishing" and len(w.gl.mrs) == 1

    w.restart()
    w.yt.add_issue(2, ("a2", "j.doe", "+agent"))
    w.yt.fail_once[("POST", "/api/issues/2-1/comments")] = 503   # the links note fails once: still publishing
    w.clock.advance(3600)
    w.tick()
    assert w.store.task("ACME-1").state == "publishing"
    second = w.store.task("ACME-2")
    assert second is None or second.state != "dispatched"         # the workspace is not free yet
    assert a.engine.show("ACME-2") is None

    w.clock.advance(3600)
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "code_review"
    assert {name: w.remote_branch(task.branch, name) for name in shas} == shas
    assert len(w.gl.mrs) == 2 and len({m["project_id"] for m in w.gl.mrs}) == 2
    assert len([t for t in w.bot_comments() if "[arceus:mr:" in t]) == 1
    w.tick()
    assert w.store.task("ACME-2").state == "dispatched"           # taken once ACME-1 let the workspace go
