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
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from arceus_runner import media
from arceus_runner.replies import unattributed
from arceus_runner.config import Config
from arceus_runner.db import ACTIVE_STATES, TaskStore, Task, TriggerDecision
from arceus_runner.engine import Engine, EngineError, Reply
from arceus_runner.events import EventLog
from arceus_runner.gitops import HOME_BRANCH, Git, GitError
from arceus_runner.http import BadRequest, NotFound, ServiceError
from arceus_runner.integrations import (
    CodeHost,
    Issue,
    IssueDetails,
    IssueTracker,
    MergeRequest,
    Repo,
    key_part,
    usable_repos,
)
from arceus_runner.screening import Classify, Gate, trusted

START_REFUSED = {
    76: "the repository folder is not a git repository",
    77: "the working tree has uncommitted changes from an earlier attempt",
    78: "no decision was recorded for the repository's MCP servers",
    79: "the repository has files too large for a checkpoint",
}
FAIL_REASONS = {
    "debt": "the work did not pass review",
    "codex_unavailable": "the review step is unavailable",
    "needs-user": "the review asked for a person's decision",
    "scope_violation": "the agent went outside the task's scope",
    "stalled": "the agent's session stopped at its prompt without finishing, again after being asked to finish",
}
# The review gate's words when a required independent check did not run (review-gate.sh, claude checks).
CHECK_FAILED = "independent check failed"
STALLED_NUDGE = ("Your previous session stopped at its prompt without finishing the turn, and Bulava parked it. "
                 "Pick the task up from where the work stands: finish it, commit in every repository you "
                 "changed, and declare the outcome.")
RESUME = "The session was interrupted (a reboot or a crash). Continue the task from where you stopped."
PREPARED_MARK = "arceus-prepared"
CONTINUE = "/continue"  # a comment that starts with this, from someone allowed, resumes a failed task
HOME_IGNORE = """\
# Every folder here is a repository of the project's group on its code host, cloned by the Arceus runner.
# This repository holds only the runner's notes for the agent and is never published.
/*
!/.gitignore
!/AGENTS.md
!/CLAUDE.md
"""


def home_files(where: str, repos: list[Repo], home: Path) -> dict[str, str]:
    """What the agent finds where its session starts: what the folder is, its path, and the repositories.
    `where` names the scope: GitLab group `clients/acme`."""
    lines = [f"# {where}", "",
             "This folder is not a project. Each folder in it is a separate git repository of the group,",
             "cloned by the Arceus runner and switched to the task's branch.", "",
             f"Its absolute path (the workspace): `{home}`", "",
             "- Work out from the ticket which repositories it needs, and change only those.",
             "- Commit inside each repository you change, on the branch checked out there. Commit nothing",
             "  in this folder: it belongs to the runner and is never published.",
             "- Do not push. The runner pushes the commits Codex accepted and opens one merge request per",
             "  changed repository.", "", "## Repositories", ""]
    for r in sorted(repos, key=lambda r: r.relative_path):
        lines.append(f"- `{r.relative_path}`" + (f": {' '.join(r.description.split())}" if r.description else ""))
    return {".gitignore": HOME_IGNORE, "AGENTS.md": "\n".join(lines) + "\n", "CLAUDE.md": "@AGENTS.md\n"}


