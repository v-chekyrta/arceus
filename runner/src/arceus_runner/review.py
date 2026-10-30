"""The loop after the MR: watch, gather, send back, publish again.

For every task in Code Review the runner reads each of its MRs on the code host (integrations.CodeHost):

- review notes by people that mention the bot (`@<bot_user>`, as the host writes a mention), in every discussion, so a
  late reply in an old thread is seen too; notes that do not address the bot are people talking to
  each other and wake nothing;
- the newest CI run: a failure of the commit the runner published is a CI signal, with the
  failed jobs and the end of their logs; a run still going, cancelled or for an older
  commit is not;
- a conflict that the host has confirmed (MRStatus.conflict is True; still being worked out is not);
- comments on the ticket by people on the allowlist;
- the MR's state: all merged ends the task, one closed without merge stops it.

Each signal is stored once (SQLite), so a repeated poll or a restart sends nothing twice. When
`review_debounce` has passed since the newest signal, they go through the security screen
(screening.py) and then to the agent as one follow-up of the same Bulava task (`worker-send --relation continue --source mr|ci|<tracker>`). Each MR thread
goes whole, earlier notes included, so a short "why?" keeps its context. The agent decides per
thread whether it asks for a code change or only for an answer, and leaves one answer per thread
in a file (replies.py). After Codex passes the fix, Delivery.publish pushes it to the same branch
and calls after_publish(), which posts those answers in their threads; a round that changed no
code posts them through answered_without_changes(). A round counts against `max_review_rounds`
only when it ends in published code; one answered without a change counts against
`max_review_answers`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from arceus_runner import replies
from arceus_runner.db import ReviewStore, Signal, Task
from arceus_runner.engine import EngineError
from arceus_runner.gitops import GitError
from arceus_runner.http import BadRequest, NotFound, ServiceError
from arceus_runner.integrations import CodeHost, MRStatus, Note, key_part
from arceus_runner.screening import is_decision

KIND_TITLES = {
    "code_review": "What the code review found that has to change",
    "review": "Review comments on the MR",
    "ci": "Failed CI",
    "conflict": "Conflicts with the target branch",
    "moved": "New commits on the branch that you do not have",
    "closed": "Merge requests people closed without merging",
    "ticket": "Comments on the ticket",
}
TRACE_JOBS = 3
TRACE_CHARS = 2500
NOTE_CHARS = 2000    # one note of a thread, as the agent sees it
THREAD_CHARS = 8000  # one thread: its first note and the latest ones, the middle left out
_OUR_MARKER = re.compile(r"\s*<!-- arceus:[^>]*-->")


def conflict_text(repo: str, target: str, default: str = "") -> str:
    """`default`: the branch the task branch was started from, when the MR now merges somewhere else."""
    if default and default != target:
        how = (f"The task branch was started from {default}; move only this ticket's commits onto the latest "
               f"{target}, none of {default}'s: git fetch origin && git rebase --onto origin/{target} <base>, "
               f"where <base> is the commit just below your first commit for this ticket (git log --oneline "
               f"origin/{target}..HEAD lists what moves; nothing of {default} may be in it afterwards).")
    else:
        how = f"Rebase the task branch onto the latest {target}: git fetch origin && git rebase origin/{target}."
    return (f"The MR in {repo} conflicts with {target}. {how} Resolve each conflict, git add the files and "
            f"git rebase --continue until the rebase is done; never leave it half done (git status shows no "
            f"rebase in progress), and do not merge {target} in. If earlier rounds merged {target} into the "
            f"branch, the rebase replays its commits without those merges and their conflicts may come up again: "
            f"resolve them the same way. "
            f"Then run the checks again. Do not push: the runner publishes the rebased branch to the same MR.")


def _pull_rebase(branch: str) -> str:
    return (f"Put your commits on top of theirs: git pull --rebase origin {branch}, resolving any conflicts and "
            f"finishing with git rebase --continue.")


_MOVED_TAIL = "Keep every one of their changes, and do not merge. Do not push: the runner publishes the branch."
_ROUND_RULE = ("- Make code changes with new commits on the same branch and commit everything. Rebase only "
               "as a conflict or a push by people above asks; never merge and never push. If nothing in this "
               "round needs a code change, change nothing and say why in your result.")
# What earlier versions of the runner wrote into signals and rounds that may still wait in the database to be
# sent: told to merge, where the runner now has conflicts and people's pushes taken in by rebase.
_FORMER = [
    (re.compile(r"The MR in (\S+) conflicts with (\S+)\. Merge the latest \2 into the task branch "
                r"\(git fetch origin && git merge origin/\2\), resolve the conflicts and commit\. No rebase\."),
     lambda m: conflict_text(m[1], m[2])),
    (re.compile(r"Merge them in: git pull --no-rebase origin (\S+), resolve any conflicts and commit\. "
                r"Do not rewrite other people's commits\."),
     lambda m: f"{_pull_rebase(m[1])} {_MOVED_TAIL}"),
    (re.compile(r"- Make code changes with new commits on the same branch, without rebase or force push, and "
                r"commit everything\. If nothing in this round needs a code change, change nothing and say why "
                r"in your result\."),
     lambda m: _ROUND_RULE),
]


def current_instructions(text: str) -> str:
    """`text` with what an earlier runner told the agent about conflicts replaced by what it tells it now."""
    for pattern, new in _FORMER:
        text = pattern.sub(new, text)
    return text


def _closed_id(mr) -> str:
    return f"closed-{key_part(mr.project_id)}-{key_part(mr.iid)}"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _note_signal(mr, note_id) -> str:
    return f"note-{key_part(mr.project_id)}-{key_part(note_id)}"


def _pipeline_signal(mr, ci_id) -> str:
    return f"pipeline-{key_part(mr.project_id)}-{key_part(ci_id)}"


def _conflict_signal(mr, sha: str) -> str:
    return f"conflict-{key_part(mr.project_id)}-{key_part(sha)}"


def _thread_labels(signals: list[Signal]) -> dict[tuple[str, str], str]:
    """`T1`, `T2`, … for the MR threads of a round, the same each time the round's signals are read."""
    keys = sorted({(s.repo, s.discussion_id) for s in signals if s.kind == "review" and s.repo and s.discussion_id})
    return {k: f"T{i}" for i, k in enumerate(keys, 1)}


