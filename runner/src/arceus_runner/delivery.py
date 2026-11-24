"""Live delivery: a queued ticket becomes an MR with the commit Codex reviewed.

Every step can be repeated. The runner's own state is in SQLite, Bulava's in
`task show`; after a crash the next tick looks at both and finishes what is
missing instead of doing anything twice:

- a ticket comment goes through the action journal and carries a marker, so a
  comment posted just before a crash is found instead of posted again;
- a dispatch or a follow-up has a stable message id, Bulava answers a repeat
  with `duplicate`;
- a push is checked against the remote first, an MR is looked up before it is
  created.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from arceus_runner import materials, media, platforms
from arceus_runner.replies import unattributed
from arceus_runner.config import Config
from arceus_runner.db import REVIEW_UNDER_WAY, TaskStore, Task, TriggerDecision
from arceus_runner.engine import Engine, EngineError, Reply
from arceus_runner.events import EventLog
from arceus_runner.gitops import HOME_BRANCH, Git, GitError
from arceus_runner.progress import Liveness, task_key
from arceus_runner.http import BadRequest, NotFound, ServiceError
from arceus_runner.integrations import (
    CodeHost,
    Comment,
    Issue,
    IssueDetails,
    IssueTracker,
    MergeRequest,
    Repo,
    key_part,
)
from arceus_runner.screening import Classify, Gate, is_decision, trusted
from arceus_runner.publish import Publisher
from arceus_runner.workspace import AgentContext, Workspace
from arceus_runner.texts import (
    ANSWER_BYTES,
    QUESTION_KEEP,
    START_REFUSED,
    comments_section,
    log_hint,
    no_change_foot,
    no_change_question,
    parent_section,
    parse_ts,
    piece_kind,
    research_brief,
    review_findings,
    review_question,
    review_stop,
    short_text,
    slugify,
    split_answer,
    stop_question,
    ticket_media_text,
    ticket_pieces,
)

FAIL_REASONS = {
    "debt": "the work did not pass review",
    "codex_unavailable": "the review step is unavailable",
    "scope_violation": "the agent went outside the task's scope",
    "stalled": "the agent's session stopped at its prompt without finishing, again after being asked to finish",
}
# How Bulava's review gate words the stops the runner tells apart (hooks/review-gate.sh, `receipt.review_why` and the
# headings in `blocked.md`). tests/test_real_gate.py fails when an engine update changes one of them.
GATE_NO_CHANGE = "Declared 'succeeded_no_change' with an empty diff"
GATE_UNREACHABLE = "reviewer unreachable"
GATE_UNCOMMITTED = "uncommitted work after"
GATE_WORKER_FAILED = "worker declared: failed"
GATE_WORKER_BLOCKER = "worker blocker finding:"
GATE_STALLED = "(stalled:"
GATE_FAIL = "VERDICT: FAIL"
GATE_VERIFIER = "[VERIFIER OVERRIDE]"
GATE_STOPPED = "the review stopped the work without a decision the runner can act on"  # any other `needs-user`
STALLED_NUDGE = ("Your previous session stopped at its prompt without finishing the turn, and Bulava parked it. "
                 "Pick the task up from where the work stands: finish it, commit in every repository you "
                 "changed, and declare the outcome.")
RESUME = "The session was interrupted (a reboot or a crash). Continue the task from where you stopped."
CONTINUE = "/continue"  # a comment that starts with this, from someone allowed, resumes a failed task (_continuable)
# Which client a journaled action goes through: one that fails holds back only its own actions (retry_pending).
# Reactions have their own client on the code host (GitLabHost.reactions_client); the rest go to the tracker.
ACTION_CLIENTS = {"reply": "code_host", "mr_react": "reactions", "merge": "code_host"}
TAKEN = "eyes"  # the reaction on a command the runner took: the person sees it was seen and will be done


# The review gate's block in its findings for the heavy checks registered with `add-check --final`
# (engine/hooks/review-gate.sh).
GATE_FINAL = "[FINAL CHECKS]"
STALL_LIMIT = 2  # the gate's SUPERVISOR_STALL_LIMIT by default: rounds without progress before it parks the run


def _hours(d) -> str:
    """A limit as the ticket says it: "8 h", "90 min"."""
    s = d.total_seconds()
    return f"{s / 3600:.0f} h" if s >= 3600 and s % 3600 == 0 else f"{s / 60:.0f} min"


FETCHED_KEPT = 4  # deliveries whose fetched files are kept in memory while they wait (Delivery._fetched): at most
                  # 4 × materials.MAX_TOTAL_BYTES
class Delivery:
    def __init__(self, cfg: Config, store: TaskStore, events: EventLog, tracker: IssueTracker, host: CodeHost,
                 engine: Engine, git: Git, classify: Classify, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.cfg = cfg
        self.store = store
        self.events = events
        self.tracker = tracker
        self.host = host
        self.engine = engine
        self.git = git
        self.now = now
        self._skipped: set[tuple[str, str]] = set()  # (issue key, tag event) not takeable: not tried again
        self._engine_seen: dict[str, tuple] = {}  # task id → the engine's state last written as engine_state
        self.published: set[str] = set()  # tasks published since the loop last read their MRs (loop.tick)
        # (task id, comment ids) → the files as fetched, and what the tracker said of them: a follow-up that waits
        # for the workspace is not downloaded again on every tick.
        self._fetched: dict[tuple, tuple[tuple, list[materials.Material]]] = {}
        self.gate = Gate(self, classify)  # every text from people or CI passes it before the engine sees it
        self.workspace = Workspace(cfg, store, events, git, engine, host, now)
        self.publisher = Publisher(cfg, store, events, git, host, tracker, self.workspace, fail=self.fail,
                                   comment_with_media=self.comment_with_media, hold_back=self.platforms_missing,
                                   journal_merges=self.journal_merges, arm_merges=self.arm_merges)
        self.reviewer = None  # code_review.ReviewAgent, set by the loop: told of MRs and merges (review.py)
        # progress.ProgressWatcher.liveness, set by the loop: an attempt's newest sign of life, for its timeout.
        # Without it an attempt is timed from its start.
        self.liveness: Callable[[str, str], Liveness | None] | None = None

    @property
    def review(self):
        """review.ReviewLoop, set by the loop: the MRs' review rounds. Publishing reports to it too."""
        return self.publisher.review

    @review.setter
    def review(self, loop) -> None:
        self.publisher.review = loop

    def agent_context(self) -> AgentContext:
        """What a background agent works with (workspace_run.py): the services and the workspace, not this."""
        return AgentContext(cfg=self.cfg, store=self.store, events=self.events, now=self.now, engine=self.engine,
                            git=self.git, host=self.host, workspace=self.workspace, gate=self.gate)

    def taken(self, task: Task, comment_id: str) -> None:
        """Mark a person's command the runner has just taken with a reaction, so they see it is queued. Journaled
        with the change that takes it (call it inside that transaction); retry_pending() puts it there."""
        self.store.action_intent(f"{task.task_id}:react:{key_part(comment_id, keep_dash=True)}", task.task_id,
                                 "react", json.dumps({"comment": comment_id, "reaction": TAKEN}))

    def mr_taken(self, task: Task, mr, note_id) -> tuple[str, dict] | None:
        """The same for a request in a thread of one of the task's merge requests: a reaction on that note, by the
        bot. Journaled with the signal (call it inside that transaction); react_mr() puts it there. Returns the
        journal key and what it holds, or None when the code host has no reactions."""
        if not getattr(self.host, "react_note", None):
            return None
        key = f"{task.task_id}:mr_react:{key_part(mr.project_id)}-{key_part(note_id)}"
        data = {"repo": mr.repo, "project": mr.project_id, "iid": mr.iid, "note": note_id, "reaction": TAKEN}
        self.store.action_intent(key, task.task_id, "mr_react", json.dumps(data))
        return key, data

    def react_mr(self, task_id: str, key: str, data: dict) -> None:
        """Put a journaled reaction on its merge request note. A note that is gone or a reaction the host refuses
        ends it with an event; an outage raises and leaves it for retry_pending()."""
        if react := getattr(self.host, "react_note", None):
            try:
                react(data["project"], data["iid"], data["note"], data["reaction"])
            except (NotFound, BadRequest) as e:
                self.events.emit("reaction_refused", task_id=task_id, repo=data.get("repo"), note=data["note"],
                                 reason=str(e))
        self.store.action_done(key)

    # -- comments: once per (task, kind), even across a crash -----------------------------------


    def journal_merges(self, task: Task, published: list[tuple[Repo, MergeRequest, str]]) -> list[tuple[str, dict]]:
        """tracker.tags.merge on the ticket, put there by someone in allowed_users: each reviewed MR is to merge
        once its pipeline passes (where the default branch deploys dev by itself: "deploy it to dev"). Written to
        the action journal, one entry per published commit, before the ticket moves on (Publisher.publish), so a
        crash or an unwell host only delays it."""
        c = self.cfg.tracker
        tag = c.tags.merge
        if not published or tag not in self.tracker.tags(task.issue_db_id):
            return []
        who = self.tracker.tag_author(task.issue_db_id, tag)
        if who not in c.allowed_users:
            shas = "-".join(sha[:10] for _, _, sha in published)
            self.events.emit("merge_refused", task_id=task.task_id, by=who or None)
            self.comment_once(task, f"merge-refused:{shas}",
                              f"The `{tag}` tag was put on by {who or 'someone the tracker does not name'}, who is "
                              f"not among the people who may start the agent, so nothing was merged. The merge "
                              f"requests wait for people as usual.")
            return []
        out = []
        for repo, mr, sha in published:
            key = f"{task.task_id}:merge:{key_part(repo.id)}:{key_part(mr.iid)}:{sha[:12]}"
            act = self.store.action(key)
            if act and act[0] == "done":
                continue
            data = {"repo_id": repo.id, "repo": repo.relative_path, "iid": mr.iid, "url": mr.url, "sha": sha,
                    "target": repo.default_branch or "", "by": who, "tag": tag}
            if not act:
                self.store.action_intent(key, task.task_id, "merge", json.dumps(data))
            out.append((key, data))
        return out

    def arm_merges(self, task: Task, merges: list[tuple[str, dict]]) -> None:
        """The journaled merges of a publish, once the ticket is in Code Review. A host that is unwell leaves the
        rest in the journal for retry_pending."""
        for key, data in merges:
            try:
                self._arm_merge(task, key, data)
            except ServiceError as e:
                self.events.emit("notify_pending", task_id=task.task_id, action=key, reason=str(e))
                break

    def _arm_merge(self, task: Task, key: str, data: dict) -> None:
        """Set one journaled MR to merge when green, after checking it is still the reviewed change into the
        default branch. A refusal (by these checks or by the host) is final and said on the ticket; a host that is
        unwell raises ServiceError and the journal entry waits for the next try."""
        st = self.host.mr_status(data["repo_id"], data["iid"])
        if st.state == "merged":
            result = "merged"
        elif st.state != "opened":
            result = "refused: the merge request is closed"
        elif not data["target"] or st.target != data["target"]:
            result = f"refused: it merges into `{st.target}`, not the default branch `{data['target'] or '?'}`"
        elif st.sha != data["sha"]:
            result = "refused: it has commits the review did not see"
        else:
            try:
                self.host.merge_when_green(data["repo_id"], data["iid"], data["sha"])
                result = "set"
            except (BadRequest, NotFound) as e:
                result = f"refused: {str(e)[:300]}"
        self.store.action_done(key, json.dumps({"result": result, "by": data["by"]}))
        self.events.emit("merge_when_green" if result in ("set", "merged") else "merge_when_green_refused",
                         task_id=task.task_id, repo=data["repo"], mr_iid=data["iid"], sha=data["sha"],
                         detail=None if result == "set" else result)
        said = {"set": "merges into the default branch as soon as its pipeline passes",
                "merged": "is already merged"}.get(result, f"not set to merge ({result.removeprefix('refused: ')})")
        self.comment_once(task, f"merge:{key_part(data['repo_id'])}:{key_part(data['iid'])}:{data['sha'][:10]}",
                          f"Asked by {data['by']} with `{data['tag']}`: {data['url']} {said}. What the default "
                          f"branch deploys by itself (dev) follows its merge.")

    def comment_once(self, task: Task, kind: str, text: str) -> tuple[str, int] | None:
        """Post a comment unless it is already there. Returns (id, created ms) of the comment."""
        key = f"{task.task_id}:comment:{kind}"
        marker = f"[arceus:{kind}]"
        act = self.store.action(key)
        if act and act[0] == "done":
            return tuple(json.loads(act[1])) if act[1] else None
        if act:  # intent without done: it may have been posted before a crash
            for c in self.tracker.comments(task.issue_db_id):
                if c.author == self.cfg.tracker.bot_user and marker in c.text:
                    self.store.action_done(key, json.dumps([c.id, c.created]))
                    return c.id, c.created
        self.store.action_intent(key, task.task_id, "comment", json.dumps({"kind": kind, "text": text}))
        cid, created = self.tracker.add_comment(task.issue_db_id, f"{text}\n\n{marker}")
        self.store.action_done(key, json.dumps([cid, created]))
        return cid, created

    def tag_once(self, task: Task, tag: str, key: str) -> None:
        act = self.store.action(key)
        if act and act[0] == "done":
            return
        self.store.action_intent(key, task.task_id, "tag", json.dumps({"tag": tag}))
        self.tracker.add_tag(task.issue_db_id, tag)
        self.store.action_done(key)

    # -- tags at the runner's own steps (tracker.tags_on) -----------------------------------------

    def tags_on_plan(self, task: Task, step: str, occurrence: str = "") -> list[tuple[str, dict]]:
        """The journal keys and payloads of the tags `tracker.tags_on.<step>` adds and removes, in the configured
        order. `occurrence` tells apart steps that happen again on the same ticket (a research ticket's runs)."""
        change = getattr(self.cfg.tracker.tags_on, step)
        at = f"{step}:{occurrence}" if occurrence else step
        return [(f"{task.task_id}:tags_on:{at}:{'add' if add else 'remove'}:{key_part(tag)}",
                 {"tag": tag, "add": add, "step": step})
                for add, names in ((True, change.add), (False, change.remove)) for tag in names]

    def journal_tags_on(self, task: Task, step: str, occurrence: str = "") -> list[str]:
        """Record the step's tags; call it inside the transaction of the step itself, so a crash cannot lose them.
        Returns their keys for apply_tags_on()."""
        plan = self.tags_on_plan(task, step, occurrence)
        for key, data in plan:
            self.store.action_intent(key, task.task_id, "step_tag", json.dumps(data))
        return [key for key, _ in plan]

    def apply_tags_on(self, task: Task, keys: list[str]) -> None:
        """Put the journaled step tags on the ticket. A tracker outage leaves them, in order, to retry_pending()."""
        for key in keys:
            act = self.store.action(key)
            if not act or act[0] == "done":
                continue
            try:
                self._step_tag(task, key, json.loads(act[1]))
            except ServiceError as e:
                self.events.emit("notify_pending", task_id=task.task_id, action=key, reason=str(e))
                return

    def _step_tag(self, task: Task, key: str, data: dict) -> None:
        """One step tag. A tag the tracker does not have or refuses ends it with an event (the health check names
        it); it never holds the task or the tags after it."""
        try:
            if data["add"]:
                self.tracker.add_tag(task.issue_db_id, data["tag"])
            else:
                self.tracker.remove_tag(task.issue_db_id, data["tag"])
        except (LookupError, BadRequest, NotFound) as e:
            self.events.emit("ticket_tag_refused", task_id=task.task_id, step=data["step"], tag=data["tag"],
                             added=data["add"], reason=str(e)[:300])
            self.store.action_done(key, "refused")
            return
        self.store.action_done(key)
        self.events.emit("ticket_tagged", task_id=task.task_id, step=data["step"], tag=data["tag"], added=data["add"])

    def retry_pending(self) -> None:
        """Finish ticket and merge request updates that a failure or a crash left half done. A service that fails
        keeps the rest of its own updates waiting, in their order, for the next tick; the others go on."""
        unwell: set[str] = set()
        for key, task_id, kind, payload in self.store.pending_actions():
            side = ACTION_CLIENTS.get(kind, "tracker")
            task = self.store.task(task_id)
            if not task or not payload or side in unwell:
                continue
            data = json.loads(payload)
            try:
                if kind == "comment":
                    self.comment_once(task, data["kind"], data["text"])
                elif kind == "tag":
                    self.tracker.add_tag(task.issue_db_id, data["tag"])
                    self.store.action_done(key)
                elif kind == "untag":
                    self.tracker.remove_tag(task.issue_db_id, data["tag"])
                    self.store.action_done(key)
                elif kind == "step_tag":
                    self._step_tag(task, key, data)
                elif kind == "state":
                    if task.state in data.get("while", [task.state]):  # never move a ticket back past a later step
                        self.tracker.set_state(task.issue_db_id, data["state"])
                    self.store.action_done(key)
                elif kind == "reply" and getattr(self, "review", None):
                    self.review.retry_reply(task, key, data)
                elif kind == "react":
                    if react := getattr(self.tracker, "react", None):
                        try:
                            react(task.issue_db_id, data["comment"], data["reaction"])
                        except (NotFound, BadRequest) as e:  # the comment is gone, or the tracker refuses it
                            self.events.emit("reaction_refused", task_id=task_id, comment=data["comment"],
                                             reason=str(e))
                    self.store.action_done(key)
                elif kind == "mr_react":
                    self.react_mr(task_id, key, data)
                elif kind == "merge":
                    self._arm_merge(task, key, data)
            except ServiceError as e:
                self.events.emit("notify_pending", task_id=task_id, action=key, reason=str(e))
                unwell.add(side)  # still unwell; the next tick tries again

    # -- taking a task --------------------------------------------------------------------------

    def maybe_start(self, can_take: bool) -> None:
        if not can_take or self.workspace.taken():
            return
        if self.store.has_pending_review() or self._continues_waiting() or self.workspace.others_waiting():
            return  # fixes for open MRs, resumed tasks, requested code reviews and background runs go first
        for decision in self.store.accepted_untaken():
            if self.skipped(decision):
                continue
            if self.start(decision):
                return

    def skipped(self, decision: TriggerDecision) -> bool:
        return (decision.issue_key, decision.tag_activity_id) in self._skipped

    def start(self, decision: TriggerDecision) -> bool:
        """Take one queued ticket. False if it is not takeable now (and was skipped)."""
        c = self.cfg.tracker
        details = self.tracker.issue(decision.issue_key)
        tags = self.tracker.tags(details.db_id)
        if details.state != c.states.queued or c.tags.queue not in tags:
            return self._not_taken(decision, f"no longer queued (state {details.state!r}, "
                                             f"tag present: {c.tags.queue in tags})")
        # The decision was made when the tag was seen. Whoever put it on since is who queued the ticket now:
        # someone the tracker cannot name, or someone not on the list, does not inherit an earlier approval.
        now = self.tracker.trigger(Issue(details.db_id, details.key, details.summary, 0, 0))
        if now is None or now.id != decision.tag_activity_id:
            return self._not_taken(decision, "the tag was set again since it was approved, "
                                             f"{f'by {now.author}' if now and now.author else 'by someone unknown'}")
        if now.author not in c.allowed_users:
            return self._not_taken(decision, f"{now.author} is no longer in allowed_users")
        known = self.store.task(details.key)
        if known is not None and known.people_work:
            # Only its merge requests were reviewed so far (people wrote them). It becomes the agent's task once no
            # review of it runs or waits; the reviews and what they posted stay on record.
            if known.state == "security_hold" or self.store.reviews(*REVIEW_UNDER_WAY, task_id=known.task_id):
                return False
            self.store.forget_task(known.task_id)
            self.events.emit("review_ticket_taken_over", task_id=known.task_id)

        # A question instead of a change: decided once, here, and kept for every follow-up and restart.
        kind = "research" if c.tags.research in tags else "code"
        base = dict(task_id=details.key, issue_db_id=details.db_id, summary=details.summary[:300],
                    tag_author=decision.author, base_branch="", question_id=None, question_at_ms=None,
                    reason=None, mr_urls="[]", attempt_started=None, kind=kind)
        repos = self.workspace.group()
        if not repos:
            task = self.store.create_task(**base, state="claimed", repo="", repo_path="", branch="",
                                          extra_repos="[]")
            self.fail(task, f"the {self.host.scope_kind} {self.host.scope} has no repositories (after exclude_repos)")
            return True

        # The ticket does not say where the work is, and no repository is "the main one": the session
        # starts in the workspace folder holding all of them, and the agent changes whichever the
        # ticket needs. MRs follow only real changes. (`repo` is empty for such a task: older tasks
        # kept the repository they started in there.)
        names = sorted(r.relative_path for r in repos)
        base.update(base_branch=HOME_BRANCH)
        task = self.store.create_task(
            **base, state="claimed", repo="", repo_path=str(self.cfg.paths.workspace),
            branch=f"{self.cfg.code_host.branch_prefix}{details.key}-{slugify(details.summary)}",
            extra_repos=json.dumps(names))
        self.events.emit("task_claimed", task_id=task.task_id, author=task.tag_author, repos=names,
                         branch=task.branch, kind=task.kind)
        self.workspace.notes_file(task).unlink(missing_ok=True)  # left by a runner.db made anew: not this task's
        self._take(task, repos, details)
        return True

    def _not_taken(self, decision: TriggerDecision, reason: str) -> bool:
        self._skipped.add((decision.issue_key, decision.tag_activity_id))
        self.events.emit("task_not_taken", task_id=decision.issue_key, reason=reason)
        return False

    def _take(self, task: Task, repos: list[Repo], details: IssueDetails) -> None:
        # Screened as it is now, at the start, not when it was tagged: anyone may have edited it since.
        # A held ticket is not claimed: it stays in the queue state, its workspace untouched.
        # The comments and the files people attached are part of what the ticket asks: screened with it, in one go.
        comments = self._people_comments(task)
        parent = self._parent(details)
        files = self.ticket_files(task, None, comments)
        text = f"{task.task_id}: {details.summary}\n\n{details.description}".strip()
        tags = self._ticket_tags(task)
        if tags:
            text += "\n\n" + self._tags_section(tags)
        if parent:
            text += "\n\n" + parent_section(parent)
        if comments:
            text += "\n\n" + comments_section(comments)
        brief = research_brief(self.answer_file(task, self.research_run(task))) if task.research else ""
        if files:
            texts = [("the description", None, details.description),
                     *((f"the comment by {x.author} of {materials._when(x.created)}", str(x.id), x.text) for x in comments)]
            # The research brief goes below the files and counts with them against what the screen reads.
            text, files = materials.with_section(text, files, self._files_folder(task), texts, after=brief)
        elif brief:
            text += "\n\n" + brief
        authors = [task.tag_author, *details.writers, *(parent.writers if parent else ()), *(x.author for x in comments),
                   *(m.attachment.author for m in files)]
        clearance = self.gate.clear(task, "ticket", text, authors=authors, resume_state="claimed",
                                    files=materials.screen_files(files))
        if clearance is None:
            return
        why = self.workspace.prepare(task, repos)
        if why:
            self.fail(task, why)
            return
        self.publish_files(task, files, fresh=True)
        self._remember_tags(task, tags)
        self.claim_on_ticket(task)
        self.dispatch(task, text, clearance)


    def _ticket_tags(self, task: Task) -> list[str]:
        """The ticket's tags as they are now, the runner's own left out."""
        return platforms.ticket_tags(self.tracker.tags(task.issue_db_id), self.cfg.tracker.tags)

    def _tags_section(self, tags: list[str], reviewer: bool = False) -> str:
        present = [r.relative_path for r in self.workspace.group()] if self.cfg.tracker.platforms and not reviewer else []
        return platforms.section(tags, self.cfg.tracker.platforms, present, reviewer=reviewer)

    def _remember_tags(self, task: Task, tags: list[str]) -> None:
        """The tags the agent was last told of, so a follow-up says so when people changed them."""
        key = f"{task.task_id}:tags"
        with self.store.transaction():
            self.store.action_intent(key, task.task_id, "tags")
            self.store.action_done(key, json.dumps(tags))

    def _changed_tags(self, task: Task) -> tuple[str, list[str] | None]:
        """For a follow-up: the tags section when the ticket's tags are not the ones the agent was told of, and
        the tags to remember once the follow-up is sent ("", None when they are the same)."""
        tags = self._ticket_tags(task)
        known = self.store.action(f"{task.task_id}:tags")
        told = json.loads(known[1]) if known and known[1] else []
        if tags == told:
            return "", None
        if not tags:
            return "The ticket's tags changed since you started: it has no tags now.", tags
        return "The ticket's tags changed since you started.\n\n" + self._tags_section(tags), tags

    def platforms_missing(self, task: Task, changed: set[str], attempt: int) -> bool:
        """The ticket's tags name a platform (tracker.platforms) none of whose repositories has changes (`changed`:
        the repositories of this round's commits and of the task's MRs): nothing is published. The first time the
        work goes back to the agent with what is missing; after that the ticket comes back to people, who can
        /continue it or take the tag off. True when publishing stops here (Publisher.publish asks before any push)."""
        if not self.cfg.tracker.platforms:
            return False
        tags = self._ticket_tags(task)
        gaps = platforms.missing(tags, self.cfg.tracker.platforms, changed)
        if not gaps:
            return False
        what = platforms.describe_missing(gaps)
        done = ", ".join(f"`{r}`" for r in sorted(changed))
        self.events.emit("platforms_missing", task_id=task.task_id, attempt=attempt, missing=sorted(gaps),
                         changed=sorted(changed))
        key = f"{task.task_id}:platforms"
        if not self.store.action(key):
            sent = self._platforms_back(task, key, what, done, tags)
            if sent != "refused":
                return True
        self.fail(task, "the ticket's tags name a platform the work did not change, so nothing was published",
                  f"Tagged, with no changes: {what}.\nThe changes are in: {done}.\n"
                  "If the ticket needs no work for that platform, take its tag off before you continue.",
                  attempt=attempt)
        return True

    def _platforms_back(self, task: Task, key: str, what: str, done: str, tags: list[str]) -> str:
        """The work back to the agent once, with what the tags ask: "sent", "wait" (the workspace or Bulava is
        busy: the task stays in publishing and is looked at again) or "refused"."""
        text = (f"The runner did not publish your work. The ticket is tagged for platforms your changes do not "
                f"touch: {what}. You changed only {done}. Do this ticket's work for each tagged platform in its "
                "repository, then finish as before; the work you already did stays. If a tagged platform truly "
                "needs no change, do not finish: ask (needs_input) and say why.\n\n"
                + self._tags_section(list(platforms.tagged_platforms(tags, self.cfg.tracker.platforms))))
        if not self.workspace.followup_ready(task):
            return "wait"
        reply = self.engine.send(task_id=task.task_id, message_id=f"platforms-{task.task_id}", source="other",
                                 author=self.cfg.tracker.bot_user, repo=Path(task.repo_path), text=text,
                                 clearance=trusted(text), extra_dirs=self.workspace.extra_dirs_file(task))
        if reply.rc == 6:
            self.events.emit("send_busy", task_id=task.task_id, reason=reply.why())
            return "wait"
        if not reply.accepted:
            self.events.emit("platforms_back_refused", task_id=task.task_id, reason=reply.why())
            return "refused"
        with self.store.transaction():
            self.store.action_intent(key, task.task_id, "platforms")
            self.store.action_done(key, json.dumps({"attempt": reply.data.get("attempt")}))
            self.store.update_task(task.task_id, state="dispatched", attempt_started=self.now().isoformat())
        self._remember_tags(task, tags)
        self.events.emit("platforms_sent_back", task_id=task.task_id, attempt=reply.data.get("attempt"))
        return "sent"

    # -- what people wrote and attached on the ticket ---------------------------------------------

    def _people_comments(self, task: Task) -> list[Comment]:
        """The ticket's comments from people, oldest first: not the runner's own, not a security reviewer's decision,
        not a bare `/continue` (a command to the runner that says nothing of the work, and would make a text a
        reviewer rejected look new), not a request to the code reviewer (its, as for an answer: check_answers)."""
        bot = self.cfg.tracker.bot_user
        return [x for x in self.tracker.comments(task.issue_db_id) if x.author != bot and not is_decision(x.text)
                and x.text.strip().lower() != CONTINUE
                and not (self.reviewer and self.reviewer.claims(task.task_id, x))]

    def _parent(self, details: IssueDetails) -> IssueDetails | None:
        """The ticket this one is a subtask of, when this one has no description: a "Development" subtask of a
        feature says nothing itself, and what it asks is in its parent."""
        if details.description.strip() or not details.parent:
            return None
        try:
            return self.tracker.issue(details.parent)
        except NotFound:
            return None

    def _files_folder(self, task: Task) -> Path:
        return materials.folder(self.cfg.paths.workspace.resolve(), task.task_id)

    def ticket_files(self, task: Task, only: set[str] | None, comments: list[Comment]) -> list[materials.Material]:
        """The files on the ticket for one delivery, fetched: on the ticket itself and on `comments` when `only` is
        None (the start), or only those on the comments with the ids in `only` (a follow-up). Files the runner
        attached itself are its own screenshots, not material. Empty when the tracker cannot read attachments."""
        if not getattr(self.tracker, "reads_attachments", False):
            return []
        bot = self.cfg.tracker.bot_user
        where: dict[str | None, str] = {None: "the ticket"}
        where |= {str(x.id): f"a comment by {x.author}" + (f" of {materials._when(x.created)}" if x.created else "")
                  for x in comments}
        mine = [a for a in self.tracker.attachments(task.issue_db_id) if a.author != bot
                and (a.comment_id in only if only is not None else (a.comment_id is None or a.comment_id in where))]
        if not mine:
            return []
        key = (task.task_id, None if only is None else frozenset(only))
        seen = tuple((a.id, a.name, a.size, a.created, a.comment_id) for a in mine)
        kept = self._fetched.get(key)
        if kept and kept[0] == seen:
            return kept[1]
        items = materials.fetch(self.tracker, mine, where)
        self._fetched.pop(key, None)
        if all(m.status != "unavailable" for m in items):  # a file the bot could not read is asked for again
            self._fetched[key] = (seen, items)
            while len(self._fetched) > FETCHED_KEPT:
                del self._fetched[next(iter(self._fetched))]
        self.events.emit("ticket_files_fetched", task_id=task.task_id, files=len(items),
                         given=sum(m.status == "ok" for m in items),
                         skipped=[f"{m.attachment.id}: {m.reason}" for m in items if m.status != "ok"])
        return items

    def publish_files(self, task: Task, files: list[materials.Material], fresh: bool = False) -> None:
        """The screened files into the workspace, right before the text that names them goes to the agent."""
        if files or fresh:
            materials.publish(files, self.cfg.paths.workspace.resolve(), task.task_id, fresh=fresh)
        for key in [k for k in self._fetched if k[0] == task.task_id]:
            del self._fetched[key]  # delivered: the bytes are on disk, nothing waits for them any more

    def commented_files(self, task: Task) -> set[str]:
        """The ids of the comments people attached files to: such a comment says something even without text."""
        if not getattr(self.tracker, "reads_attachments", False):
            return set()
        bot = self.cfg.tracker.bot_user
        return {a.comment_id for a in self.tracker.attachments(task.issue_db_id) if a.comment_id and a.author != bot}

    def answer_file(self, task: Task, run: int) -> Path:
        """Where the agent writes its answer to a research ticket in run `run`: the workspace's notes folder for the
        ticket, which no repository holds and the cleanup knows. A file of its own per run, so what an earlier run
        left there can never be taken for this run's answer."""
        return self.cfg.paths.workspace.resolve() / ".arceus" / "notes" / task.task_id / f"answer-{run}.md"

    def claim_on_ticket(self, task: Task) -> None:
        self.tracker.set_state(task.issue_db_id, self.cfg.tracker.states.in_progress)
        try:
            self.tracker.assign_to_bot(task.issue_db_id)
        except BadRequest as e:
            self.events.emit("assignee_not_set", task_id=task.task_id, reason=str(e))
        n = len(self.workspace.names(task))
        if task.research:
            self.comment_once(task, "claim", f"The agent has taken this ticket as a question: it reads the code of the "
                                             f"{n} {'repository' if n == 1 else 'repositories'} of "
                                             f"{self.host.scope_kind} `{self.host.scope}` and answers here. No merge "
                                             "request will be opened.")
            return
        self.comment_once(task, "claim", f"The agent has taken this ticket: branch `{task.branch}` in the "
                                         f"{n} {'repository' if n == 1 else 'repositories'} of "
                                         f"{self.host.scope_kind} `{self.host.scope}`. It decides which of them "
                                         "the ticket needs; merge requests will appear wherever it changes something.")

    def dispatch(self, task: Task, text: str, clearance) -> None:
        # Message ids keep the `yt-` prefix they had before trackers were pluggable: the engine dedups by them.
        reply = self.engine.dispatch(task_id=task.task_id, message_id=f"yt-{task.task_id}", author=task.tag_author,
                                     source=self.tracker.engine_source,
                                     branch=task.branch,
                                     base=f"origin/{task.base_branch}" if task.repo else HOME_BRANCH,
                                     repo=Path(task.repo_path), text=text, clearance=clearance,
                                     extra_dirs=self.workspace.extra_dirs_file(task))
        if reply.accepted:
            self.store.update_task(task.task_id, state="dispatched", attempt_started=self.now().isoformat())
            # What was asked for. The task keeps it through every follow-up (the engine's run-env);
            # `auto` means the CLI chose, and only its own transcripts say what that was.
            self.events.emit("task_dispatched", task_id=task.task_id, tier=reply.tier,
                             attempt=reply.data.get("attempt"), dispatch_id=reply.data.get("dispatch_id"),
                             **self.cfg.bulava.model_profile())
        elif reply.rc == 6:
            self.events.emit("dispatch_busy", task_id=task.task_id, reason=reply.why())
        else:
            self.fail(task, START_REFUSED.get(reply.rc) or f"the execution engine did not accept the task ({reply.why()})")

    # -- following a task -----------------------------------------------------------------------

    def reconcile(self) -> None:
        self.retry_pending()
        for task in self.store.tasks("claimed", "dispatched", "publishing"):
            try:
                self._reconcile(task)
            except (ServiceError, EngineError, GitError) as e:
                self.events.emit("reconcile_failed", task_id=task.task_id, reason=str(e))

    def _reconcile(self, task: Task) -> None:
        show = self.engine.show(task.task_id)
        if task.state == "claimed":
            if show is None:  # crashed between taking it and the dispatch, or released by a security reviewer
                # Another task, a code review or a background run works in the shared workspace: it waits. Other
                # claimed tasks do not count here, or two of them would wait for each other.
                if self.workspace.taken(task, states=("dispatched", "publishing")):
                    return
                self._take(task, self.workspace.group(), self.tracker.issue(task.task_id))
                return
            # The engine has it: the dispatch went through before a crash. Start the clock now if the
            # runner never saw the acceptance, so a record stuck in `queued` still times out.
            task = self.store.update_task(task.task_id, state="dispatched",
                                          attempt_started=task.attempt_started or self.now().isoformat())
        if show is None:
            self.fail(task, "the execution engine lost track of this task (its state was lost)")
            return

        state = show.get("state")
        attempts = show.get("attempts") or []
        self._note_engine_state(task, show)
        if state in ("queued", "running"):
            if state == "queued" or show.get("attempt_live"):
                self._check_timeout(task, attempts)
            else:
                self._resume(task, len(attempts))
        elif state == "waiting_review":
            self.publish(task, show)
        elif state == "done" and task.research:
            self.answer(task, show, reviewed=True)
        elif state == "done" and self.store.task_mrs(task.task_id):
            # A follow-up the agent answered without changing code: say why, stay in Code Review.
            self.review.answered_without_changes(task, (show.get("outcome") or {}).get("summary") or "")
        elif state == "done":
            # succeeded_no_change / succeeded_research, reviewed by Codex: a result, nothing to push.
            summary = (show.get("outcome") or {}).get("summary") or "No code changes were needed."
            self.post_whole(task, f"done:{len(attempts)}",
                            "Arceus finished without code changes; the result passed review.", summary,
                            visuals=self.publisher.task_media(task))
            self.store.update_task(task.task_id, state="done", reason="no code changes")
            self.events.emit("task_done", task_id=task.task_id)
        elif state in ("needs_input", "blocked"):
            self.ask(task, show)
        elif state == "failed":
            why = show.get("state_reason") or "failed"
            outcome = show.get("outcome") or {}
            receipt = show.get("receipt") or {}
            if why == "stalled" and self._nudge_stalled(task, len(attempts)):
                return
            if why == "debt" and task.research \
                    and outcome.get("result") in ("succeeded_no_change", "succeeded_research"):
                # The engine reviews code; an answer that changed none it cannot verify and banks as debt.
                # That is how a research ticket normally ends, so the answer goes out, marked as not reviewed.
                self.answer(task, show, reviewed=False)
                return
            if why == "debt" and self.store.task_mrs(task.task_id) \
                    and outcome.get("result") in ("succeeded_no_change", "succeeded_research"):
                # A review round answered without a code change. Bulava cannot verify an empty diff and
                # banks it as debt; nothing is pushed either way, so it is an answer, not a failure.
                self.review.answered_without_changes(task, outcome.get("summary") or "")
                return
            if why == "debt" and self._passed_before(task, receipt, outcome):
                # The work was committed and passed review in an earlier attempt that ended without a declared
                # outcome; this one only said so. Bulava banks the empty diff as debt, but every commit in it
                # is one a passing review saw, so it is published as that review left it.
                self.events.emit("published_earlier_pass", task_id=task.task_id, attempt=len(attempts))
                self.publish(task, show | {"receipt": receipt | {"review": "passed"}})
                return
            if why == "debt" and "VERDICT: PASS" in (receipt.get("review_why") or ""):
                # Codex passed it; Bulava's own verifier did not prove it with a build/test run.
                failed, unproven = self._verifier_gaps(receipt)
                if failed or (not unproven and "[VERIFIER OVERRIDE]" in receipt["review_why"]):
                    verdict = "but the automated checks failed"
                elif unproven:
                    verdict = "but the automated checks were inconclusive"
                else:
                    verdict = "but the automated checks did not confirm it with the project's build and tests"
                self.fail(task, f"The change passed review, {verdict}, so it was not published",
                          self._debt_details(task, receipt, outcome, failed + unproven), attempt=len(attempts))
                return
            if why == "debt" and self._nothing_changed(receipt, outcome):
                # The agent concluded that nothing needs changing, and no repository moved off its base. Bulava reviews
                # no such claim and banks it as debt; it is not a failure but a conclusion for a person to accept
                # (closing the ticket) or send back (a reply).
                self.ask(task, show, no_change=True)
                return
            if why == "needs-user":
                asked = self._review_asked(receipt, attempts)
                if asked:
                    # The reviewers stopped the work for a person's decision: that is a question, put whole on the
                    # ticket, and a plain reply takes the work on, as with any other question.
                    self.ask(task, show, review_asked=asked)
                    return
                said = receipt.get("review_why") or ""
                if said.startswith(GATE_WORKER_BLOCKER):
                    # The agent filed a `blocker` finding: it waits for a person to give or do something (a log, an
                    # access, a re-run), as when it declares `blocked`. A question, and a plain reply takes it on.
                    needs = [f for f in receipt.get("findings") or []
                             if isinstance(f, dict) and f.get("kind") == "blocker"]
                    if not needs:
                        needs = [{"kind": "blocker", "text": said.removeprefix(GATE_WORKER_BLOCKER).strip()}]
                    self.ask(task, show | {"receipt": receipt | {"findings": needs}})
                    return
            reason, details = self._gate_stop(task, why, receipt, outcome, attempts)
            self.fail(task, reason, details, attempt=len(attempts))
        else:
            self.events.emit("unknown_engine_state", task_id=task.task_id, state=state)

    def _passed_without_outcome(self, task: Task, show: dict) -> bool:
        """`waiting_review` with a passed last attempt but no receipt: every repository `task show` lists is at
        its base or at the commit the engine recorded as passed (`repo_reviewed`), and one of them moved."""
        attempts = show.get("attempts") or []
        if show.get("state_reason") != "passed" or not attempts or attempts[-1].get("review") != "passed":
            return False
        repos = show.get("repos") or []
        changed = [r for r in repos if (r.get("reviewed_sha") or "") != (r.get("base_sha") or "")]
        passed = self._repo_reviewed(task)
        return bool(changed) and all(r.get("clean") and passed.get(r.get("path")) == r.get("reviewed_sha")
                                     for r in changed)

    def _repo_reviewed(self, task: Task) -> dict:
        """The engine's record of what the last passing review saw in each repository (tasks.sh, on a pass only)."""
        try:
            data = json.loads((self.cfg.paths.bulava_state / "tasks" / task_key(task.task_id) / "task.json")
                              .read_text())
        except (OSError, ValueError):
            return {}
        return data.get("repo_reviewed") or {}

    def _passed_before(self, task: Task, receipt: dict, outcome: dict) -> bool:
        """An attempt that only declared the outcome an earlier one left out: no MR yet, an empty-diff no-change
        or research claim, and every repository at the commit the last passing review saw (the engine records
        it as `repo_reviewed`, on a pass only), with at least one of them past its base."""
        if self.store.task_mrs(task.task_id) or outcome.get("result") not in ("succeeded_no_change",
                                                                              "succeeded_research"):
            return False
        if "with an empty diff" not in (receipt.get("review_why") or ""):
            return False
        passed = self._repo_reviewed(task)
        repos = receipt.get("repos") or []
        changed = [r for r in repos if (r.get("reviewed_sha") or "") != (r.get("base_sha") or "")]
        return bool(changed) and all(r.get("clean") and passed.get(r.get("path")) == r.get("reviewed_sha")
                                     for r in changed)

    def _note_engine_state(self, task: Task, show: dict) -> None:
        """An `engine_state` line when what `task show` says changes; nothing on a tick where it does not.
        A restarted runner writes the current state once."""
        attempts = show.get("attempts") or []
        seen = (show.get("state"), show.get("state_reason") or "", len(attempts), bool(show.get("attempt_live")))
        if self._engine_seen.get(task.task_id) == seen:
            return
        self._engine_seen[task.task_id] = seen
        last = attempts[-1] if attempts else {}
        self.events.emit("engine_state", task_id=task.task_id, state=seen[0], state_reason=seen[1] or None,
                         attempt=seen[2], attempt_live=seen[3], attempt_kind=last.get("kind"),
                         attempt_started=last.get("started_at"))

    def _nudge_stalled(self, task: Task, attempt: int) -> bool:
        """A stalled attempt gets one `continue` asking it to finish; a stall right after that goes to a person.

        True while the nudge is sent or waits for Bulava; False when the task should fail now.
        """
        if attempt > 1 and self.store.action(f"{task.task_id}:stalled:{attempt - 1}"):
            return False  # the attempt that stalled was itself the nudge
        key = f"{task.task_id}:stalled:{attempt}"
        if not self.store.action(key):
            self.store.action_intent(key, task.task_id, "stalled")
        if not self.workspace.followup_ready(task):
            return True  # the nudge waits for the workspace: not a refusal
        reply = self.engine.send(task_id=task.task_id, message_id=f"stalled-{task.task_id}-{attempt}", source="other",
                                 author=self.cfg.tracker.bot_user, repo=Path(task.repo_path), text=STALLED_NUDGE,
                                 clearance=trusted(STALLED_NUDGE), extra_dirs=self.workspace.extra_dirs_file(task))
        if reply.rc == 6:
            self.events.emit("send_busy", task_id=task.task_id, reason=reply.why())
            return True
        if not reply.accepted:
            self.store.action_done(key, "refused")
            return False
        with self.store.transaction():
            self.store.update_task(task.task_id, state="dispatched", attempt_started=self.now().isoformat())
            self.store.action_done(key, json.dumps({"attempt": reply.data.get("attempt")}))
        self.events.emit("stalled_nudged", task_id=task.task_id, attempt=attempt, tier=reply.tier)
        try:
            self.comment_once(task, f"stalled:{attempt}",
                              "The agent's session stopped at its prompt without finishing. The runner asked it "
                              "once to pick the work up and finish; if it stalls again, the ticket comes back to you.")
        except ServiceError as e:
            self.events.emit("notify_pending", task_id=task.task_id, reason=str(e))
        return True

    def _nothing_changed(self, receipt: dict, outcome: dict) -> bool:
        """An unreviewed `succeeded_no_change` with nothing on the branch at all: every repository the receipt lists
        clean and still at its base. Commits an earlier attempt left, or checks that failed, are not this case."""
        said = receipt.get("review_why") or ""
        if outcome.get("result") != "succeeded_no_change" or GATE_VERIFIER in said:
            return False
        if (receipt.get("codex") or {}).get("status") != "unreviewed" and not said.startswith(GATE_NO_CHANGE):
            return False
        repos = receipt.get("repos") or []
        if not repos or self._verifier_gaps(receipt, causes=False)[0]:
            return False
        return all(r.get("clean") and r.get("base_sha") and r.get("reviewed_sha") == r.get("base_sha") for r in repos)

    def _gate_note(self, receipt: dict, attempts: list[dict]) -> str:
        """The review gate's notes for this attempt (`runs/<run_id>/reports/blocked.md`), or nothing. The note is
        read only for the run of the receipt, and only when that is the last attempt's: an earlier attempt's note
        never stands for this one."""
        run_id = str(receipt.get("run_id") or "")
        last = (attempts[-1].get("run_id") if attempts else None) or run_id
        if not run_id or run_id != last or "/" in run_id or run_id.startswith("."):
            return ""
        try:
            note = self.cfg.paths.bulava_state / "runs" / run_id / "reports" / "blocked.md"
            return note.read_text(errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _spelled_option(task: Task, text: str) -> str:
        """An answer that is only an option's number, as the question on the ticket asks for, goes on with the option's
        words: the brief the agent gets has the question's text, not its options."""
        m = re.fullmatch(r"\s*(\d{1,2})\s*[.)]?\s*", text)
        try:
            options = (json.loads(task.question or "{}") or {}).get("options") or []
        except (ValueError, AttributeError):
            options = []
        if m and 1 <= int(m[1]) <= len(options):
            return f"{m[1]}. {options[int(m[1]) - 1]}"
        return text

    def _review_churned(self, task_id: str) -> bool:
        """Whether the review stopped because every fix brought a new blocker: the task's review list
        (`review-ledger-<task key>` under bulava_state, kept across attempts) counts such rounds as `churn` beside the
        rounds without progress (`stall`), and when all of those were churn the gate said so (review-gate.sh,
        stall_why). Its words are only in the run's journal, which goes with the session."""
        try:
            d = json.loads((self.cfg.paths.bulava_state / f"review-ledger-{task_key(task_id)}").read_text())
            churn, stall = int(d.get("churn") or 0), int(d.get("stall") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            return False
        return churn >= STALL_LIMIT and churn >= stall

    def _review_asked(self, receipt: dict, attempts: list[dict]) -> str:
        """What the reviewers asked a person when they stopped this attempt (`texts.review_stop`), from the review
        gate's note; empty for any other `needs-user`."""
        return self.events.redact(review_stop(self._gate_note(receipt, attempts)))

    def _gate_stop(self, task: Task, why: str, receipt: dict, outcome: dict, attempts: list[dict]) -> tuple[str, str]:
        """The reason and the details a failed attempt puts on the ticket. `needs-user` and `debt` cover several
        different stops of the review gate; each one Bulava names in its receipt gets words of its own, so the ticket
        says what happened rather than that a review wanted something. Only what the runner picked out goes on the
        ticket, never `review_why` whole: it can hold a CLI's stderr and paths on the server."""
        summary = outcome.get("summary") or ""
        said = receipt.get("review_why") or ""
        # Names and exit codes only: what a check printed stays in the run's own logs on the server.
        if why == "needs-user" and GATE_FINAL in said:
            # The heavy checks registered with `add-check --final` run once the review accepts the work; they did not
            # pass in this attempt or left nothing readable, and nothing is accepted without them. Their evidence is
            # the verifier's too (a check that could not start is a `fail` there): this, not that, is why it stopped.
            self._log_verifier_gaps(task, receipt)
            return ("the review accepted the work, but its final checks did not pass or left no readable result, "
                    "so nothing was published"), summary
        failed, unproven = self._verifier_gaps(receipt, causes=False) if why in ("debt", "needs-user") else ([], [])
        if failed or GATE_VERIFIER in said:
            self._log_verifier_gaps(task, receipt)
            # The project's own checks failed in this attempt's last round, so Bulava turned whatever Codex said into
            # a FAIL until the gate's limit (which banks it as debt with no words of its own): the failing checks are
            # the reason, not the review.
            return ("the project's automated checks kept failing, so the work was not accepted",
                    self._debt_details(task, receipt, outcome, failed + unproven))
        if why == "debt" and outcome.get("result") == "succeeded_no_change" and (
                (receipt.get("codex") or {}).get("status") == "unreviewed" or said.startswith(GATE_NO_CHANGE)):
            # Bulava reviews no claim that nothing needs changing (engine/SERVER.md, "unreviewed"). With nothing on
            # the branch it is a question (_nothing_changed); here the branch has commits no review passed.
            moved = [r for r in receipt.get("repos") or [] if r.get("reviewed_sha") != r.get("base_sha")]
            if moved:
                return ("the agent's last attempt said nothing more needed changing, but the commits it made earlier "
                        "were never reviewed, so nothing was published", self._debt_details(task, receipt, outcome))
            return ("the agent changed no code, saying none was needed, and that conclusion was not reviewed "
                    "in this attempt", summary)
        if why == "needs-user":
            if said.startswith(GATE_UNREACHABLE):
                return "the reviewer (Codex) could not be reached, so the work was not reviewed", summary
            if said.startswith(GATE_UNCOMMITTED):
                where = sorted({Path(p).name for p in said.partition(":")[2].split() if p.startswith("/")})
                left = f"Left uncommitted in: {', '.join(where)}." if where else ""
                return ("the agent left work uncommitted after being asked to commit it",
                        "\n\n".join(x for x in (left, summary) if x))
            if said.startswith(GATE_WORKER_FAILED):
                return "the agent reported that it could not finish the task", summary
            last = re.split(r"(?m)^(?=## )", self._gate_note(receipt, attempts).strip())[-1].partition("\n")[0]
            if (GATE_STALLED in last or GATE_FAIL in said) and self._review_churned(task.task_id):
                found = review_findings(said)
                listed = ["The review's findings still open:", *(f"- {self.events.redact(f)}" for f in found)]
                return ("the review stopped because every fix brought a new blocking problem, so a person has to "
                        "decide how to go on",
                        "\n\n".join(x for x in ("\n".join(listed) if found else "", summary) if x))
            if GATE_STALLED in last:
                found = review_findings(said)
                listed = ["The review's findings that kept coming back:",
                          *(f"- {self.events.redact(f)}" for f in found)]
                return ("the review kept finding the same problems, with no progress between rounds",
                        "\n\n".join(x for x in ("\n".join(listed) if found else "", summary) if x))
            if GATE_FAIL in said:
                # Rounds ran out (the gate's hard limit) with findings the fix check never saw closed.
                found = review_findings(said)
                listed = ["The review's findings still open:", *(f"- {self.events.redact(f)}" for f in found)]
                return ("the review still found problems when its rounds ran out",
                        "\n\n".join(x for x in ("\n".join(listed) if found else "", summary) if x))
            return GATE_STOPPED, summary
        return FAIL_REASONS.get(why) or FAIL_REASONS.get(outcome.get("reason") or "") or why, summary

    def _verifier_criteria(self, receipt: dict) -> list[tuple[Path, dict]]:
        """The verifier's failed and inconclusive checks for this run, as its evidence.json files have them."""
        run_id = receipt.get("run_id") or ""
        if not run_id or "/" in run_id:
            return []
        out = []
        for path in sorted((self.cfg.paths.bulava_state / "runs" / run_id / "evidence").glob("*/evidence.json")):
            try:
                criteria = json.loads(path.read_text()).get("criteria") or []
            except (OSError, ValueError, AttributeError):
                continue
            out += [(path, c) for c in criteria if isinstance(c, dict) and c.get("status") in ("fail", "inconclusive")]
        return out

    def _verifier_gaps(self, receipt: dict, causes: bool = True) -> tuple[list[str], list[str]]:
        """The verifier's failed and inconclusive checks, one line each for the ticket, never with a check's command
        (it can carry a token or an address). `causes`: the check's name as its author wrote it, and the line of its log
        (or its note) that names the cause, through the runner's redaction. Without it only a neutral label ("check 2",
        in the evidence's order) and the exit code: what the agent or the project wrote stays in the run's evidence on
        the server (`_log_verifier_gaps` says which run)."""
        failed, unproven = [], []
        for n, (path, c) in enumerate(self._verifier_criteria(receipt), 1):
            name = short_text(str(c.get("criterion") or "").strip(), 200) if causes else ""
            line = f"`{name}`" if name else f"check {n}"
            line += f" failed (exit {c.get('exit_code')})" if c.get("status") == "fail" else " was inconclusive"
            hint = (c.get("note") or log_hint(path.parent / Path(c.get("artifact") or "").name)) if causes else ""
            if hint:
                line += f": {hint}"
            (failed if c.get("status") == "fail" else unproven).append(self.events.redact(line))
        return failed, unproven

    def _log_verifier_gaps(self, task: Task, receipt: dict) -> None:
        """Which check a ticket's label stands for, in the event log: its number, status and exit code, and the run whose
        evidence has the rest. The event log goes on to Loki, so a check's name, command, note and output stay in the
        run's own evidence on the server (`runs/<run_id>/evidence/*/evidence.json`), never in the event."""
        for n, (_, c) in enumerate(self._verifier_criteria(receipt), 1):
            self.events.emit("verifier_gap", task_id=task.task_id, run_id=receipt.get("run_id"), check=n,
                             status=c.get("status"), exit_code=c.get("exit_code"))

    def _debt_details(self, task: Task, receipt: dict, outcome: dict, gaps: list[str] = ()) -> str:
        """Where the unpublished work is and what the review noted, for the person who decides next."""
        home = self.cfg.paths.workspace.resolve()
        changed = [Path(r["path"]).name for r in receipt.get("repos") or []
                   if r.get("reviewed_sha") and r.get("reviewed_sha") != r.get("base_sha")
                   and Path(r["path"]).resolve() != home]
        lines = []
        if gaps:
            lines += ["Automated checks that did not pass:", *(f"- {g}" for g in gaps[:8]), ""]
        lines += [f"The work is committed on branch `{task.branch}` in: {', '.join(changed) or 'no repository'}."]
        notes = [unattributed(f.get("text", "")).strip() for f in receipt.get("findings") or [] if f.get("text")]
        if notes:
            lines += ["", "Review notes:", *(f"- {n}" for n in notes[:5])]
        if outcome.get("summary"):
            lines += ["", f"Summary of the work: {unattributed(outcome['summary']).strip()}"]
        return "\n".join(lines)

    def _limits_exhausted(self) -> list[str]:
        """Which subscription windows Bulava last read as used up (usage.json, codex-usage.json)."""
        out = []
        for provider, name in (("claude", "usage.json"), ("codex", "codex-usage.json")):
            try:
                data = json.loads((self.cfg.paths.bulava_state / name).read_text())
            except (OSError, ValueError):
                continue
            for window in ("five_hour", "seven_day"):
                used = (data.get(window) or {}).get("used_percentage")
                if isinstance(used, (int, float)) and used >= 100:
                    out.append(f"{provider} {window}")
        return out

    def _check_timeout(self, task: Task, attempts: list[dict]) -> None:
        # A usage-limit pause is not a hang: the clock stops while the attempt waits for a usage window, as the engine
        # records it for this very attempt (paused-for-limit.json). One that goes on working while a limit is out (a
        # final check beating, background agents) is not paused. Without the progress watcher a used-up limit is
        # all there is to go by.
        out = self._limits_exhausted()
        last_attempt = attempts[-1] if attempts else {}
        did = last_attempt.get("dispatch_id") or ""
        lim, now = self.cfg.limits, self.now()
        alive = self.liveness(task.task_id, did) if self.liveness else None
        waits = alive.paused if self.liveness and alive else bool(out)
        paused, resumed = self._pause(task, did, now, waits, out, alive.pause_cleared_at if alive else None,
                                      alive.pause_recorded_at if alive else None)
        if waits:
            return
        began = parse_ts(last_attempt.get("started_at"))
        times = [t for t in (began, parse_ts(task.attempt_started)) if t]
        started = max(times) if times else None
        if not started:
            return
        # Silence is measured from the attempt's newest sign of life: a review that keeps going back and forth for
        # hours is working, not hung. The ceiling bounds the attempt however busy it is, counted from its start in the
        # engine without its pauses for a usage limit (a pause neither counts nor starts the count again).
        if (now - (began or started)).total_seconds() - paused > lim.attempt_ceiling.total_seconds():
            self.engine.stop(Path(task.repo_path))
            self.fail(task, f"the attempt ran out of time: no verdict in {_hours(lim.attempt_ceiling)}, still working"
                            f"{self._where(task, alive)}", attempt=len(attempts))
            return
        # A pause that ended is a fresh start for the silence: the attempt could not have shown life while it waited.
        last = max([started, *([datetime.fromtimestamp(alive.at, UTC)] if alive and alive.at else []),
                    *([resumed] if resumed else [])])
        if now - last > lim.attempt_timeout:
            self.engine.stop(Path(task.repo_path))
            self.fail(task, f"the agent hung: no sign of activity for {_hours(lim.attempt_timeout)}"
                            f"{self._where(task, alive)}", attempt=len(attempts))

    def _pause(self, task: Task, dispatch_id: str, now: datetime, waits: bool, limits: list[str],
               cleared_at: float | None, recorded_at: float | None = None) -> tuple[float, datetime | None]:
        """The seconds attempt `dispatch_id` has stood paused for a usage window, the pause now running included, and
        when its last pause ended. Kept with the task (attempt_pause: {dispatch, since, total, ended}), so a restart
        neither loses nor restarts them; another attempt's is dropped, and so on disk too. A pause begins when the engine
        recorded it (its marker's recorded_at, not before the last one ended), else when the runner first sees it; it
        ends when the engine cleared it (`pause-last.json` cleared_at, after it began), else when the runner sees it
        over. So a runner that was down counts the pause the engine kept, no more and no less."""
        try:
            p = json.loads(task.attempt_pause or "{}")
        except ValueError:
            p = {}
        changed = not isinstance(p, dict) or p.get("dispatch") != dispatch_id
        if changed:
            p = {"dispatch": dispatch_id, "since": None, "total": 0.0}
        since = parse_ts(p.get("since"))
        if waits and not since:
            floor = parse_ts(p.get("ended"))
            rec = datetime.fromtimestamp(recorded_at, UTC) if recorded_at else None
            since = rec if rec and rec <= now and not (floor and rec < floor) else now
            p["since"], changed = since.isoformat(), True
            self.events.emit("attempt_paused_for_limit", task_id=task.task_id, limits=limits)
        elif not waits and since:
            cleared = datetime.fromtimestamp(cleared_at, UTC) if cleared_at else None
            end = cleared if cleared and since < cleared <= now else now
            p["total"] = float(p.get("total") or 0) + (end - since).total_seconds()
            p["since"], p["ended"], since, changed = None, end.isoformat(), None, True
        if changed:
            self.store.update_task(task.task_id, attempt_pause=json.dumps(p))
        return (float(p.get("total") or 0) + ((now - since).total_seconds() if since else 0.0),
                parse_ts(p.get("ended")))

    def _where(self, task: Task, alive: Liveness | None) -> str:
        """Where a stopped attempt was, for its reason: the step, the review round, the review's open items."""
        parts = [*([f"last step: {alive.step}"] if alive and alive.step else []),
                 *([f"review round {alive.round}"] if alive and alive.round else [])]
        open_ids = self._open_review_ids(task.task_id)
        if open_ids:
            parts.append("open review items: " + ", ".join(open_ids))
        return f" ({'; '.join(parts)})" if parts else ""

    def _open_review_ids(self, task_id: str) -> list[str]:
        """The ids still open in the task's review list (`review-ledger-<task key>`, kept across attempts)."""
        try:
            d = json.loads((self.cfg.paths.bulava_state / f"review-ledger-{task_key(task_id)}").read_text())
            return [str(i["id"]) for i in d.get("items") or []
                    if isinstance(i, dict) and i.get("open") and not i.get("same_as") and i.get("id")]
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return []

    def _resume(self, task: Task, attempt: int) -> None:
        if not self.workspace.followup_ready(task):
            return  # sent on a later tick, once the workspace is free
        reply = self.engine.send(task_id=task.task_id, message_id=f"resume-{task.task_id}-{attempt}", source="other",
                                 author=self.cfg.tracker.bot_user, repo=Path(task.repo_path),
                                 extra_dirs=self.workspace.extra_dirs_file(task), text=RESUME, clearance=trusted(RESUME))
        self._after_send(task, reply, "attempt_resumed")

    def _after_send(self, task: Task, reply: Reply, event: str, untag: str | None = None, **fields) -> bool:
        if reply.accepted:
            with self.store.transaction():
                self.store.update_task(task.task_id, state="dispatched", question_id=None, question_at_ms=None,
                                       attempt_started=self.now().isoformat())
                if untag:
                    self.store.action_intent(f"{task.task_id}:untag:{untag}:{reply.data.get('attempt')}",
                                             task.task_id, "untag", json.dumps({"tag": untag}))
            self.events.emit(event, task_id=task.task_id, tier=reply.tier, attempt=reply.data.get("attempt"), **fields)
            return True
        if reply.rc == 6:
            self.events.emit("send_busy", task_id=task.task_id, reason=reply.why())
            return False
        self.fail(task, f"the execution engine did not accept the follow-up ({reply.why()})")
        return False

    # -- outcomes -------------------------------------------------------------------------------

    def fail(self, task: Task, reason: str, details: str = "", attempt: int | None = None,
             also: Callable[[], None] | None = None) -> None:
        """`also` runs inside the same transaction: what has to land with the failure or not at all."""
        # A failure outside an attempt is numbered among the task's failure comments, never named by its
        # words: a new wording in a later version must not make the same failure a new comment. A failure
        # recorded before is never named again (its intent keeps its own kind), and one that never reached
        # the transaction below gets the same number when it is seen again.
        kind = f"failed:{attempt}" if attempt is not None else \
            f"failed:n{self.store.count_actions(f'{task.task_id}:comment:failed:') + 1}"
        if task.state in ("dispatched", "needs_input") and task.branch:
            self._save_progress(task)
        head = (f"Arceus could not answer this ticket: {reason}." if task.research else
                f"Arceus could not take this ticket to a merge request: {reason}.")
        foot = ""
        if task.branch and task.research:
            foot = (f"To have Arceus try again, reply with a comment that starts with `{CONTINUE}`, followed "
                    "by anything it should know.")
        elif task.branch:
            foot = (f"To have Arceus pick this up again, reply with a comment that starts with "
                    f"`{CONTINUE}`, followed by anything it should know (what was fixed, what to do "
                    f"differently). Its branches and commits are kept.")
        # The details whole, in parts when long: every part is journaled with the failure, below.
        pieces = [(piece_kind(kind, i), p) for i, p in enumerate(ticket_pieces(head, details, foot), 1)]
        # Our own state first: it must not depend on the tracker being up. The comment and the tag go
        # through the journal, and retry_pending() finishes them if the tracker fails right now.
        # The state and both intents in one transaction: a crash or a tracker failure after it
        # leaves the comment and the tag to retry_pending(), never lost.
        tag = self.cfg.tracker.tags.failed
        tag_key = f"{task.task_id}:tag:{tag}:{kind}"
        with self.store.transaction():
            task = self.store.update_task(task.task_id, state="failed", reason=reason,
                                          failed_at_ms=int(self.now().timestamp() * 1000))
            self.store.action_intent(tag_key, task.task_id, "tag", json.dumps({"tag": tag}))
            for k, text in pieces:
                self.store.action_intent(f"{task.task_id}:comment:{k}", task.task_id, "comment",
                                         json.dumps({"kind": k, "text": text}))
            if also:
                also()
        self.events.emit("task_failed", task_id=task.task_id, reason=reason)
        try:
            for k, text in pieces:
                self.comment_once(task, k, text)
            self.tag_once(task, tag, tag_key)
        except ServiceError as e:
            self.events.emit("notify_pending", task_id=task.task_id, reason=str(e))

    def stop_closed(self, task: Task, closed: list[tuple[str, str]]) -> None:
        """People closed every MR of the task without merging any: their decision, not a failure. The task stops,
        with no failure tag; a `/continue` still picks it up, as after a failure. `closed`: (url, who closed it)."""
        # Numbered like a failure: the task may be continued and closed again, and that is another comment.
        kind = f"closed:n{self.store.count_actions(f'{task.task_id}:comment:closed:') + 1}"
        who = sorted({by for _, by in closed if by})
        by = f" by {', '.join(who)}" if who else ""
        body = (f"Every merge request of this task was closed without merge{by}, "
                "so Arceus has stopped working on it:\n" + "\n".join(f"- {url}" for url, _ in closed) +
                f"\n\nTo have Arceus pick this up again, reply with a comment that starts with `{CONTINUE}`, followed "
                "by what it should do differently. Its branches and commits are kept.")
        with self.store.transaction():
            task = self.store.update_task(task.task_id, state="stopped", reason="every merge request was closed "
                                          "without merge", failed_at_ms=int(self.now().timestamp() * 1000))
            self.store.action_intent(f"{task.task_id}:comment:{kind}", task.task_id, "comment",
                                     json.dumps({"kind": kind, "text": body}))
        self.events.emit("task_stopped", task_id=task.task_id, reason="merge requests closed",
                         mrs=[url for url, _ in closed])
        try:
            self.comment_once(task, kind, body)
        except ServiceError as e:
            self.events.emit("notify_pending", task_id=task.task_id, reason=str(e))

    def _save_progress(self, task: Task) -> None:
        """The failed attempt's uncommitted work, committed on the task branch where it stands.

        Nothing is lost for a later `/continue`, and the shared workspace is clean for the next task
        (Bulava refuses to start on a dirty tree). It is reviewed with the rest before anything is
        published; the runner never pushes a commit Codex did not accept.
        """
        for name in self.workspace.names(task):
            path = self.cfg.paths.workspace / name
            try:
                self._end_unfinished(task, name, path)
                if not (path / ".git").exists() or self.git.current_branch(path) != task.branch \
                        or self.git.is_clean(path):
                    continue
                sha = self.git.commit_all(path, f"WIP [{task.task_id}]: uncommitted work of a failed attempt, "
                                                "kept by the Arceus runner")
                self.events.emit("progress_saved", task_id=task.task_id, repo=name, sha=sha)
            except GitError as e:
                self.events.emit("progress_not_saved", task_id=task.task_id, repo=name, reason=str(e))

    def _end_unfinished(self, task: Task, name: str, path: Path) -> str | None:
        """A rebase, cherry-pick, revert or merge the attempt left half done is undone: the branch is back as it
        was before it, with every commit it had.

        Committing in the middle of one would commit conflict markers (on a detached HEAD, in a rebase), and a
        checkout would leave it hanging over the next task in this repository."""
        if not (path / ".git").exists():
            return None
        operation = self.git.unfinished(path)
        if operation:
            self.git.abort(path, operation)
            self.events.emit("git_operation_aborted", task_id=task.task_id, repo=name, operation=operation)
        return operation

    def answer(self, task: Task, show: dict, reviewed: bool) -> None:
        """A research ticket's result: the agent's answer on the ticket, whole, and nothing published.

        The answer is copied out of the workspace before anything is posted, so a retry after a crash or a tracker
        failure posts the same text even once the workspace is cleaned. Each piece goes through comment_once:
        pieces posted before a failure are not posted again. The task is done only once every piece is on the
        ticket. Pieces and the copy are named by the research run (research_run), not by the engine's attempt:
        the engine starts counting again when it lost a task and `/continue` started it anew."""
        run = self.research_run(task)
        text, why = self._kept_answer(task, run)
        if not text:
            self._no_answer(task, show, run, why)
            return
        head = ("Arceus looked into this ticket and changed no code; the answer passed review." if reviewed else
                "Arceus looked into this ticket and changed no code.")
        foot = [f"For a follow-up question, reply with a comment that starts with `{CONTINUE}`, followed by the "
                "question."]
        unpublished = self._research_commits(task, show)
        if unpublished:
            foot.insert(0, f"The agent also committed changes on branch `{task.branch}` in "
                           f"{', '.join(unpublished)}. A research ticket publishes nothing, so they stay on the server.")
        pieces = split_answer(text)
        posted = []
        for i, piece in enumerate(pieces, 1):
            parts = [head] if i == 1 else []
            if len(pieces) > 1:
                parts.append(f"**Answer, part {i} of {len(pieces)}**")
            parts.append(piece)
            if i == len(pieces):
                parts += foot
            posted.append(self.comment_once(task, f"answer:{run}:{i}", "\n\n".join(parts)))
        self._leave_clean(task)
        answered = self.cfg.tracker.states.answered_state
        with self.store.transaction():
            self.store.update_task(task.task_id, state="done", reason="answered", question_id=None,
                                   question_at_ms=None, published_at_ms=posted[0][1] if posted[0] else None)
            if answered:
                self.store.action_intent(f"{task.task_id}:state:answered:{run}", task.task_id, "state",
                                         json.dumps({"state": answered, "while": ["done"]}))
            self.journal_tags_on(task, "answered", str(run))
        self.events.emit("task_done", task_id=task.task_id, kind=task.kind, reviewed=reviewed, run=run,
                         comments=len(pieces), answer_chars=len(text), unpublished_commits=unpublished or None)
        self.retry_pending()

    # -- research runs: the start and one per `/continue`, counted in the journal ------------------------

    def research_run(self, task: Task) -> int:
        """The number of the research ticket's current run: 1 for the start, one more for every `/continue`."""
        return self.store.count_actions(f"{task.task_id}:research-run:") + 1

    def _new_research_run(self, task: Task, cid: str) -> None:
        """A `/continue` starts the next run: counted once, by the comment that asked for it."""
        key = f"{task.task_id}:research-run:{cid}"
        if not task.research or self.store.action(key):
            return
        self.store.action_intent(key, task.task_id, "research-run")
        self.store.action_done(key)

    def _no_answer(self, task: Task, show: dict, run: int, why: str) -> None:
        """The run ended without an answer the runner can post. The agent is asked once, saying why, to write it
        where it belongs; a run that was asked already fails, with what the agent said instead."""
        attempt = len(show.get("attempts") or [])
        key = f"{task.task_id}:answer-asked:{run}"
        act = self.store.action(key)
        if act and act[0] == "done":
            summary = unattributed((show.get("outcome") or {}).get("summary") or "").strip()
            self.fail(task, f"the agent did not leave an answer the runner can post ({why})",
                      f"What the agent said instead: {summary}" if summary else "", attempt=attempt)
            return
        if not act:
            self.store.action_intent(key, task.task_id, "answer-asked")
        if not self.workspace.followup_ready(task):
            return  # asked once the workspace is free
        text = (f"The runner cannot post your answer: {why}. Write the whole answer in Markdown to "
                f"`{self.answer_file(task, run)}`, a plain file of at most {ANSWER_BYTES // 1024} KB, commit nothing, and "
                "declare the outcome succeeded_research again.")
        reply = self.engine.send(task_id=task.task_id, message_id=f"answer-{task.task_id}-{run}", source="other",
                                 author=self.cfg.tracker.bot_user, repo=Path(task.repo_path), text=text,
                                 clearance=trusted(text), extra_dirs=self.workspace.extra_dirs_file(task))
        if reply.rc == 6:
            self.events.emit("send_busy", task_id=task.task_id, reason=reply.why())
            return
        if not reply.accepted:
            self.store.action_done(key, "refused")
            self.fail(task, f"the agent did not leave an answer the runner can post ({why}), and the execution "
                            f"engine did not take the request to write it ({reply.why()})", attempt=attempt)
            return
        with self.store.transaction():
            self.store.update_task(task.task_id, state="dispatched", attempt_started=self.now().isoformat())
            self.store.action_done(key, json.dumps({"attempt": reply.data.get("attempt")}))
        self.events.emit("answer_requested", task_id=task.task_id, run=run, reason=why, tier=reply.tier)

    def _answer_copy(self, task: Task, run: int) -> Path:
        return self.cfg.paths.db.parent / "tasks" / task.task_id / f"answer-{run}.md"

    def _kept_answer(self, task: Task, run: int) -> tuple[str, str]:
        """The run's answer as the runner keeps it, read from the agent's file once and kept with the task's other
        files; the names of the tools behind it left out. ("", why) when there is none to post."""
        copy = self._answer_copy(task, run)
        if copy.exists():
            return copy.read_text(encoding="utf-8"), ""
        text, why = self._read_answer(task, run)
        # Nothing in it may pass for the runner's own comment markers (comment_once finds its comments by them).
        text = re.sub(r"\[arceus:", "[arceus :", unattributed(text)).strip()
        if not text:
            return "", why or "the answer file is empty"
        copy.parent.mkdir(parents=True, exist_ok=True)
        tmp = copy.with_name(copy.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(copy)
        return text, ""

    def _read_answer(self, task: Task, run: int) -> tuple[str, str]:
        """The agent's answer file and, when it cannot be posted, why. Read as untrusted: the agent writes the
        folder, and the runner reads with its own rights and posts what it reads. A symlink (it could point at the
        runner's secrets), a file that is not where it should be, or one past the size limit is not read."""
        f = self.answer_file(task, run)
        try:
            if f.is_symlink() or f.parent.is_symlink():
                why = "the answer file is a symbolic link"
            elif not f.is_file():
                return "", f"there is no answer file at {f}"
            elif f.resolve().parent != f.parent.resolve() or f.parent.parent.resolve() != f.parent.parent:
                why = "the answer file is outside the notes folder"
            elif f.stat().st_nlink > 1:
                why = "the answer file is a hard link to another file"
            elif f.stat().st_size > ANSWER_BYTES:
                why = f"the answer file is larger than {ANSWER_BYTES // 1024} KB"
            else:
                return f.read_text(encoding="utf-8", errors="replace"), ""
            self.events.emit("answer_refused", task_id=task.task_id, reason=why)
            return "", why
        except OSError as e:
            self.events.emit("answer_unreadable", task_id=task.task_id, reason=str(e))
            return "", f"the answer file cannot be read ({e.strerror or e})"

    def _research_commits(self, task: Task, show: dict) -> list[str]:
        """The repositories where a research attempt committed anyway: past their base, the home left out."""
        home = self.cfg.paths.workspace.resolve()
        repos = (show.get("receipt") or {}).get("repos") or show.get("repos") or []
        return sorted(Path(r["path"]).name for r in repos
                      if r.get("path") and Path(r["path"]).resolve() != home
                      and (r.get("reviewed_sha") or "") != (r.get("base_sha") or ""))

    def _leave_clean(self, task: Task) -> None:
        """What a research attempt left uncommitted goes to the stash, so the next task does not find the shared
        workspace dirty (a dirty repository refuses its start). Nothing is deleted."""
        for name in self.workspace.names(task):
            path = self.cfg.paths.workspace / name
            try:
                if not (path / ".git").exists() or self.git.is_clean(path):
                    continue
                self._end_unfinished(task, name, path)
                stash = self.git.stash_all(path, f"arceus: what the research of {task.task_id} left")
                self.events.emit("research_leftovers_stashed", task_id=task.task_id, repo=name, stash=stash)
            except GitError as e:
                self.events.emit("research_leftovers_kept", task_id=task.task_id, repo=name, reason=str(e))

    def ask(self, task: Task, show: dict, review_asked: str = "", no_change: bool = False) -> None:
        """The question on the ticket, tagged needs-input: the agent's own, or what the reviewers asked when they
        stopped the work (`review_asked`). A stop with no question in words says that nothing is published and
        what the agent named as needed, so the ticket never reads as a report with nothing asked. `no_change`: the
        agent's conclusion that nothing needs changing, for a person to accept by closing the ticket."""
        q = show.get("question") or {}
        outcome = show.get("outcome") or {}
        attempt = len(show.get("attempts") or [])
        report = outcome.get("summary") or ""
        mrs = [m.url for m in self.store.task_mrs(task.task_id) if m.url]  # a later round: these stay as they were
        if no_change:
            q = {}
            text = no_change_question(report)
        elif review_asked:
            q = {}
            text = review_question(review_asked, report, mrs)
        elif q.get("text") and show.get("state_reason") == "spec_conflict":
            # The review gate's question (spec_conflict): the reviewer's own line, quoting the task, through the same
            # redaction as any other review text that reaches the ticket.
            text = self.events.redact(q["text"])
        elif q.get("text"):
            text = q["text"]
        else:
            needs = [str(f.get("text") or "") for f in (show.get("receipt") or {}).get("findings") or []
                     if isinstance(f, dict) and f.get("kind") == "blocker" and f.get("text")]
            text = stop_question(needs, report, mrs)
        options = list(q.get("options") or [])
        # The question goes whole, with the screenshots it is about: what is cut off is what the person never sees.
        foot = "\n".join([*(f"{i}. {opt}" for i, opt in enumerate(options, 1)), "",
                          "Reply with a comment on this ticket: the option's number or your own words." if options
                          else "Reply with a comment on this ticket: your decision on each point. The agent "
                               "continues with it, and the work is reviewed again before anything is published."
                          if review_asked else "Reply with a comment on this ticket."]).strip()
        if no_change:
            foot = no_change_foot()
        ask_head = "the agent found nothing to change and needs your decision:" if no_change else \
            "the agent needs an answer to continue:"
        # Bulava names a review's spec_conflict question by its words, so the same dispute in a later attempt has the
        # same id: its comment, and the time answers are read from, are that attempt's own. The id stays Bulava's.
        kind = f"question:{q.get('id')}:{attempt}" if show.get("state_reason") == "spec_conflict" and q.get("id") \
            else f"question:{q.get('id') or attempt}"
        posted = self.post_whole(task, kind,
                                 f"{self.tracker.mention(task.tag_author)}, {ask_head}",
                                 text, foot, visuals=self.publisher.task_media(task))[0]
        self.tracker.add_tag(task.issue_db_id, self.cfg.tracker.tags.needs_input)
        # The words go with the state, whole as the ticket got them: the console's task page shows them, and the
        # project's channel (notify.py) quotes them, cut to its own length, whenever it tells the question.
        words = {"id": q.get("id"), "text": short_text(text, QUESTION_KEEP), "options": list(q.get("options") or []),
                 "blocked": show.get("state") == "blocked", "review": bool(review_asked),
                 **({"no_change": True} if no_change else {})}
        self.store.update_task(task.task_id, state="needs_input", question_id=q.get("id"),
                               question_at_ms=posted[1] if posted else None, question=json.dumps(words))
        self.events.emit("task_needs_input", task_id=task.task_id, question_id=q.get("id"),
                         blocked=words["blocked"], **({"review": True} if review_asked else {}),
                         **({"no_change": True} if no_change else {}))

    def open_threads(self, task: Task, ref: str) -> tuple[str, list[str]]:
        """The open MR discussions not addressed to the agent, for the comment on the ticket `ref`
        (ReviewLoop.open_threads). Read once and kept: a retry under the same message id, or the same text waiting
        for a security reviewer, must not change because someone wrote on the MR meanwhile."""
        review = getattr(self, "review", None)
        if not review:
            return "", []
        key = f"{task.task_id}:mr-context:{ref}"
        kept = self.store.action(key)
        if kept:
            data = json.loads(kept[1] or "{}")
            return data.get("text", ""), data.get("writers", [])
        text, writers = review.open_threads(task)
        payload = json.dumps({"text": text, "writers": writers})
        with self.store.transaction():
            self.store.action_intent(key, task.task_id, "mr_context", payload)
            self.store.action_done(key, payload)
        return text, writers

    def check_answers(self) -> None:
        """A comment from an allowed person after the question is the answer."""
        c = self.cfg.tracker
        for task in self.store.tasks("needs_input"):
            try:
                if task.no_change_asked and self._closed_on_no_change(task):
                    continue
                after = task.question_at_ms or 0
                # >=: a reply in the same millisecond as the question counts. The engine dedups by
                # message id, so a comment seen twice is sent once.
                # A request for a code review is the reviewer's (code_review.py), never an answer to the agent.
                candidates = [x for x in self.tracker.comments(task.issue_db_id)
                              if x.created >= after and x.author != c.bot_user and x.author in c.allowed_users
                              and not is_decision(x.text)
                              and not (self.reviewer and self.reviewer.claims(task.task_id, x))]
                # A comment with a file and no text (a screenshot) is an answer too.
                with_files = self.commented_files(task) if any(not x.text.strip() for x in candidates) else set()
                answer = next((x for x in candidates if x.text.strip() or str(x.id) in with_files), None)
                if not answer:
                    self._unanswered(task)
                    continue
                if self.workspace.waits(task):
                    continue  # the answer is read again on a later tick; the task stays in needs_input
                files = self.ticket_files(task, {str(answer.id)}, [answer])
                text = self._spelled_option(task, answer.text) if answer.text.strip() \
                    else f"{answer.author} answered with files only."
                tags, new_tags = self._changed_tags(task)
                if tags:
                    text += f"\n\n{tags}"
                threads, writers = self.open_threads(task, f"answer:{answer.id}")
                if threads:
                    text += f"\n{threads}"
                if files:
                    text, files = materials.with_section(
                        text, files, self._files_folder(task),
                        [(f"the answer by {answer.author}", str(answer.id), answer.text)])
                clearance = self.gate.clear(task, "answer", text, authors=[answer.author, *writers,
                                                                           *(m.attachment.author for m in files)],
                                            resume_state="needs_input", files=materials.screen_files(files))
                if clearance is None or not self.workspace.followup_ready(task):
                    continue
                self.publish_files(task, files)
                # The engine dedups by message id: the comment's id escaped (unchanged for YouTrack's), and the
                # plain retry after a `:`, which an escaped id never contains.
                comment = key_part(answer.id, keep_dash=True)
                reply = self.engine.send(task_id=task.task_id, message_id=f"yt-comment-{comment}",
                                         source=self.tracker.engine_source, author=answer.author,
                                         repo=Path(task.repo_path),
                                         text=text, clearance=clearance, answers=task.question_id,
                                         extra_dirs=self.workspace.extra_dirs_file(task))
                if reply.rc == 4 and task.question_id:
                    # The question is no longer open on the engine's side: send it as plain follow-up.
                    reply = self.engine.send(task_id=task.task_id, message_id=f"yt-comment-{comment}:plain",
                                             source=self.tracker.engine_source, author=answer.author,
                                             repo=Path(task.repo_path),
                                             text=text, clearance=clearance,
                                             extra_dirs=self.workspace.extra_dirs_file(task))
                if self._after_send(task, reply, "answer_sent", untag=c.tags.needs_input, author=answer.author):
                    if new_tags is not None:
                        self._remember_tags(task, new_tags)
                    if hasattr(self.store, "consume_signal"):
                        self.store.consume_signal(f"yt-{answer.id}", task.task_id, "ticket", answer.author,
                                                  answer.text, self.now().isoformat(), "answer")
                    self.retry_pending()  # takes the needs-input tag off now, or on a later tick
            except (ServiceError, EngineError, GitError) as e:
                self.events.emit("reconcile_failed", task_id=task.task_id, reason=str(e))

    # -- picking a failed task up again ---------------------------------------------------------

    def check_continue(self) -> None:
        """A `/continue` comment from an allowed person after a failure, after people closed every MR of the task,
        or after the answer to a research ticket (a follow-up question): recorded, sent by send_continue()."""
        c = self.cfg.tracker
        window = self.cfg.limits.continue_window
        for task in [*self.store.tasks("failed", "stopped"), *(t for t in self.store.tasks("done") if t.research)]:
            # A task stopped because its ticket was moved on by hand has no `failed_at_ms`: the review stopped there.
            since = task.published_at_ms if task.state == "done" else task.failed_at_ms
            if not since or not task.branch:
                continue  # failed before any work existed (no repositories), or before this was possible
            if self.now() - datetime.fromtimestamp(since / 1000, UTC) > window:
                continue
            try:
                comments = self.tracker.comments(task.issue_db_id)
            except ServiceError as e:
                self.events.emit("reconcile_failed", task_id=task.task_id, reason=str(e))
                return
            for x in comments:
                if x.created < since or x.author == c.bot_user or x.author not in c.allowed_users:
                    continue
                if not x.text.strip().lower().startswith(CONTINUE):
                    continue
                key = f"{task.task_id}:continue:{key_part(x.id, keep_dash=True)}"
                if self.store.action(key):
                    continue  # this one was handled (or is waiting to be)
                with self.store.transaction():
                    self.store.action_intent(key, task.task_id, "continue", json.dumps(
                        {"comment": x.id, "author": x.author, "created": x.created,
                         "text": x.text.strip()[len(CONTINUE):].strip()}))
                    self.taken(task, x.id)
                self.events.emit("continue_requested", task_id=task.task_id, author=x.author, comment=x.id)
                self.retry_pending()
                break  # one request per failure: the next failure sets a new boundary

    def send_continue(self) -> None:
        """Hand a recorded `/continue` to Bulava as the next attempt of the same task."""
        for key, task_id, payload in self.store.pending_continues():
            task = self.store.task(task_id)
            data = json.loads(payload or "{}")
            if task and task.state == "security_hold":
                continue  # the request itself waits for a security reviewer
            if not task or not self._continuable(task):
                self.store.action_done(key, "stale")  # it moved on by another way meanwhile
                continue
            if self.workspace.waits(task):
                return  # the workspace is shared: wait until the running task is over
            try:
                self._send_continue(task, key, data)
            except (ServiceError, EngineError, GitError) as e:
                self.events.emit("reconcile_failed", task_id=task.task_id, reason=str(e))
            return  # one at a time

    @staticmethod
    def _continuable(task: Task) -> bool:
        # A task stops with `failed_at_ms` only when people closed every MR (stop_closed).
        return task.state == "failed" or (task.state == "stopped" and bool(task.failed_at_ms)) \
            or (task.research and task.state == "done")

    def _send_continue(self, task: Task, key: str, data: dict) -> None:
        if self.workspace.waits(task):
            return  # before either way below: both switch the workspace
        if self.engine.show(task.task_id) is None:
            # Bulava never had it (the start was refused) or lost it: start it again from the ticket,
            # on the same branches, which keep whatever was committed on them.
            self._resumed(task, key, data, state="claimed", attempt="new")
            self.reconcile()
            return
        if not self.workspace.followup_ready(task):
            return  # the request stays pending
        # A research ticket's `/continue` is its next run, with an answer file of its own (_new_research_run).
        answer = self.answer_file(task, self.research_run(task) + 1) if task.research else None
        if task.state == "done":
            text = (f"{data['author']} has a follow-up to your answer on the ticket. Answer it the same way: the "
                    f"full answer in `{answer}`, then the outcome succeeded_research. Commit nothing.")
        elif task.state == "stopped":
            text = (f"{data['author']} asked you to continue this task. It had stopped because people closed every "
                    "merge request of it without merging any: read on the MRs and the ticket why, then pick it up "
                    "from where the work stands and finish it.")
        else:
            text = (f"{data['author']} asked you to continue this task after it failed ({task.reason}). "
                    f"Pick it up from where the work stands and finish it.")
            if answer:
                text += (f" This run's answer goes to `{answer}`, whatever an earlier run wrote elsewhere; then "
                         "declare the outcome succeeded_research. Commit nothing.")
        asked = Comment(str(data["comment"]), "", data["author"], data.get("created") or 0)
        files = self.ticket_files(task, {asked.id}, [asked])
        if data.get("text"):
            text += f"\n\n{data['text']}"
        tags, new_tags = self._changed_tags(task)
        if tags:
            text += f"\n\n{tags}"
        threads, writers = self.open_threads(task, key)
        if threads:
            text += f"\n{threads}"
        if files:
            text, files = materials.with_section(
                text, files, self._files_folder(task),
                [(f"the request by {data['author']}", asked.id, data.get("text") or "")])
        if data.get("text") or files or tags or threads:
            clearance = self.gate.clear(task, "continue", text, authors=[data["author"], *writers,
                                                                         *(m.attachment.author for m in files)],
                                        resume_state=task.state, ref=key, files=materials.screen_files(files))
            if clearance is None:
                return
        else:
            clearance = trusted(text)  # a bare `/continue`: only the runner's words
        self.publish_files(task, files)
        reply = self.engine.send(task_id=task.task_id, message_id=f"yt-continue-{data['comment']}",
                                 source=self.tracker.engine_source, author=data["author"], repo=Path(task.repo_path),
                                 text=text, clearance=clearance, extra_dirs=self.workspace.extra_dirs_file(task))
        if reply.accepted:  # dispatched, or a duplicate of what Bulava took before a crash
            if new_tags is not None:
                self._remember_tags(task, new_tags)
            self._resumed(task, key, data, state="dispatched", attempt=reply.data.get("attempt"))
        elif reply.rc == 6:
            self.events.emit("send_busy", task_id=task.task_id, reason=reply.why())
        elif reply.rc == 2:
            self._resumed(task, key, data, state="claimed", attempt="new")
            self.reconcile()
        else:
            self.store.action_done(key, "refused")
            self.comment_once(task, f"continue-refused:{key_part(data['comment'], keep_dash=True)}",
                              f"{self.tracker.mention(data['author'])}, the execution engine did not accept the "
                              f"request to continue: {reply.why()}.")

    def _resumed(self, task: Task, key: str, data: dict, state: str, attempt) -> None:
        """The task runs again: a person's request overrides the review and CI budgets it ran out of."""
        c = self.cfg.tracker
        tag = c.tags.failed
        with self.store.transaction():
            # An older pending "add agent-failed" must not put the tag back after it is taken off.
            for k, tid, kind, p in self.store.pending_actions():
                if tid == task.task_id and kind == "tag" and json.loads(p or "{}").get("tag") == tag:
                    self.store.action_done(k, "superseded")
            # What the ticket said up to the `/continue` is in the request or behind it: only later comments
            # go to the agent as review once the work is published again.
            self.store.update_task(task.task_id, state=state, reason=None, failed_at_ms=None,
                                   attempt_started=self.now().isoformat(), review_rounds=0, ci_attempts=0,
                                   review_answers=0,
                                   published_at_ms=data.get("created") or int(self.now().timestamp() * 1000))
            if hasattr(self.store, "signals"):
                self.store.drop_signals([sg.id for sg in self.store.signals(task.task_id, unbatched=True)
                                         if sg.kind in ("ticket", "code_review")], "continue")
            self.store.action_done(key, json.dumps({"attempt": attempt}))
            cid = key_part(data["comment"], keep_dash=True)
            self._new_research_run(task, cid)
            self.store.action_intent(f"{task.task_id}:untag:{tag}:{cid}", task.task_id, "untag",
                                     json.dumps({"tag": tag}))
            self.store.action_intent(f"{task.task_id}:state:continue:{cid}", task.task_id, "state", json.dumps(
                {"state": c.states.in_progress, "while": ["claimed", "dispatched"]}))
            self.store.action_intent(f"{task.task_id}:comment:continue:{cid}", task.task_id, "comment",
                                     json.dumps({"kind": f"continue:{cid}", "text": (
                                         f"The agent is picking this up again, as {data['author']} asked."
                                         if state == "dispatched" else
                                         f"The agent is starting this again from the ticket, as {data['author']} "
                                         "asked; its branches and commits are kept.")}))
        self.events.emit("task_continued", task_id=task.task_id, author=data["author"], attempt=attempt,
                         fresh=state == "claimed")
        self.retry_pending()

    def _continues_waiting(self) -> bool:
        """A `/continue` ready to go; one held for a security reviewer does not keep new tickets waiting."""
        return any((t := self.store.task(task_id)) and t.state != "security_hold"
                   for _, task_id, _ in self.store.pending_continues())

    def _closed_on_no_change(self, task: Task) -> bool:
        """A person closed the ticket while it held the agent's no-change conclusion: they accepted it. Closed is what
        the tracker itself says (YouTrack's `resolved`), never a guess from the state's name: a ticket moved to another
        open state, or with no state, still waits and a reply still takes the work on. The task is done, without a
        failure, and the needs-input tag comes off (journaled with the state, so a tracker outage leaves it to
        retry_pending()). A comment on a ticket that is closed is not sent to the agent: closing is the decision."""
        c = self.cfg.tracker
        issue = self.tracker.issue(task.task_id)
        if not issue.closed:
            return False
        where = f" (\"{issue.state}\")" if issue.state else ""
        with self.store.transaction():
            self.store.update_task(task.task_id, state="done", question_id=None, question_at_ms=None,
                                   reason=f"no code changes: a person closed the ticket{where}")
            self.store.action_intent(f"{task.task_id}:untag:{c.tags.needs_input}:closed:{task.question_at_ms}",
                                     task.task_id, "untag", json.dumps({"tag": c.tags.needs_input}))
        self.events.emit("task_done", task_id=task.task_id, reason="no change accepted", state=issue.state)
        self.retry_pending()
        return True

    def _unanswered(self, task: Task) -> None:
        """No answer yet: a reminder after needs_input_reminder, Failed after needs_input_fail_after. The agent's
        no-change conclusion never fails: it waits for the ticket to be closed or for a reply, with one reminder."""
        if not task.question_at_ms:
            return
        asked = datetime.fromtimestamp(task.question_at_ms / 1000, UTC)
        waited = self.now() - asked
        lim = self.cfg.limits
        if task.no_change_asked:
            if waited > lim.needs_input_reminder:
                self.comment_once(task, f"reminder:{task.question_at_ms}",
                                  f"{self.tracker.mention(task.tag_author)}, the agent's conclusion above still waits "
                                  "for your decision: close the ticket if you agree, or reply with what still needs "
                                  "doing.")
            return
        if waited > lim.needs_input_fail_after:
            days = lim.needs_input_fail_after.total_seconds() / 86400
            self.fail(task, f"no answer to the agent's question in {days:g} days")
            return
        if waited > lim.needs_input_reminder:
            self.comment_once(task, f"reminder:{task.question_id or task.question_at_ms}",
                              f"{self.tracker.mention(task.tag_author)}, the agent is still waiting for an answer to "
                              "the question above. "
                              f"Without one the task will be closed as failed in "
                              f"{(lim.needs_input_fail_after - waited).days + 1} days.")

    def notice_rejected(self, issue_db_id: str, key: str, author: str, activity_id: str) -> None:
        """Tag set by someone not on the allowlist: said once on the ticket."""
        action = f"{key}:rejected:{key_part(activity_id, keep_dash=True)}"
        if (self.store.action(action) or ("",))[0] == "done":
            return
        marker = f"[arceus:rejected:{key_part(activity_id, keep_dash=True)}]"
        self.store.action_intent(action, key, "rejected")
        if not any(c.author == self.cfg.tracker.bot_user and marker in c.text
                   for c in self.tracker.comments(issue_db_id)):
            tag = self.cfg.tracker.tags.queue
            who = author or "someone the tracker does not name"
            self.tracker.add_comment(issue_db_id, f"The `{tag}` tag was set by {who}, but only people on the project's "
                                             f"list can start the agent. The ticket was not taken. To start it, the "
                                             f"tag has to be set by someone on the list.\n\n{marker}")
        self.store.action_done(action)

    # -- publishing the reviewed commit ---------------------------------------------------------

    def publish(self, task: Task, show: dict) -> None:
        if task.research:
            # Whatever the agent committed, a research ticket publishes nothing: its answer is the result.
            self.answer(task, show, reviewed=False)
            return
        attempt = len(show.get("attempts") or [])
        receipt = show.get("receipt") or {}
        if not receipt.get("review") and self._passed_without_outcome(task, show):
            # The attempt passed review but ended without a declared outcome, so Bulava wrote no receipt (staging,
            # STG-29 and STG-37). What the review accepted is in `task show` and in `repo_reviewed`.
            self.events.emit("published_without_outcome", task_id=task.task_id, attempt=attempt)
            receipt = {"review": "passed", "repos": show["repos"], "no_outcome": True}
            show = show | {"receipt": receipt}
        if receipt.get("review") != "passed":
            self.fail(task, f"the review verdict is \"{receipt.get('review')}\", nothing to publish", attempt=attempt)
            return
        # Nothing half done is published, and nothing half done stays behind in the shared workspace either.
        left = [f"{name}: a {op} was left half done; it is undone, the branch is back as it was before it"
                for name in self.workspace.names(task)
                if (op := self._end_unfinished(task, name, self.cfg.paths.workspace / name))]
        if left:
            self.fail(task, "the attempt left a git operation half done, nothing was pushed", "\n".join(left),
                      attempt=attempt)
            return
        if self.publisher.publish(task, show, attempt):
            self.published.add(task.task_id)

    def comment_with_media(self, task: Task, kind: str, text: str,
                           visuals: list[media.MediaItem]) -> tuple[str, int] | None:
        """comment_once, with the screenshots and recordings not yet on the ticket attached to it."""
        return self.post_whole(task, kind, text, visuals=visuals)[0]

    def post_whole(self, task: Task, kind: str, head: str, text: str = "", foot: str = "",
                   visuals: list[media.MediaItem] = ()) -> list[tuple[str, int] | None]:
        """A comment whose `text` (the agent's words) reaches the ticket whole: split into parts when long
        (ticket_pieces), never cut. The screenshots not yet on the ticket go with the last part. Each part goes
        through comment_once, so a retry posts only the parts that are missing. Returns each part's (id, ms)."""
        fresh = [m for m in visuals if (self.store.action(self._attach_key(task, m)) or ("",))[0] != "done"] \
            if self.tracker.supports_attachments else []
        tail = ticket_media_text(self.tracker, fresh) if fresh else ""
        pieces = ticket_pieces(head, text, foot, tail)
        posted = [self.comment_once(task, piece_kind(kind, i), p) for i, p in enumerate(pieces, 1)]
        if posted[-1] and fresh:
            self._ticket_media(task, posted[-1][0], fresh)
        return posted

    @staticmethod
    def _attach_key(task: Task, m: media.MediaItem) -> str:
        return f"{task.task_id}:attach:{m.sha256}"

    def _ticket_media(self, task: Task, comment_id: str, items: list[media.MediaItem]) -> None:
        """Attach the files to the runner's comment on the ticket; a file already on the ticket is not sent again."""
        for m in items:
            key = self._attach_key(task, m)
            if (self.store.action(key) or ("",))[0] == "done":
                continue
            self.store.action_intent(key, task.task_id, "attach", m.upload_name)
            try:
                self.tracker.attach(task.issue_db_id, comment_id, m.upload_name, m.data(), m.mime)
            except (BadRequest, NotFound) as e:
                self.events.emit("media_skipped", task_id=task.task_id, detail=f"{m.path.name} on the ticket: {e}")
                continue
            self.store.action_done(key)