def slugify(text: str, limit: int = 40) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")[:limit].strip("-")
    return slug or "task"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _short(text: str, limit: int = 1500) -> str:
    text = unattributed(text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


MEDIA_BEGIN, MEDIA_END = "<!-- arceus:media -->", "<!-- /arceus:media -->"


def media_section(shots: list[str]) -> str:
    """The MR's screenshots, between markers so a later round can replace them in place."""
    return "\n\n".join([MEDIA_BEGIN + "\n## Screenshots and recordings", *shots, MEDIA_END]) if shots else ""


def with_media_section(description: str, shots: list[str]) -> str:
    section = media_section(shots)
    start, end = description.find(MEDIA_BEGIN), description.find(MEDIA_END)
    if start != -1 and end > start:
        return description[:start] + section + description[end + len(MEDIA_END):]
    if not section:
        return description
    marker = "\n## Checks"
    at = description.find(marker)
    return (description[:at] + "\n\n" + section + description[at:]) if at != -1 else \
        description.rstrip() + "\n\n" + section


def ticket_media_text(tracker: IssueTracker, items: list[media.MediaItem]) -> str:
    """The comment's own attachments, shown in place the way the tracker does it."""
    return "\n\n".join(["Screenshots and recordings of the change:",
                         *(f"**{m.caption}**\n\n{tracker.attachment_markdown(m.upload_name, m.caption)}"
                           for m in items)])


_CAUSE = re.compile(r"not found|no such file|not on path|\berror\b|\bfailed\b|denied|timed out", re.I)


def _log_hint(log: Path) -> str:
    """The line of a check's log that names the cause: the first error-looking line that is not make's own
    `*** Error` echo, else the last line."""
    try:
        lines = [ln.strip() for ln in log.read_text(errors="replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    hint = next((ln for ln in lines if _CAUSE.search(ln) and not ln.startswith("make: ***")),
                lines[-1] if lines else "")
    return _short(hint, 200)


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
        self._waiting: dict[str, tuple] = {}  # task id → why its follow-up waits, as last written as followup_waits
        self.gate = Gate(self, classify)  # every text from people or CI passes it before the engine sees it

    # -- comments: once per (task, kind), even across a crash -----------------------------------

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

    def retry_pending(self) -> None:
        """Finish ticket updates that a failure or a crash left half done."""
        for key, task_id, kind, payload in self.store.pending_actions():
            task = self.store.task(task_id)
            if not task or not payload:
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
                elif kind == "state":
                    if task.state in data.get("while", [task.state]):  # never move a ticket back past a later step
                        self.tracker.set_state(task.issue_db_id, data["state"])
                    self.store.action_done(key)
                elif kind == "reply" and getattr(self, "review", None):
                    self.review.retry_reply(task, key, data)
            except ServiceError as e:
                self.events.emit("notify_pending", task_id=task_id, action=key, reason=str(e))
                return  # the tracker is still unwell; the next tick tries again

    # -- taking a task --------------------------------------------------------------------------

    def maybe_start(self, can_take: bool) -> None:
        if not can_take or self.workspace_taken():
            return
        if self.store.has_pending_review() or self._continues_waiting():
            return  # fixes for open MRs and resumed tasks go first
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

        base = dict(task_id=details.key, issue_db_id=details.db_id, summary=details.summary[:300],
                    tag_author=decision.author, base_branch="", question_id=None, question_at_ms=None,
                    reason=None, mr_urls="[]", attempt_started=None)
        repos = self._group()
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
                         branch=task.branch)
        self._take(task, repos, details)
        return True

    def _not_taken(self, decision: TriggerDecision, reason: str) -> bool:
        self._skipped.add((decision.issue_key, decision.tag_activity_id))
        self.events.emit("task_not_taken", task_id=decision.issue_key, reason=reason)
        return False

    def _take(self, task: Task, repos: list[Repo], details: IssueDetails) -> None:
        # Screened as it is now, at the start, not when it was tagged: anyone may have edited it since.
        # A held ticket is not claimed: it stays in the queue state, its workspace untouched.
        text = f"{task.task_id}: {details.summary}\n\n{details.description}".strip()
        clearance = self.gate.clear(task, "ticket", text, authors=[task.tag_author, *details.writers],
                                    resume_state="claimed")
        if clearance is None:
            return
        why = self.prepare_workspace(task, repos)
        if why:
            self.fail(task, why)
            return
        self.claim_on_ticket(task)
        self.dispatch(task, text, clearance)

    def _group(self) -> list[Repo]:
        return usable_repos(self.cfg, self.host.repos())

    def _names(self, task: Task) -> list[str]:
        """The task's repositories, by path below the group (the starting one first for older tasks)."""
        return [n for n in [task.repo, *json.loads(task.extra_repos or "[]")] if n]

    def _task_repos(self, task: Task, repos: list[Repo]) -> list[Repo]:
        """The task's repositories as they were when it was taken."""
        by_name = {r.relative_path: r for r in repos}
        return [by_name[n] for n in self._names(task) if n in by_name]

    def prepare_workspace(self, task: Task, repos: list[Repo]) -> str | None:
        """Clone or fetch every repository of the task, the task branch in each; the home ready.

        The folder the session starts in gets its branch from dispatch.sh itself (--branch, --base,
        --dirty fail). A reason is returned instead when the work cannot start.
        """
        mine = self._task_repos(task, repos)
        started = self.now()
        self.events.emit("workspace_preparing", task_id=task.task_id, repos=len(mine))
        for repo in mine:
            self.ensure_repo(repo, self.cfg.paths.workspace / repo.relative_path)
        extras = [r for r in mine if r.relative_path != task.repo]
        dirty = [r.relative_path for r in extras
                 if not self.git.is_clean(self.cfg.paths.workspace / r.relative_path)]
        if dirty:
            return f"{START_REFUSED[77]}: {', '.join(dirty)}"
        for repo in extras:
            self.git.start_branch(self.cfg.paths.workspace / repo.relative_path, task.branch,
                                  f"origin/{repo.default_branch or 'main'}")
        if not task.repo:
            self._prepare_home(repos)
        self._write_extra_dirs(task)
        self.events.emit("workspace_ready", task_id=task.task_id, repos=len(mine),
                         seconds=round((self.now() - started).total_seconds(), 1))
        return None

    def _prepare_home(self, repos: list[Repo]) -> None:
        """The workspace folder as the git project Bulava runs in, on main with the current notes."""
        home = self.cfg.paths.workspace
        h = self.host
        if self.git.write_home(home, home_files(f"{h.label} {h.scope_kind} `{h.scope}`", repos, home.resolve()),
                               f"Arceus: workspace of {h.label} {h.scope_kind} {h.scope}"):
            self.events.emit("workspace_home_updated", path=str(home), repos=len(repos))
        if not (home / ".git" / PREPARED_MARK).exists():
            self.engine.prepare_repo(home)
            (home / ".git" / PREPARED_MARK).write_text("trust, mcp-decide skip\n")

    def _extra_dirs_file(self, task: Task) -> Path | None:
        extras = json.loads(task.extra_repos or "[]")
        if not extras:
            return None
        return self.cfg.paths.db.parent / "tasks" / task.task_id / "extra-dirs.txt"

    def _write_extra_dirs(self, task: Task) -> None:
        f = self._extra_dirs_file(task)
        if not f:
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(f"{self.cfg.paths.workspace / r}\n" for r in json.loads(task.extra_repos))
        f.write_text(lines)
        # All of them are the agent's to change (engine/SERVER.md: listed beside the file).
        (f.parent / "writable-directories.txt").write_text(lines)

    def workspace_taken(self, task: Task | None = None) -> Task | None:
        """The task working in the shared workspace now, other than `task`; None when it is free.

        Every repository of the group and the home are one checkout, so whatever switches a branch
        there waits for it. A task's own resume or nudge does not wait for itself.
        """
        return next((t for t in self.store.tasks(*ACTIVE_STATES) if task is None or t.task_id != task.task_id), None)

    def _workspace_waits(self, task: Task) -> bool:
        """True (and noted once) while another task holds the workspace: `task`'s follow-up keeps for a later tick."""
        holder = self.workspace_taken(task)
        if holder is None:
            return False
        self._note_waiting(task, "workspace_taken", holder=holder.task_id)
        return True

    def _note_waiting(self, task: Task, reason: str, **fields) -> None:
        """A `followup_waits` line when the reason changes; nothing on a tick that waits for the same thing."""
        seen = (reason, *sorted((k, str(v)) for k, v in fields.items()))
        if self._waiting.get(task.task_id) == seen:
            return
        self._waiting[task.task_id] = seen
        self.events.emit("followup_waits", task_id=task.task_id, reason=reason, **fields)

    def _followup_ready(self, task: Task) -> bool:
        """Before a follow-up: every repository of the task on the task branch, caught up with the code host.

        The workspace is shared, so another task may have switched a repository to its own branch
        in between; and people may have pushed to the MR branch. Both are put right here; nothing
        local is rewritten (catch_up only fast-forwards).

        False, with nothing touched, while the follow-up has to wait: another task works in the
        workspace, or a repository that has to change branch holds uncommitted work (a checkout
        would carry it into this task's branch). The caller sends nothing and tries on a later tick.
        """
        if self._workspace_waits(task):
            return False
        home = self.cfg.paths.workspace
        home_moves = (not task.repo and (home / ".git").exists() and self.git.branch_exists(home, task.branch)
                      and self.git.current_branch(home) != task.branch)
        repos = []  # (name, path, has to change branch)
        for name in self._names(task):
            path = self.cfg.paths.workspace / name
            if not (path / ".git").exists() or not self.git.branch_exists(path, task.branch):
                continue
            repos.append((name, path, self.git.current_branch(path) != task.branch))
        # All checked before anything is switched: a follow-up never starts in a half-switched workspace.
        dirty = (["."] if home_moves and not self.git.is_clean(home) else []) + \
            [name for name, path, moves in repos if moves and not self.git.is_clean(path)]
        if dirty:
            self._note_waiting(task, "uncommitted_changes", repos=dirty)
            return False
        self._waiting.pop(task.task_id, None)
        if home_moves:
            self.git.home_on(home, task.branch)  # another task started from main in between
        for name, path, moves in repos:
            if moves:
                self.git.start_branch(path, task.branch, "HEAD")
            if self.store.task_mrs(task.task_id) and self.git.is_clean(path):
                moved = self.git.catch_up(path, task.branch)
                if moved in ("forwarded", "diverged"):  # "same" and "ahead": nothing came from the code host
                    self.events.emit("branch_caught_up", task_id=task.task_id, repo=name, result=moved)
        self._write_extra_dirs(task)
        return True

    def ensure_repo(self, repo: Repo, path: Path) -> None:
        if not (path / ".git").exists():
            self.git.clone(repo.http_url, path)
            self.events.emit("repo_cloned", repo=repo.relative_path)
        fixed = self.git.fix_modes(path)
        if fixed:
            self.events.emit("workspace_modes_fixed", repo=repo.relative_path, files=fixed)
        if not (path / ".git" / PREPARED_MARK).exists():
            self.git.configure(path)
            self.engine.prepare_repo(path)
            (path / ".git" / PREPARED_MARK).write_text("trust, mcp-decide skip\n")
        self.git.fetch(path)

    def claim_on_ticket(self, task: Task) -> None:
        self.tracker.set_state(task.issue_db_id, self.cfg.tracker.states.in_progress)
        try:
            self.tracker.assign_to_bot(task.issue_db_id)
        except BadRequest as e:
            self.events.emit("assignee_not_set", task_id=task.task_id, reason=str(e))
        n = len(self._names(task))
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
                                     extra_dirs=self._extra_dirs_file(task))
        if reply.accepted:
            self.store.update_task(task.task_id, state="dispatched", attempt_started=self.now().isoformat())
            # What was asked for. The task keeps it through every follow-up (the engine's run-env);
            # `auto` means the CLI chose, and only its own transcripts say what that was.
            self.events.emit("task_dispatched", task_id=task.task_id, tier=reply.tier,
                             attempt=reply.data.get("attempt"), dispatch_id=reply.data.get("dispatch_id"),
                             **self.cfg.bulava.model_profile(), **self.cfg.bulava.claude_checks.profile())
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
                if any(t.task_id != task.task_id for t in self.store.tasks("dispatched", "publishing")):
                    return  # another task works in the shared workspace: this one starts after it
                self._take(task, self._group(), self.tracker.issue(task.task_id))
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
        elif state == "done" and self.store.task_mrs(task.task_id):
            # A follow-up the agent answered without changing code: say why, stay in Code Review.
            self.review.answered_without_changes(task, (show.get("outcome") or {}).get("summary") or "")
        elif state == "done":
            # succeeded_no_change / succeeded_research, reviewed by Codex: a result, nothing to push.
            summary = (show.get("outcome") or {}).get("summary") or "No code changes were needed."
            self.comment_once(task, f"done:{len(attempts)}",
                              f"Arceus finished without code changes; the result passed review.\n\n{_short(summary)}")
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
            if why == "debt" and self.store.task_mrs(task.task_id) \
                    and outcome.get("result") in ("succeeded_no_change", "succeeded_research"):
                # A review round answered without a code change. Bulava cannot verify an empty diff and
                # banks it as debt; nothing is pushed either way, so it is an answer, not a failure.
                self.review.answered_without_changes(task, outcome.get("summary") or "")
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
            review_why = receipt.get("review_why") or ""
            if why == "needs-user" and CHECK_FAILED in review_why:
                # bulava.claude_checks.required: the engine stopped before Codex, so it is not Codex's decision.
                # The gate's own words stay in the event log; the ticket gets which checks did not run.
                self.events.emit("required_check_missed", task_id=task.task_id, review_why=review_why)
                kinds = re.search(rf"{CHECK_FAILED} \(([^)]*)\)", review_why)
                self.fail(task, "a required pre-review check (code or security) did not run, "
                                "so the work was not reviewed",
                          f"Checks that did not run: {kinds.group(1) if kinds else 'code or security'}.",
                          attempt=len(attempts))
                return
            reason = FAIL_REASONS.get(why) or FAIL_REASONS.get(outcome.get("reason") or "") or why
            self.fail(task, reason, outcome.get("summary") or "", attempt=len(attempts))
        else:
            self.events.emit("unknown_engine_state", task_id=task.task_id, state=state)

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
        if not self._followup_ready(task):
            return True  # the nudge waits for the workspace: not a refusal
        reply = self.engine.send(task_id=task.task_id, message_id=f"stalled-{task.task_id}-{attempt}", source="other",
                                 author=self.cfg.tracker.bot_user, repo=Path(task.repo_path), text=STALLED_NUDGE,
                                 clearance=trusted(STALLED_NUDGE), extra_dirs=self._extra_dirs_file(task))
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

    def _verifier_gaps(self, receipt: dict) -> tuple[list[str], list[str]]:
        """The verifier's failed and inconclusive checks for this run, one line each, from its evidence.json."""
        failed, unproven = [], []
        run_id = receipt.get("run_id") or ""
        if not run_id or "/" in run_id:
            return failed, unproven
        for path in sorted((self.cfg.paths.bulava_state / "runs" / run_id / "evidence").glob("*/evidence.json")):
            try:
                criteria = json.loads(path.read_text()).get("criteria") or []
            except (OSError, ValueError, AttributeError):
                continue
            for c in criteria:
                status = c.get("status")
                if status not in ("fail", "inconclusive"):
                    continue
                line = f"`{c.get('criterion') or c.get('command') or '?'}`"
                if status == "fail":
                    line += f" failed (exit {c.get('exit_code')})"
                else:
                    line += " was inconclusive"
                hint = c.get("note") or _log_hint(path.parent / Path(c.get("artifact") or "").name)
                if hint:
                    line += f": {hint}"
                (failed if status == "fail" else unproven).append(line)
        return failed, unproven

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
        # A usage-limit pause is not a hang: the clock stops while a limit is out.
        out = self._limits_exhausted()
        if out:
            if task.attempt_started != "paused":
                self.events.emit("attempt_paused_for_limit", task_id=task.task_id, limits=out)
            self.store.update_task(task.task_id, attempt_started=self.now().isoformat())
            return
        times = [t for t in (_parse_ts((attempts[-1] if attempts else {}).get("started_at")),
                             _parse_ts(task.attempt_started)) if t]
        started = max(times) if times else None
        limit = self.cfg.limits.attempt_timeout
        if started and self.now() - started > limit:
            self.engine.stop(Path(task.repo_path))
            self.fail(task, f"the agent hung: no result in {limit.total_seconds() / 3600:.0f} h", attempt=len(attempts))

    def _resume(self, task: Task, attempt: int) -> None:
        if not self._followup_ready(task):
            return  # sent on a later tick, once the workspace is free
        reply = self.engine.send(task_id=task.task_id, message_id=f"resume-{task.task_id}-{attempt}", source="other",
                                 author=self.cfg.tracker.bot_user, repo=Path(task.repo_path),
                                 extra_dirs=self._extra_dirs_file(task), text=RESUME, clearance=trusted(RESUME))
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
        body = f"Arceus could not take this ticket to a merge request: {reason}."
        if details:
            body += f"\n\n{_short(details)}"
        if task.branch:
            body += (f"\n\nTo have Arceus pick this up again, reply with a comment that starts with "
                     f"`{CONTINUE}`, followed by anything it should know (what was fixed, what to do "
                     f"differently). Its branches and commits are kept.")
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
            self.store.action_intent(f"{task.task_id}:comment:{kind}", task.task_id, "comment",
                                     json.dumps({"kind": kind, "text": body}))
            if also:
                also()
        self.events.emit("task_failed", task_id=task.task_id, reason=reason)
        try:
            self.comment_once(task, kind, body)
            self.tag_once(task, tag, tag_key)
        except ServiceError as e:
            self.events.emit("notify_pending", task_id=task.task_id, reason=str(e))

    def _save_progress(self, task: Task) -> None:
        """The failed attempt's uncommitted work, committed on the task branch where it stands.

        Nothing is lost for a later `/continue`, and the shared workspace is clean for the next task
        (Bulava refuses to start on a dirty tree). It is reviewed with the rest before anything is
        published; the runner never pushes a commit Codex did not accept.
        """
        for name in self._names(task):
            path = self.cfg.paths.workspace / name
            try:
                if not (path / ".git").exists() or self.git.current_branch(path) != task.branch \
                        or self.git.is_clean(path):
                    continue
                sha = self.git.commit_all(path, f"WIP [{task.task_id}]: uncommitted work of a failed attempt, "
                                                "kept by the Arceus runner")
                self.events.emit("progress_saved", task_id=task.task_id, repo=name, sha=sha)
            except GitError as e:
                self.events.emit("progress_not_saved", task_id=task.task_id, repo=name, reason=str(e))

    def ask(self, task: Task, show: dict) -> None:
        q = show.get("question") or {}
        outcome = show.get("outcome") or {}
        attempt = len(show.get("attempts") or [])
        text = q.get("text") or outcome.get("summary") or "The agent needs a decision from a person."
        lines = [f"{self.tracker.mention(task.tag_author)}, the agent needs an answer to continue:", "", _short(text)]
        for i, opt in enumerate(q.get("options") or [], 1):
            lines.append(f"{i}. {opt}")
        lines += ["", "Reply with a comment on this ticket: the option's number or your own words."]
        posted = self.comment_once(task, f"question:{q.get('id') or attempt}", "\n".join(lines))
        self.tracker.add_tag(task.issue_db_id, self.cfg.tracker.tags.needs_input)
        self.store.update_task(task.task_id, state="needs_input", question_id=q.get("id"),
                               question_at_ms=posted[1] if posted else None)
        self.events.emit("task_needs_input", task_id=task.task_id, question_id=q.get("id"),
                         blocked=show.get("state") == "blocked")

    def check_answers(self) -> None:
        """A comment from an allowed person after the question is the answer."""
        c = self.cfg.tracker
        for task in self.store.tasks("needs_input"):
            try:
                after = task.question_at_ms or 0
                # >=: a reply in the same millisecond as the question counts. The engine dedups by
                # message id, so a comment seen twice is sent once.
                answer = next((x for x in self.tracker.comments(task.issue_db_id)
                               if x.created >= after and x.author != c.bot_user and x.author in c.allowed_users
                               and x.text.strip()), None)
                if not answer:
                    self._unanswered(task)
                    continue
                if self._workspace_waits(task):
                    continue  # the answer is read again on a later tick; the task stays in needs_input
                clearance = self.gate.clear(task, "answer", answer.text, authors=[answer.author],
                                            resume_state="needs_input")
                if clearance is None or not self._followup_ready(task):
                    continue
                # The engine dedups by message id: the comment's id escaped (unchanged for YouTrack's), and the
                # plain retry after a `:`, which an escaped id never contains.
                comment = key_part(answer.id, keep_dash=True)
                reply = self.engine.send(task_id=task.task_id, message_id=f"yt-comment-{comment}",
                                         source=self.tracker.engine_source, author=answer.author,
                                         repo=Path(task.repo_path),
                                         text=answer.text, clearance=clearance, answers=task.question_id,
                                         extra_dirs=self._extra_dirs_file(task))
                if reply.rc == 4 and task.question_id:
                    # The question is no longer open on the engine's side: send it as plain follow-up.
                    reply = self.engine.send(task_id=task.task_id, message_id=f"yt-comment-{comment}:plain",
                                             source=self.tracker.engine_source, author=answer.author,
                                             repo=Path(task.repo_path),
                                             text=answer.text, clearance=clearance,
                                             extra_dirs=self._extra_dirs_file(task))
                if self._after_send(task, reply, "answer_sent", untag=c.tags.needs_input, author=answer.author):
                    if hasattr(self.store, "consume_signal"):
                        self.store.consume_signal(f"yt-{answer.id}", task.task_id, "ticket", answer.author,
                                                  answer.text, self.now().isoformat(), "answer")
                    self.retry_pending()  # takes the needs-input tag off now, or on a later tick
            except (ServiceError, EngineError, GitError) as e:
                self.events.emit("reconcile_failed", task_id=task.task_id, reason=str(e))

    # -- picking a failed task up again ---------------------------------------------------------

    def check_continue(self) -> None:
        """A `/continue` comment from an allowed person after a failure: recorded, sent by send_continue()."""
        c = self.cfg.tracker
        window = self.cfg.limits.continue_window
        for task in self.store.tasks("failed"):
            if not task.failed_at_ms or not task.branch:
                continue  # failed before any work existed (no repositories), or before this was possible
            if self.now() - datetime.fromtimestamp(task.failed_at_ms / 1000, UTC) > window:
                continue
            try:
                comments = self.tracker.comments(task.issue_db_id)
            except ServiceError as e:
                self.events.emit("reconcile_failed", task_id=task.task_id, reason=str(e))
                return
            for x in comments:
                if x.created < task.failed_at_ms or x.author == c.bot_user or x.author not in c.allowed_users:
                    continue
                if not x.text.strip().lower().startswith(CONTINUE):
                    continue
                key = f"{task.task_id}:continue:{key_part(x.id, keep_dash=True)}"
                if self.store.action(key):
                    continue  # this one was handled (or is waiting to be)
                self.store.action_intent(key, task.task_id, "continue", json.dumps(
                    {"comment": x.id, "author": x.author, "created": x.created,
                     "text": x.text.strip()[len(CONTINUE):].strip()}))
                self.events.emit("continue_requested", task_id=task.task_id, author=x.author, comment=x.id)
                break  # one request per failure: the next failure sets a new boundary

    def send_continue(self) -> None:
        """Hand a recorded `/continue` to Bulava as the next attempt of the same task."""
        for key, task_id, payload in self.store.pending_continues():
            task = self.store.task(task_id)
            data = json.loads(payload or "{}")
            if task and task.state == "security_hold":
                continue  # the request itself waits for a security reviewer
            if not task or task.state != "failed":
                self.store.action_done(key, "stale")  # it moved on by another way meanwhile
                continue
            if self._workspace_waits(task):
                return  # the workspace is shared: wait until the running task is over
            try:
                self._send_continue(task, key, data)
            except (ServiceError, EngineError, GitError) as e:
                self.events.emit("reconcile_failed", task_id=task.task_id, reason=str(e))
            return  # one at a time

    def _send_continue(self, task: Task, key: str, data: dict) -> None:
        if self._workspace_waits(task):
            return  # before either way below: both switch the workspace
        if self.engine.show(task.task_id) is None:
            # Bulava never had it (the start was refused) or lost it: start it again from the ticket,
            # on the same branches, which keep whatever was committed on them.
            self._resumed(task, key, data, state="claimed", attempt="new")
            self.reconcile()
            return
        if not self._followup_ready(task):
            return  # the request stays pending
        text = (f"{data['author']} asked you to continue this task after it failed ({task.reason}). "
                f"Pick it up from where the work stands and finish it.")
        if data.get("text"):
            text += f"\n\n{data['text']}"
            clearance = self.gate.clear(task, "continue", text, authors=[data["author"]], resume_state="failed",
                                        ref=key)
            if clearance is None:
                return
        else:
            clearance = trusted(text)  # a bare `/continue`: only the runner's words
        reply = self.engine.send(task_id=task.task_id, message_id=f"yt-continue-{data['comment']}",
                                 source=self.tracker.engine_source, author=data["author"], repo=Path(task.repo_path),
                                 text=text, clearance=clearance, extra_dirs=self._extra_dirs_file(task))
        if reply.accepted:  # dispatched, or a duplicate of what Bulava took before a crash
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
                                         if sg.kind == "ticket"], "continue")
            self.store.action_done(key, json.dumps({"attempt": attempt}))
            cid = key_part(data["comment"], keep_dash=True)
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

    def _unanswered(self, task: Task) -> None:
        """No answer yet: a reminder after needs_input_reminder, Failed after needs_input_fail_after."""
        if not task.question_at_ms:
            return
        asked = datetime.fromtimestamp(task.question_at_ms / 1000, UTC)
        waited = self.now() - asked
        lim = self.cfg.limits
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

    def _why_rejected(self, path: Path, branch: str, sha: str, remote: str | None, err: GitError) -> tuple[str, str]:
        """The branch on the code host is not the reviewed commit and cannot be moved there without force."""
        host = self.host.label
        if remote:
            try:
                self.git.fetch_branch(path, branch)
                if self.git.is_ancestor(path, sha, remote):
                    # Someone (the agent, or a person) pushed more on top of what Codex reviewed.
                    return (f"the branch on {host} has commits that were not reviewed",
                            f"reviewed {sha[:12]}, on {host} {remote[:12]}; the branch is left as it is, no MR opened")
            except GitError:
                pass
            return (f"the branch on {host} has diverged from the reviewed commit",
                    f"reviewed {sha[:12]}, on {host} {remote[:12]}; no force push")
        return "the push was rejected", str(err)

    def publish(self, task: Task, show: dict) -> None:
        receipt = show.get("receipt") or {}
        if receipt.get("review") != "passed":
            self.fail(task, f"the review verdict is \"{receipt.get('review')}\", nothing to publish",
                      attempt=len(show.get("attempts") or []))
            return
        task = self.store.update_task(task.task_id, state="publishing")
        repos = receipt.get("repos") or []
        workspace = self.cfg.paths.workspace.resolve()
        problems: list[str] = []
        plan: list[tuple[Path, dict]] = []
        mine = {(workspace / n).resolve() for n in self._names(task)}
        seen: set[Path] = set()
        for r in repos:
            path = Path(r.get("path") or "").resolve()
            name = r.get("path")
            if path == workspace and not task.repo:
                # The home is the runner's, never published: whatever was committed there stays local.
                if (r.get("reviewed_sha") or "") != (r.get("base_sha") or ""):
                    self.events.emit("home_commits_not_published", task_id=task.task_id,
                                     base=r.get("base_sha"), reviewed=r.get("reviewed_sha"))
                continue
            if workspace not in path.parents:
                problems.append(f"{name}: outside the workspace")
                continue
            if path not in mine:
                problems.append(f"{name}: not one of this task's repositories")
                continue
            seen.add(path)
            if r.get("branch") != task.branch:
                problems.append(f"{name}: branch {r.get('branch')!r}, expected {task.branch!r}")
            if not r.get("clean"):
                problems.append(f"{name}: the working tree is not clean")
            reviewed, base = r.get("reviewed_sha") or "", r.get("base_sha") or ""
            try:
                head, current = self.git.head(path), self.git.current_branch(path)
            except GitError as e:
                problems.append(f"{name}: {e}")
                continue
            if head != reviewed:
                problems.append(f"{name}: HEAD {head[:12]} is not the reviewed {reviewed[:12]}")
            if current != r.get("branch"):
                problems.append(f"{name}: branch {current!r} is checked out")
            if reviewed and reviewed != base:
                plan.append((path, r))
        for path in sorted(mine - seen):
            # A repository Bulava did not report on may still hold the agent's commits: never lose them silently.
            try:
                if path.exists() and self.git.branch_exists(path, task.branch) \
                        and self.git.current_branch(path) == task.branch and self.git.has_unpushed(path):
                    problems.append(f"{self._repo_name(path)}: has commits on {task.branch}, but the execution engine "
                                    "did not report on this repository")
            except GitError as e:
                problems.append(f"{self._repo_name(path)}: {e}")
        if problems:
            # Nothing is pushed unless every repository checks out.
            self.fail(task, "the commit is not the reviewed one, nothing was pushed", "\n".join(problems),
                      attempt=len(show.get("attempts") or []))
            return
        if not plan:
            self.fail(task, "no changes", attempt=len(show.get("attempts") or []))
            return

        group = {(workspace / r.relative_path).resolve(): r for r in self.host.repos()}
        missing = [str(path) for path, _ in plan if path not in group]
        if missing:  # all of them are resolved before the first push
            self.fail(task, f"a repository is not in the {self.host.label} {self.host.scope_kind}, nothing was pushed",
                      "\n".join(missing))
            return
        unreviewed = self._below_base(task, plan, group)
        if unreviewed:
            self.fail(task, "the branch has commits below the reviewed range, nothing was pushed",
                      "\n".join(unreviewed), attempt=len(show.get("attempts") or []))
            return
        plan, dropped, reopened = self._closed_mrs(task, plan, group)
        if reopened:
            self.fail(task, "a merge request of this task was closed without merge, but its branch has changes "
                            "again; nothing was pushed", "\n".join(reopened), attempt=len(show.get("attempts") or []))
            return
        stored = {m.repo: m for m in self.store.task_mrs(task.task_id)}
        dropped = [d for d in dropped if d[0] in stored]
        for repo, mr, sha in dropped:
            # The agent took its changes back out: the MR is no longer part of this task.
            if mr.state == "opened":
                pid = stored[repo].project_id
                key = f"{task.task_id}:close-mr:{key_part(pid)}:{key_part(mr.iid)}"
                if not self.store.action(key):  # the note once, also when the close is retried after a crash
                    self.host.note_mr(pid, mr.iid, (
                        f"Closed by the Arceus runner: the agent took its changes for {task.task_id} back out of "
                        f"this repository (reviewed commit `{sha[:12]}`), so there is nothing left to merge here. "
                        "The rest of the task goes on in its other merge requests."))
                    self.store.action_intent(key, task.task_id, "close-mr")
                self.host.close_mr(pid, mr.iid)
                self.store.action_done(key)
            self.store.drop_mr(task.task_id, repo)
            self.events.emit("mr_dropped", task_id=task.task_id, repo=repo, mr_iid=mr.iid, url=mr.url,
                             closed_by_runner=mr.state == "opened")
        if dropped:
            gone = {mr.url for _, mr, _ in dropped}
            task = self.store.update_task(task.task_id, mr_urls=json.dumps(
                sorted(set(json.loads(task.mr_urls)) - gone)))
        if not plan:
            self.fail(task, "the agent took all of its changes back out; its merge requests are closed" if dropped
                      else "no changes", attempt=len(show.get("attempts") or []))
            return
        visuals, skipped = media.load(media.media_dir(workspace, task.task_id))
        for line in skipped:
            self.events.emit("media_skipped", task_id=task.task_id, detail=line)
        urls: list[str] = []
        for path, r in plan:
            repo = group[path]
            sha, branch = r["reviewed_sha"], r["branch"]
            if self.git.remote_sha(path, branch) != sha:
                try:
                    self.git.push_sha(path, sha, branch)
                except GitError as e:
                    # The server may have taken the push before the connection broke. Ask it: the
                    # right commit there is success; an unreachable remote stays for the next tick.
                    remote = self.git.remote_sha(path, branch)
                    if remote != sha:
                        if remote and self.store.task_mrs(task.task_id):
                            self.git.fetch_branch(path, branch)
                            if not self.git.is_ancestor(path, sha, remote):
                                # A person pushed to the MR branch while the agent was fixing: their
                                # commits stay, and the agent merges them in on the next round.
                                self.review.note_moved(task, repo.relative_path, remote, sha)
                                return
                            # The reviewed commit is already in, people added more on top: theirs to own.
                            self.events.emit("branch_ahead_of_review", task_id=task.task_id,
                                             repo=repo.relative_path, reviewed=sha, remote=remote)
                        else:
                            self.fail(task, *self._why_rejected(path, branch, sha, remote, e))
                            return
            remote = self.git.remote_sha(path, branch)
            if remote != sha and not (remote and self.store.task_mrs(task.task_id)
                                      and self.git.is_ancestor(path, sha, remote)):
                self.fail(task, f"after the push the branch on {self.host.label} is not the reviewed commit",
                          f"{repo.relative_path}: {remote} instead of {sha}")
                return
            self.events.emit("pushed", task_id=task.task_id, repo=repo.relative_path, branch=branch, sha=sha)
            shots = self._mr_media(task, repo.id, repo.relative_path, media.for_repo(visuals, repo.relative_path))
            description = self._mr_description(task, show, path, r, shots)
            existed = {m.repo: m for m in self.store.task_mrs(task.task_id)}.get(repo.relative_path)
            mr = self._open_mr(repo, task, branch, description, after_closed=bool(r.get("after_closed")))
            if existed or mr.state != "opened" or self.store.task_mrs(task.task_id):
                self.host.set_description(repo.id, mr.iid, description)  # a later round: describe what is there now
            self.store.upsert_mr(task.task_id, repo.relative_path, repo.id, mr.iid, mr.url, sha)
            urls.append(mr.url)
            self.events.emit("mr_ready", task_id=task.task_id, repo=repo.relative_path, mr_iid=mr.iid, url=mr.url)

        task = self.review.count_round(task)  # a review round counts once it ends in published code
        follow_up = task.review_rounds or task.ci_attempts
        self.store.update_task(task.task_id, mr_urls=json.dumps(sorted(set(urls) | set(json.loads(task.mr_urls)))))
        shas = "-".join(r["reviewed_sha"][:10] for _, r in plan)
        head = ("Arceus addressed the review comments; the updated changes passed review. Same MR, updated branch:"
                if follow_up else "Arceus finished the work; the changes passed review. Merge request:")
        body = head + "\n" + "\n".join(f"- {u}" for u in urls)
        if dropped:
            body += "\n\nNo longer part of this task (its changes were taken back out, the MR is closed):\n" + \
                "\n".join(f"- {mr.url}" for _, mr, _ in dropped)
        posted = self.comment_with_media(task, f"mr:{shas}", body, visuals)
        self.review.after_publish(task, {self._repo_name(p): r["reviewed_sha"] for p, r in plan})
        self.tracker.set_state(task.issue_db_id, self.cfg.tracker.states.code_review)
        self.store.update_task(task.task_id, state="code_review", reason=None,
                               published_at_ms=task.published_at_ms or (posted[1] if posted else None))
        self.events.emit("task_published", task_id=task.task_id, mrs=urls, round=follow_up)

    def _open_mr(self, repo: Repo, task: Task, branch: str, description: str, after_closed: bool) -> MergeRequest:
        """The branch's MR, created unless it already has one (a retry after a crash, or a person closed it).

        `after_closed`: a person asked for more work after closing the branch's MR, so a closed one
        is not the answer and a new MR is opened next to it.
        """
        existing = self.host.branch_mr(repo.id, branch)
        if existing and not (after_closed and existing.state == "closed"):
            return existing
        return self.host.create_mr(repo.id, source_branch=branch, target_branch=repo.default_branch or task.base_branch,
                                   title=f"{task.task_id}: {task.summary}"[:250], description=description,
                                   label=self.cfg.code_host.mr_label)

    def _below_base(self, task: Task, plan: list[tuple[Path, dict]], group: dict) -> list[str]:
        """Repositories whose reviewed range does not start where the branch left the default branch.

        Codex saw `base_sha..reviewed_sha`. A base above the branch point (an engine that measured
        from the start of the attempt, not of the task) leaves the commits below it unreviewed,
        and pushing the branch would publish them.
        """
        out = []
        for path, r in plan:
            default = f"origin/{group[path].default_branch or task.base_branch or 'main'}"
            if self.git.is_ancestor(path, r["base_sha"], default):
                continue
            below = self.git.log_subjects(path, default, r["base_sha"], limit=10)
            out.append(f"{group[path].relative_path}: the review started at {r['base_sha'][:12]}, which is not on "
                       f"{default}; never reviewed: " + ("; ".join(below) or "unknown commits"))
        return out

    def _closed_mrs(self, task: Task, plan: list[tuple[Path, dict]], group: dict) \
            -> tuple[list[tuple[Path, dict]], list[tuple[str, MergeRequest, str]], list[str]]:
        """Split off the repositories whose branch has nothing left to merge, and those the code host refuses.

        A branch whose commits add up to nothing against the default branch (the agent reverted its
        work there, its way of taking a repository out of the task) is not pushed and gets no MR; its
        MR leaves the task, closed by the runner when it is still open. A branch that does change
        something while a person closed its MR cannot go anywhere: that decision stands, and the
        runner neither reopens the MR nor opens another.
        """
        kept, dropped, refused = [], [], []
        for path, r in plan:
            repo = group[path]
            default = f"origin/{repo.default_branch or task.base_branch or 'main'}"
            changed = self.git.changes_since_fork(path, default, r["reviewed_sha"])
            mr = self.host.branch_mr(repo.id, r["branch"])
            if changed and mr and mr.state == "closed" and self._asked_after(task, mr):
                kept.append((path, {**r, "after_closed": True}))  # a new MR next to the closed one
            elif changed and mr and mr.state == "closed":
                refused.append(f"{repo.relative_path}: {mr.url} was closed by a person; reopen it, or have the "
                               f"agent take these changes out of {repo.relative_path}")
            elif changed:
                kept.append((path, r))
            else:
                self.events.emit("no_net_change", task_id=task.task_id, repo=repo.relative_path,
                                 reviewed=r["reviewed_sha"])
                if mr and mr.state in ("opened", "closed"):  # a merged one stays: it is done, not dropped
                    dropped.append((repo.relative_path, mr, r["reviewed_sha"]))
        return kept, dropped, refused

    @staticmethod
    def _asked_after(task: Task, mr: MergeRequest) -> bool:
        """The MR was closed before the last go-ahead (`/continue`) people gave the task after its closing."""
        return bool(mr.closed_at_ms and task.published_at_ms and mr.closed_at_ms < task.published_at_ms)

    def _repo_name(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.cfg.paths.workspace.resolve()))

    def _mr_media(self, task: Task, project_id, repo: str, items: list[media.MediaItem]) -> list[str]:
        """Upload the agent's screenshots and recordings to the project once each; Markdown for the MR."""
        out = []
        if not self.host.supports_uploads:
            return out
        for m in items:
            key = f"{task.task_id}:upload:{key_part(project_id)}:{m.sha256}"
            act = self.store.action(key)
            if act and act[0] == "done":
                url = act[1]
            else:
                self.store.action_intent(key, task.task_id, "upload", m.upload_name)
                try:
                    url = self.host.upload(project_id, m.upload_name, m.data(), m.mime)
                except (BadRequest, NotFound) as e:  # refused (size, type): the MR goes out without it
                    self.events.emit("media_skipped", task_id=task.task_id, repo=repo, detail=f"{m.path.name}: {e}")
                    continue
                self.store.action_done(key, url)
            out.append(f"**{m.caption}**\n\n![{m.caption}]({url})")
        return out

    def comment_with_media(self, task: Task, kind: str, text: str,
                           visuals: list[media.MediaItem]) -> tuple[str, int] | None:
        """comment_once, with the screenshots and recordings not yet on the ticket attached to it."""
        fresh = [m for m in visuals if (self.store.action(self._attach_key(task, m)) or ("",))[0] != "done"] \
            if self.tracker.supports_attachments else []
        if fresh:
            text += "\n\n" + ticket_media_text(self.tracker, fresh)
        posted = self.comment_once(task, kind, text)
        if posted and fresh:
            self._ticket_media(task, posted[0], fresh)
        return posted

    def refresh_media(self, task: Task) -> list[media.MediaItem]:
        """A round that changed no code may still bring new screenshots: put them into the open MRs."""
        visuals, skipped = media.load(media.media_dir(self.cfg.paths.workspace.resolve(), task.task_id))
        for line in skipped:
            self.events.emit("media_skipped", task_id=task.task_id, detail=line)
        if not visuals:
            return []
        for mr in self.store.task_mrs(task.task_id):
            if mr.state not in ("", "opened"):
                continue
            shots = self._mr_media(task, mr.project_id, mr.repo, media.for_repo(visuals, mr.repo))
            current = self.host.description(mr.project_id, mr.iid)
            updated = with_media_section(current, shots)
            if updated != current:
                self.host.set_description(mr.project_id, mr.iid, updated)
        return visuals

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

    def _mr_description(self, task: Task, show: dict, path: Path, r: dict, shots: list[str] = ()) -> str:
        """What the MR says: the ticket, what the agent says it did, the commits, how it was checked."""
        outcome = (show.get("outcome") or {}).get("summary") or ""
        commits = self.git.log_subjects(path, r["base_sha"], r["reviewed_sha"])
        others = [m.url for m in self.store.task_mrs(task.task_id) if m.repo != self._repo_name(path)]
        parts = [f"Ticket: [{task.task_id}]({self.tracker.issue_url(task.task_id)}) — {task.summary}"]
        if outcome:
            parts += ["", "## What was done", unattributed(outcome).strip()]
        if commits:
            parts += ["", "## Commits", *(f"- {c}" for c in commits)]
        if shots:
            parts += ["", media_section(shots)]
        parts += ["", "## Checks",
                  f"Commit `{r['reviewed_sha'][:12]}` passed review. Arceus pushed the branch and opened this MR; "
                  "merging is up to the team."]
        if others:
            parts += ["", "## Other MRs of this task", *(f"- {u}" for u in others)]
        return "\n".join(parts)