def _fit(lines: list[str], limit: int = THREAD_CHARS) -> list[str]:
    """The first line and as many of the latest as fit, so the question and the newest notes stay."""
    if sum(len(x) + 1 for x in lines) <= limit:
        return lines
    tail: list[str] = []
    room = limit - len(lines[0]) - 60
    for line in reversed(lines[1:]):
        if len(line) + 1 > room:
            break
        tail.insert(0, line)
        room -= len(line) + 1
    left_out = len(lines) - 1 - len(tail)
    return [lines[0], f"- … {left_out} earlier {'note' if left_out == 1 else 'notes'} left out …", *tail]


class ReviewLoop:
    def __init__(self, delivery):
        self.d = delivery
        self.cfg = delivery.cfg
        self.store: ReviewStore = delivery.store
        self.host: CodeHost = delivery.host
        self.events = delivery.events

    # -- watching -------------------------------------------------------------------------------

    def poll(self, only: set[str] | None = None) -> None:
        for task in self.store.tasks("code_review"):
            if only is not None and task.task_id not in only:
                continue
            try:
                if self._watch(task):
                    self._maybe_send(self.store.task(task.task_id))
            except (ServiceError, EngineError, GitError) as e:
                self.events.emit("review_poll_failed", task_id=task.task_id, reason=str(e))

    def _watch(self, task: Task) -> bool:
        """Read every MR of the task. False once the task is over (merged, closed, moved on)."""
        mrs = self.store.task_mrs(task.task_id)
        if not mrs:
            return False
        now = self.d.now().isoformat()
        statuses: dict[str, MRStatus] = {}
        for mr in mrs:
            st = self.host.mr_status(mr.project_id, mr.iid)
            statuses[mr.repo] = st
            if st.state != mr.state:
                self.store.update_mr(task.task_id, mr.repo, state=st.state)
                self.events.emit("mr_state", task_id=task.task_id, repo=mr.repo, mr_iid=mr.iid, state=st.state)
                if st.state == "opened":  # reopened before the agent got to it: nothing to take out
                    self.store.withdraw_signal(_closed_id(mr))
            if st.state != "opened":
                continue
            if self.d.reviewer:
                self.d.reviewer.approval_outlived(task, mr.project_id, mr.iid, st.sha)
            self._notes(task, mr, now)
            self._pipeline(task, mr, st, now)
            self._conflict(task, mr, st, now)

        states = {s.state for s in statuses.values()}
        if "opened" not in states:  # nothing left to review: people decided on every MR
            merged = [m for m in mrs if statuses[m.repo].state == "merged"]
            if merged:
                self._finished(task, merged)
            else:
                self.d.fail(task, "every merge request of this task was closed without merge; the runner stopped "
                                  "following it")
            return False
        for mr in mrs:
            if statuses[mr.repo].state == "closed" and not self._closed(task, mr, statuses[mr.repo], now):
                return False
        if self._ticket_moved_on(task):
            return False
        self._ticket_comments(task, now)
        return True

    def _notes(self, task: Task, mr, now: str) -> None:
        bot = self.cfg.code_host.bot_user
        reviewer = self.d.reviewer
        for n in self.host.notes(mr.project_id, mr.iid, mr.url):
            if n.system or n.author == bot or not self.host.mentions_bot(n.body):
                continue  # only a note that mentions the bot is a request to it
            if n.author == getattr(self.host, "approver", "") and "<!-- arceus:" in n.body:
                continue  # the code review's own note, signed by the approver; the person behind it may still ask
            if reviewer and reviewer.is_mr_command(n.body):
                continue  # the code review command: started from the ticket, and never a change request
            where = f" ({n.path}:{n.line})" if n.path else ""
            text = f"{n.author} in {mr.repo}{where}: {n.body.strip()}\n{n.url}"
            if self.store.add_signal(_note_signal(mr, n.id), task.task_id, "review", n.author, text, now,
                                     repo=mr.repo, discussion_id=n.discussion_id):
                self.events.emit("review_signal", task_id=task.task_id, kind="review", author=n.author,
                                 repo=mr.repo, url=n.url)

    def _pipeline(self, task: Task, mr, st: MRStatus, now: str) -> None:
        if st.ci_sha != mr.published_sha or st.ci_id is None:
            return  # no CI run yet, or one for an older commit
        if st.ci_status == "success" and task.ci_attempts:
            self.d.store.update_task(task.task_id, ci_attempts=0)
        if st.ci_status != "failed" or str(st.ci_id) == str(mr.handled_pipeline):
            return
        jobs = self.host.failed_jobs(mr.project_id, st.ci_id)
        parts = [f"Pipeline {st.ci_url or st.ci_id} in {mr.repo} failed on commit {mr.published_sha[:12]}."]
        for job in jobs[:TRACE_JOBS]:
            try:
                tail = self.d.git._scrub(self.host.job_log_tail(mr.project_id, job.id, TRACE_CHARS))
            except ServiceError as e:
                tail = f"(could not read the log: {e})"
            parts.append(f"Job {job.name} (stage {job.stage}): {job.url}\n```\n{tail.strip()}\n```")
        if len(jobs) > TRACE_JOBS:
            parts.append(f"{len(jobs) - TRACE_JOBS} more failed jobs: " + ", ".join(j.name for j in jobs[TRACE_JOBS:]))
        with self.store.transaction():
            self.store.add_signal(_pipeline_signal(mr, st.ci_id), task.task_id, "ci", f"{self.host.name}-ci",
                                  "\n\n".join(parts), now, repo=mr.repo)
            self.store.update_mr(task.task_id, mr.repo, handled_pipeline=st.ci_id)
        self.events.emit("review_signal", task_id=task.task_id, kind="ci", repo=mr.repo, pipeline=st.ci_id)

    def _closed(self, task: Task, mr, st: MRStatus, now: str) -> bool:
        """A person closed one MR while others are open: the agent takes that repository's changes out.

        The next publish then leaves the MR closed and drops it from the task. False (the task failed)
        when the agent already had its round for this and the MR still carries its changes.
        """
        sig = self.store.signal(_closed_id(mr))
        if sig and sig.batch_id:
            batch = self.store.batch(sig.batch_id)
            if batch and batch.sent_at and batch.sent_at != "stale":
                self.d.fail(task, f"{mr.url} was closed without merge, and the agent kept its changes in {mr.repo}",
                            "Reopen the MR, or have the agent take those changes out with /continue.")
                return False
            return True  # sent on a later tick
        who = st.closed_by or "a person"
        target = self._target(mr)
        text = (f"{who} closed the MR in {mr.repo} without merging it: {mr.url}. Its changes are not wanted in this "
                f"task. Take them out: git revert your commits for this ticket in {mr.repo} on {task.branch} (no reset, "
                f"no force push), so that the branch has no difference from origin/{target}. The runner then leaves "
                f"that MR closed and drops it from the task. Keep the rest of the work correct without those "
                f"changes; if the ticket or the MRs say where they belong instead, move them there.")
        if self.store.add_signal(_closed_id(mr), task.task_id, "closed", self.host.name, text, now, repo=mr.repo):
            self.events.emit("review_signal", task_id=task.task_id, kind="closed", repo=mr.repo, by=who)
        return True

    def _conflict(self, task: Task, mr, st: MRStatus, now: str) -> None:
        # Hosts work mergeability out asynchronously: only a confirmed conflict counts, never "not known yet".
        if st.conflict is False and mr.handled_conflict:
            # It merges cleanly now. The target may still move on and conflict with the same commit again:
            # that is a new conflict, so the next one is sent too.
            self.store.update_mr(task.task_id, mr.repo, handled_conflict=None)
            return
        if st.conflict is not True or mr.handled_conflict == st.sha:
            return
        default = self._target(mr)
        text = conflict_text(mr.repo, st.target or default, default)
        sig_id = _conflict_signal(mr, st.sha)
        if self.store.signal(sig_id):  # this commit conflicted before, then merged cleanly until the target moved
            sig_id += f"-{key_part(now)}"
        with self.store.transaction():
            self.store.add_signal(sig_id, task.task_id, "conflict", self.host.name, text, now, repo=mr.repo)
            self.store.update_mr(task.task_id, mr.repo, handled_conflict=st.sha)
        self.events.emit("review_signal", task_id=task.task_id, kind="conflict", repo=mr.repo)

    def _stale(self, task: Task, signals: list[Signal]) -> bool:
        """Every signal is a CI run or a conflict that is over: the pipeline passed or was replaced, or the host
        now says the MR merges cleanly (a `/continue` or another round rebased the branch meanwhile). A
        conflict the host cannot judge yet still counts."""
        if not signals or any(s.kind not in ("ci", "conflict") for s in signals):
            return False
        mrs = {m.repo: m for m in self.store.task_mrs(task.task_id)}
        for s in signals:
            if s.kind == "ci":
                if self._ci_still_failing(task, [s]):
                    return False
                continue
            mr = mrs.get(s.repo)
            if mr and self.host.mr_status(mr.project_id, mr.iid).conflict is not False:
                return False
        return True

    def _ci_still_failing(self, task: Task, signals: list[Signal]) -> bool:
        """A CI signal is worth sending only while that very pipeline is still the MR's, and red."""
        mrs = {m.repo: m for m in self.store.task_mrs(task.task_id)}
        for s in signals:
            if s.kind != "ci":
                continue
            mr = mrs.get(s.repo)
            if not mr:
                return False
            st = self.host.mr_status(mr.project_id, mr.iid)
            if st.ci_id is None or s.id != _pipeline_signal(mr, st.ci_id) or st.ci_status != "failed" \
                    or st.ci_sha != mr.published_sha:
                return False
        return True

    def _target(self, mr) -> str:
        for r in self.d._group():
            if r.relative_path == mr.repo:
                return r.default_branch or "main"
        return "main"

    def _ticket_comments(self, task: Task, now: str) -> None:
        c = self.cfg.tracker
        since = task.published_at_ms or 0
        reviewer = self.d.reviewer
        for x in self.d.tracker.comments(task.issue_db_id):
            if x.created > since and x.author != c.bot_user and x.author in c.allowed_users and x.text.strip():
                if reviewer and reviewer.claims(task.task_id, x):
                    continue  # a request to the reviewer (code_review.py), not to the agent that wrote the code
                if is_decision(x.text):
                    continue  # a security reviewer's decision on a held text: the runner's, not a review comment
                if self.store.add_signal(f"yt-{x.id}", task.task_id, "ticket", x.author,
                                         f"{x.author} on the ticket: {x.text.strip()}", now):
                    self.events.emit("review_signal", task_id=task.task_id, kind="ticket", author=x.author)

    def _ticket_moved_on(self, task: Task) -> bool:
        """Someone moved the ticket past Code Review by hand: the team took it from here."""
        c = self.cfg.tracker
        state = self.d.tracker.issue(task.task_id).state
        if state in (c.states.code_review, c.states.in_progress):
            return False
        self.store.update_task(task.task_id, state="stopped", reason=f"the ticket was moved to \"{state}\" by hand")
        self.events.emit("task_stopped", task_id=task.task_id, reason="ticket moved on", state=state)
        return True

    def _finished(self, task: Task, mrs) -> None:
        c = self.cfg.tracker
        if c.states.merged and self.d.tracker.issue(task.task_id).state in (c.states.code_review,
                                                                             c.states.in_progress):
            # Only from where the runner put it: a ticket a person already moved on stays where they put it.
            # A passing failure raises and the next poll tries again, since the task is not done yet; a board
            # without that state does not keep the task open.
            try:
                self.d.tracker.set_state(task.issue_db_id, c.states.merged)
                self.events.emit("ticket_moved", task_id=task.task_id, state=c.states.merged, reason="merged")
            except (BadRequest, NotFound) as e:
                self.events.emit("ticket_move_failed", task_id=task.task_id, state=c.states.merged, error=str(e)[:300])
        links = "\n".join(f"- {m.url}" for m in mrs)
        self.d.comment_once(task, "merged", f"All merge requests of this task are merged; the runner has stopped following it:\n{links}")
        self.store.update_task(task.task_id, state="done", reason="merge requests merged")
        self.events.emit("task_merged", task_id=task.task_id, mrs=[m.url for m in mrs])

    # -- sending back ---------------------------------------------------------------------------

    def _maybe_send(self, task: Task) -> None:
        if self.store.open_batch(task.task_id) is None and not self.store.signals(task.task_id, unbatched=True):
            return  # nothing to send, so nothing waits for the workspace either
        if self.d._workspace_waits(task):
            return  # the workspace is shared: nothing is switched while another task works in it
        batch = self.store.open_batch(task.task_id)
        if batch is not None and self._stale(task, self.store.signals(task.task_id, batch.id)):
            self.store.batch_sent(batch.id, "stale")  # the pipeline passed or the conflict went while we waited
            self.events.emit("review_batch_dropped", task_id=task.task_id, batch=batch.id,
                             reason="CI no longer red" if batch.ci_only else "nothing left to repair")
            batch = None
        if batch is None:
            pending = self.store.signals(task.task_id, unbatched=True)
            if not pending:
                return
            newest = max(_ts(s.seen_at) for s in pending)
            if self.d.now() - newest < self.cfg.schedule.review_debounce:
                return  # people may still be writing: wait for quiet (review_debounce)
            stale = [s.id for s in pending if self._stale(task, [s])]
            if stale:
                self.store.drop_signals(stale, "stale")
                pending = [s for s in pending if s.id not in stale]
                if not pending:
                    return
            ci_only = {s.kind for s in pending} == {"ci"}
            has_ci = any(s.kind == "ci" for s in pending)
            lim = self.cfg.limits
            if has_ci and task.ci_attempts >= lim.max_ci_fix_attempts:
                self.events.emit("limit_reached", task_id=task.task_id, limit="ci_fix_attempts",
                                 used=task.ci_attempts, max=lim.max_ci_fix_attempts)
                self.d.fail(task, "CI was still failing after the allowed number of repair attempts; a person is needed",
                            self._compose(task, pending)[:1500])
                return
            if not ci_only and task.review_rounds >= lim.max_review_rounds:
                self.events.emit("limit_reached", task_id=task.task_id, limit="review_rounds",
                                 used=task.review_rounds, max=lim.max_review_rounds)
                self.d.fail(task, "the review rounds allowed for this task are used up; a person is needed",
                            self._compose(task, pending)[:1500])
                return
            if not ci_only and task.review_answers >= lim.max_review_answers:
                self.events.emit("limit_reached", task_id=task.task_id, limit="review_answers",
                                 used=task.review_answers, max=lim.max_review_answers)
                self.d.fail(task, "the review rounds that may be answered without a code change are used up; "
                                  "a person is needed",
                            self._compose(task, pending)[:1500])
                return
            ids = [s.id for s in pending]
            batch_id = "review-" + hashlib.sha1("\n".join(sorted(ids)).encode()).hexdigest()[:16]
            humans = [s.author for s in pending if s.kind in ("review", "ticket")]
            source = "ci" if ci_only else (self.d.tracker.engine_source if {s.kind for s in pending} == {"ticket"}
                                           else "mr")
            threads, writers = self._threads(task, pending)
            text = self._compose(task, pending, batch_id, threads)
            batch = self.store.make_batch(batch_id, task.task_id, source, humans[0] if humans else
                                          self.cfg.tracker.bot_user, text, ci_only, self.d.now().isoformat(), ids,
                                          authors=list(dict.fromkeys([*humans, *writers])))
            self.events.emit("review_batch", task_id=task.task_id, batch=batch.id, signals=len(ids), source=source)
        self._send(task, batch)

    def _send(self, task: Task, batch) -> None:
        if self.d._workspace_waits(task):
            return  # the batch stays open and goes on a later tick
        text = current_instructions(batch.text)
        if text != batch.text:
            # Made by an earlier runner, which told the agent to merge: it goes with today's instructions, and the
            # changed text is screened afresh below.
            self.store.set_batch_text(batch.id, text)
            self.events.emit("review_batch_updated", task_id=task.task_id, batch=batch.id)
            batch = self.store.batch(batch.id)
        # The batch is stored, so a held round is screened once and waits as it is; others go on meanwhile.
        authors = [*json.loads(batch.authors or "[]"),
                   *(s.author for s in self.store.signals(task.task_id, batch.id) if s.kind in ("review", "ticket"))]
        clearance = self.d.gate.clear(task, "review", batch.text, authors=authors or [batch.author],
                                      resume_state="code_review", ref=batch.id)
        if clearance is None or not self.d._followup_ready(task):
            return
        reply = self.d.engine.send(task_id=task.task_id, message_id=batch.id, source=batch.source,
                                   author=batch.author, repo=Path(task.repo_path), text=batch.text,
                                   clearance=clearance, extra_dirs=self.d._extra_dirs_file(task))
        if reply.accepted:
            c = self.cfg.tracker
            has_ci = any(s.kind == "ci" for s in self.store.signals(task.task_id, batch.id))
            state_key = f"{task.task_id}:state:{batch.id}"
            with self.store.transaction():
                self.store.batch_sent(batch.id, self.d.now().isoformat())
                # A review round is counted when it ends: in published code (count_round) or in an answer.
                self.store.update_task(task.task_id, state="dispatched", attempt_started=self.d.now().isoformat(),
                                       ci_attempts=task.ci_attempts + (1 if has_ci else 0))
                # The ticket shows In Progress again; journaled, so a tracker failure is retried.
                self.store.action_intent(state_key, task.task_id, "state", json.dumps(
                    {"state": c.states.in_progress, "while": ["dispatched"]}))
            try:
                self.d.tracker.set_state(task.issue_db_id, c.states.in_progress)
                self.store.action_done(state_key)
            except ServiceError as e:
                self.events.emit("notify_pending", task_id=task.task_id, action=state_key, reason=str(e))
            self.events.emit("review_sent", task_id=task.task_id, batch=batch.id, tier=reply.tier,
                             attempt=reply.data.get("attempt"))
        elif reply.rc == 6:
            self.events.emit("review_busy", task_id=task.task_id, batch=batch.id)  # kept, sent next tick
        else:
            self.d.fail(task, f"the execution engine did not accept the review follow-up ({reply.why()})")

    def _threads(self, task: Task, signals: list[Signal]) -> tuple[list[str], list[str]]:
        """Each MR thread addressed to the agent in this round, whole: the earlier notes are its context.
        Also who wrote them: none of those people may release the round from a security hold."""
        labels = _thread_labels(signals)
        if not labels:
            return [], []
        mrs = {m.repo: m for m in self.store.task_mrs(task.task_id)}
        new = {s.id for s in signals if s.kind == "review"}
        notes: dict[tuple[str, str], list[Note]] = {}
        for repo in sorted({repo for repo, _ in labels}):
            mr = mrs.get(repo)
            if mr:
                for n in self.host.notes(mr.project_id, mr.iid, mr.url):
                    if not n.system:
                        notes.setdefault((repo, n.discussion_id), []).append(n)
        out, writers = [], []
        for key, label in labels.items():
            thread = notes.get(key)
            if thread:
                writers += [n.author for n in thread if n.author != self.cfg.code_host.bot_user]
                fresh = {str(n.id) for n in thread if _note_signal(mrs[key[0]], n.id) in new}
                out.append(self._render_thread(label, key[0], thread, fresh))
            else:  # the thread is gone from the code host since: what was asked is still in the signals
                asked = [s.body for s in signals if (s.repo, s.discussion_id) == key]
                out.append(f"### {label} · {key[0]}\n" + "\n".join(f"- NEW: {b}" for b in asked))
        return out, writers

    def _render_thread(self, label: str, repo: str, thread: list[Note], new: set[str]) -> str:
        """`new`: the ids of the notes that ask something of the agent in this round."""
        bot = self.cfg.code_host.bot_user
        first = thread[0]
        where = f" · {first.path}:{first.line}" if first.path else ""
        version = f" (on commit {first.commit[:12]})" if first.commit else ""
        lines = []
        for n in thread:
            who = "you (the runner posted this for you)" if n.author == bot else n.author
            body = _OUR_MARKER.sub("", n.body).strip()
            if len(body) > NOTE_CHARS:
                body = body[:NOTE_CHARS].rstrip() + " …"
            mark = "NEW, to you: " if str(n.id) in new else ""
            lines.append(f"- {mark}{who}: " + body.replace("\n", "\n  "))
        return "\n".join([f"### {label} · {repo}{where}{version}", first.url, *_fit(lines)])

    def _compose(self, task: Task, signals: list[Signal], batch_id: str | None = None,
                 threads: list[str] | None = None) -> str:
        lines = [f"Review of the MRs of task {task.task_id}."]
        if threads:
            lines += ["", "## Threads on the MRs addressed to you", "",
                      "Each thread is shown whole: earlier notes are its context; the notes marked NEW are "
                      "what is asked of you now.", ""]
            lines += "\n\n".join(threads).split("\n")
        for kind, title in KIND_TITLES.items():
            if kind == "review" and threads:
                continue
            mine = [s for s in signals if s.kind == kind]
            if mine:
                lines += ["", f"## {title}", *(f"- {current_instructions(s.body)}" for s in mine)]
        lines += ["", "## What to do", ""]
        if threads and batch_id:
            home = self.cfg.paths.workspace.resolve()
            lines += [
                "- Read each thread whole and decide what it asks. A request to change the code: change it. "
                "A question about the implementation, or about why you chose one approach over another: "
                "answer it, and do not change code for it. Do not change code only to have something to commit.",
                f"- What you decided while working on this ticket, and why, is in your notes, "
                f"{replies.decisions_file(home, task.task_id)}, if you kept them. When the reason for a choice "
                "is not written down there, say that you are reconstructing it from the code; never present "
                "a guess as the reason you had.",
                f"- Answer every thread in {replies.replies_file(home, task.task_id, batch_id)}, as JSON: "
                '{"replies": [{"thread": "T1", "changed": true, "text": "…"}]}. `changed` is true when your '
                "answer to that thread is a code change. `text` is your reply in that thread, to the person "
                "who asked, in English: what you changed, or the answer to their question. The runner posts "
                f"each answer in its thread; you cannot post in {self.host.label} yourself.",
            ]
        lines += [_ROUND_RULE]
        return "\n".join(lines)

    # -- after the round ------------------------------------------------------------------------

    def count_round(self, task: Task) -> Task:
        """The round just published changed code: it counts against max_review_rounds, once."""
        batch = self.store.last_sent_batch(task.task_id)
        if batch is None or batch.ci_only:
            return task
        return self._count(task, batch.id, "code")

    def _count(self, task: Task, batch_id: str, how: str) -> Task:
        key = f"{task.task_id}:round:{batch_id}"
        if self.store.action(key):
            return task
        with self.store.transaction():
            self.store.action_intent(key, task.task_id, "round")
            self.store.action_done(key, how)
            if how == "code":
                return self.store.update_task(task.task_id, review_rounds=task.review_rounds + 1)
            return self.store.update_task(task.task_id, review_answers=task.review_answers + 1)

    def _answers(self, task: Task, batch) -> dict[tuple[str, str], replies.Reply]:
        """The agent's answer for each thread of the round, keyed by (repo, discussion)."""
        signals = self.store.signals(task.task_id, batch.id)
        labels = _thread_labels(signals)
        path = replies.replies_file(self.cfg.paths.workspace.resolve(), task.task_id, batch.id)
        found, problems = replies.load(path, set(labels.values()))
        for line in problems:
            self.events.emit("reply_skipped", task_id=task.task_id, batch=batch.id, detail=line)
        return {key: found[label] for key, label in labels.items() if label in found}

    def _answer_threads(self, task: Task, batch, body) -> None:
        """One reply in each thread of the round: `body(answer or None, repo)` says what it is."""
        mrs = {m.repo: m for m in self.store.task_mrs(task.task_id)}
        answers = self._answers(task, batch)
        answered: set[tuple[str, str]] = set()
        for s in self.store.signals(task.task_id, batch.id):
            if not s.discussion_id or s.repo not in mrs:
                continue
            key = (s.repo, s.discussion_id)
            if s.replied or key in answered:
                answered.add(key)
                self.store.mark_replied(s.id)  # two notes in one thread get one reply
                continue
            answered.add(key)
            self._reply_once(task, s, mrs[s.repo], replies.unattributed(body(answers.get(key), s.repo))[:8000])

    def after_publish(self, task: Task, published: dict[str, str]) -> None:
        """Answer in each thread of the last round: the agent's answer, and the commit if it changed code."""
        batch = self.store.last_sent_batch(task.task_id)
        if not batch:
            return
        mrs = {m.repo: m for m in self.store.task_mrs(task.task_id)}

        def body(answer: replies.Reply | None, repo: str) -> str:
            sha = (published.get(repo) or mrs[repo].published_sha)[:12]
            if answer is None:
                return f"Fixed in {sha}."
            if answer.changed:
                return f"{answer.text}\n\nChanged in {sha}."
            return answer.text

        self._answer_threads(task, batch, body)

    def answered_without_changes(self, task: Task, summary: str) -> None:
        """The agent decided nothing had to change: its answers go to the threads, the task stays in Code Review."""
        batch = self.store.last_sent_batch(task.task_id)
        text = ("The agent looked into this and did not change the code: "
                f"{replies.unattributed(summary).strip() or 'no explanation given'}")
        if batch:
            self._answer_threads(task, batch, lambda answer, repo: answer.text if answer else text[:3000])
            if not batch.ci_only:
                task = self._count(task, batch.id, "answer")
        visuals = self.d.refresh_media(task)
        self.d.comment_with_media(task, f"nochange:{batch.id if batch else 'none'}", text[:3000], visuals)
        self.d.tracker.set_state(task.issue_db_id, self.cfg.tracker.states.code_review)
        self.store.update_task(task.task_id, state="code_review", reason=None)
        self.events.emit("review_answered", task_id=task.task_id, changed=False)

    def _reply_once(self, task: Task, s: Signal, mr, body: str) -> None:
        key = f"{task.task_id}:reply:{s.id}"
        act = self.store.action(key)
        if not act or act[0] != "done":
            payload = {"signal": s.id, "repo": s.repo, "discussion": s.discussion_id, "body": body}
            if not act:
                self.store.action_intent(key, task.task_id, "reply", json.dumps(payload))
            self.retry_reply(task, key, payload)
        self.store.mark_replied(s.id)

    def retry_reply(self, task: Task, key: str, payload: dict) -> None:
        """Post a thread reply unless it is already there: the marker survives a crash after the post."""
        mr = {m.repo: m for m in self.store.task_mrs(task.task_id)}.get(payload["repo"])
        if not mr:
            self.store.action_done(key)
            return
        marker = f"<!-- arceus:{payload['signal']} -->"
        if not any(marker in n.body and n.author == self.cfg.code_host.bot_user
                   for n in self.host.notes(mr.project_id, mr.iid, mr.url) if n.discussion_id == payload["discussion"]):
            self.host.reply(mr.project_id, mr.iid, payload["discussion"], f"{payload['body']}\n\n{marker}")
        self.store.action_done(key)
        self.store.mark_replied(payload["signal"])

    def note_moved(self, task: Task, repo: str, remote: str, sha: str, published: str | None = None,
                   rewritten: bool = False) -> None:
        """People pushed to the MR branch while the agent worked: hand it to the agent to bring their commits in.

        `rewritten`: the agent's work no longer builds on the runner's last push there (it rebased the branch).
        Their commits then go on top of it, so the runner may publish it over theirs (Delivery._replaceable);
        otherwise the agent's own commits go on top of theirs and the push is a plain one.
        """
        now = self.d.now().isoformat()
        host = self.host.label
        sig_id = f"moved-{repo}-{remote}"
        sig = self.store.signal(sig_id)
        if sig and sig.batch_id:
            batch = self.store.batch(sig.batch_id)
            if batch and batch.sent_at and batch.sent_at != "stale":
                self.d.fail(task, f"the agent's work in {repo} does not visibly keep the commits people pushed "
                                  "to the MR branch",
                            f"on {host} {remote[:12]}, reviewed {sha[:12]}; nothing was pushed, their commits stay.")
                return
        if rewritten and published:
            how = (f"Your branch is rebased, so put their commits on top of it: git fetch origin && "
                   f"git cherry-pick {published[:12]}..origin/{task.branch}, resolving any conflicts.")
        else:
            how = _pull_rebase(task.branch)
        text = (f"Branch {task.branch} of {repo} on {host} has commits that you do not have "
                f"({host} {remote[:12]}, yours {sha[:12]}). {how} {_MOVED_TAIL}")
        self.store.add_signal(sig_id, task.task_id, "moved", self.host.name, text, now, repo=repo)
        self.store.update_task(task.task_id, state="code_review")
        self.events.emit("review_signal", task_id=task.task_id, kind="moved", repo=repo)
