"""What the background agents share: a run queued by a command or a schedule that holds the shared workspace, reads
the code at each repository's default branch, and ends in a report of its own. The test runs of the platform (qa.py)
and the architecture reviews (architect.py) are this with their own brief, report and outcomes.

- **What starts a run.** `arceus-runner <kind> run` on the server, or the kind's `schedule` (days and a time of
  `schedule.timezone`). Both queue the same run; only `source` tells them apart. A scheduled time is queued once
  (`slot`), also across restarts; a time missed by more than SLOT_GRACE (the runner was down) is not made up.
  Nothing comes from the issue tracker: no ticket, no comment, nothing posted there.
- **What it is told.** The operator's instructions file (its HTML comments are left out), read when the run starts
  and kept with the run, so a later edit never changes what a run was asked. The kind adds how to work and how to
  report (`_compose`).
- **A task of its own.** The run is the engine task `<kind>-<n>`, started in Bulava like a code review. It holds the
  shared workspace from the switch to the end (db.RUN_HOLDS): tickets wait, and a queued run goes before new
  tickets. Every repository of the group is put on a local branch `arceus-<kind>/<n>` at its default branch as last
  fetched; a repository with uncommitted work is left as it is and the agent is told which commit to read there
  with `git show`. The repositories are not writable for it. Then the runner stashes what the run left, checks
  every repository out where it was and frees the workspace. A crash at any point resumes from the stored state.
- **What comes back.** The agent writes its findings into `.arceus/<kind>/<kind>-<n>/` in the workspace. The kind
  reads them defensively, and the run's report goes into its folder under the kind's `reports`. The folder in the
  workspace is removed once the report is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from arceus_runner.code_review import ReviewAgent
from arceus_runner.db import RUN_UNDER_WAY, AgentRun, ReviewStore, Task
from arceus_runner.delivery import ProjectNotesError, project_notes
from arceus_runner.engine import EngineError
from arceus_runner.gitops import HOME_BRANCH, GitError
from arceus_runner.http import ServiceError
from arceus_runner.replies import unattributed
from arceus_runner.screening import trusted

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SLOT_GRACE = timedelta(hours=1)   # a scheduled time is queued within this long after it, never later
MAX_INSTRUCTIONS = 64 * 1024  # without comments; the brief is one argv string, and Linux caps one at 128 KB


def clean_text(value: object, limit: int) -> str:
    """Text the agent wrote, as a report may show it: plain, no model labels, bounded. Escaped when rendered."""
    if not isinstance(value, str):
        return ""
    text = unattributed(value).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def read_instructions(path: Path, required: bool = True) -> tuple[str | None, str]:
    """The operator's file as the agent gets it, or None and why it cannot be. HTML comments (the examples' guidance)
    are left out, so an example as setup installs it is empty and never sends the agent after its placeholders.
    Without `required`, a missing or empty file is no instructions (""), not a reason to stop."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return (None, f"the instructions file {path} does not exist") if required else ("", "")
    except OSError as e:
        return None, f"the instructions file {path} cannot be read ({e})"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, f"the instructions file {path} is not UTF-8 text"
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()
    if not text:
        return (None, f"the instructions file {path} is empty (comments only)") if required else ("", "")
    if len(text.encode()) > MAX_INSTRUCTIONS:
        return None, f"the instructions file {path} is larger than {MAX_INSTRUCTIONS // 1024} KB without comments"
    return text + "\n", ""


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class WorkspaceRun:
    """One kind of background agent. A kind sets the class attributes and the hooks below `-- the kind's own --`."""

    KIND = ""        # the runs' ids, branches, folders and events: <KIND>-<n>, arceus-<KIND>/<n>, <KIND>_finished
    TITLE = ""       # "Platform testing, run 3"
    NOUN = ""        # "the test run qa-3"
    RESUME = ""      # what the agent is told when its session was interrupted

    def __init__(self, delivery, settings):
        self.d = delivery
        self.cfg = delivery.cfg
        self.c = settings  # the kind's section of config.yaml
        self.store: ReviewStore = delivery.store
        self.events = delivery.events
        self._waiting: dict[str, tuple] = {}
        self._schedule_told: set[str] = set()

    @property
    def stopped(self) -> str:
        return f"stopped by `arceus-runner {self.KIND} stop`"

    @property
    def enabled(self) -> bool:
        return self.c.enabled

    @property
    def git(self):
        return self.d.git

    def emit(self, event: str, **fields) -> None:
        self.events.emit(f"{self.KIND}_{event}", **fields)

    # -- the workspace ----------------------------------------------------------------------------

    def holding(self) -> Task | None:
        """The run that holds the shared workspace now, as a task record for what counts holders (cleanup, the
        loop): its id and its state, nothing of a ticket."""
        run = self.store.run_holding(self.KIND)
        if run is None:
            return None
        return Task(task_id=run.id, issue_db_id="", state="dispatched", summary=f"{self.TITLE}, run {run.n}",
                    tag_author=run.requested_by, repo="", repo_path=str(self.cfg.paths.workspace),
                    branch=self._branch(run), base_branch=HOME_BRANCH, question_id=None, question_at_ms=None,
                    reason=None, mr_urls="[]", attempt_started=run.started_at, extra_repos="[]")

    def waiting(self) -> bool:
        """A queued run waits for the workspace: it goes before new tickets."""
        return any(not r.stop_requested for r in self.store.runs(self.KIND, "pending"))

    def _branch(self, run: AgentRun) -> str:
        return f"arceus-{self.KIND}/{run.n}"

    def out_dir(self, run: AgentRun) -> Path:
        """Where the agent writes what it found: the workspace's own folder, outside every repository."""
        return self.cfg.paths.workspace.resolve() / ".arceus" / self.KIND / run.id

    def run_dir(self, run: AgentRun) -> Path:
        return self.c.reports / str(run.n)

    def _extra_dirs(self, run: AgentRun) -> Path:
        return self.cfg.paths.db.parent / "tasks" / run.id / "extra-dirs.txt"

    def report_url(self, run: AgentRun) -> str:
        return f"http://localhost:{self.c.port}/{run.n}/"

    def _note_waiting(self, run: AgentRun, reason: str, **fields) -> None:
        seen = (reason, *sorted((k, str(v)) for k, v in fields.items()))
        if self._waiting.get(run.id) != seen:
            self._waiting[run.id] = seen
            self.emit("waits", task_id=run.id, run=run.n, reason=reason, **fields)

    # -- the schedule ---------------------------------------------------------------------------------

    def queue_scheduled(self) -> None:
        """The scheduled time of today, once it has come and for SLOT_GRACE after: a run, unless one of this kind is
        queued or under way (it would look at the same thing twice in a row)."""
        s = self.c.schedule
        if not self.enabled or s is None:
            return
        now = self.d.now().astimezone(self.cfg.schedule.tz)
        if DAYS[now.weekday()] not in s.days:
            return
        slot = now.replace(hour=s.at.hour, minute=s.at.minute, second=0, microsecond=0)
        if not slot <= now < slot + SLOT_GRACE:
            return
        key = slot.isoformat()
        busy = self.store.runs(self.KIND, *RUN_UNDER_WAY)
        if busy:
            if key not in self._schedule_told:
                self._schedule_told.add(key)
                self.emit("schedule_skipped", slot=key, busy=busy[0].id)
            return
        run = self.store.add_run(self.KIND, "schedule", "schedule", slot=key)
        if run:
            self.emit("queued", task_id=run.id, run=run.n, source="schedule", slot=key)

    # -- starting one (the local poll) ------------------------------------------------------------

    def start(self, can_take: bool) -> None:
        """The oldest queued run, when the workspace is free: prepared and dispatched."""
        for run in self.store.runs(self.KIND, "pending"):
            if run.stop_requested or not self.enabled:
                self._close(run, "stopped", self.stopped if run.stop_requested
                            else f"{self.KIND} was switched off before it started")
                continue
            if not can_take:
                return
            holder = self.d.workspace_taken()
            if holder is not None:
                self._note_waiting(run, "workspace_taken", holder=holder.task_id)
                return
            try:
                self._prepare(run)
            except (ServiceError, EngineError, GitError, OSError) as e:
                self.emit("step_failed", task_id=run.id, run=run.n, step="prepare", reason=str(e))
            return  # one at a time

    def _prepare(self, run: AgentRun) -> None:
        text, why = self._instructions()
        if text is None:
            self._close(run, "unfinished", why)
            return
        ws = self.cfg.paths.workspace
        branch = self._branch(run)
        group = self.d._group()
        repos = []
        for repo in group:
            path = ws / repo.relative_path
            self.d.ensure_repo(repo, path)
            default = repo.default_branch or "main"
            sha = self.git.ref_sha(path, f"origin/{default}")
            current = self.git.current_branch(path)
            # A repository with uncommitted work (a ticket waiting on people) is not switched: that work stays where
            # it is, and the agent reads the default branch there with git show.
            switch = bool(sha) and (current == branch or self.git.is_clean(path))
            repos.append({"repo": repo.relative_path, "default": default, "sha": sha or "", "switch": switch,
                          "http_url": repo.http_url, "prev": current if current != branch else None,
                          "prev_head": self.git.head(path) if switch else ""})
        home = ws
        if (home / ".git").exists() and self.git.current_branch(home) != branch and not self.git.is_clean(home):
            self._note_waiting(run, "uncommitted_changes", repos=["."])
            return
        extra, nothing = self._scope(run, repos)
        if nothing:
            # Nothing for the agent to look at: the run is reported as such, without the engine or a branch.
            self._close(run, nothing[0], nothing[1], extra)
            return
        folder = self.run_dir(run)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "instructions.md").write_text(text, encoding="utf-8")
        snapshot = {"branch": branch, "repos": repos, **extra, "text": self._compose(run, text, repos, extra),
                    "home_prev": self.git.current_branch(home) if (home / ".git").exists() else None,
                    "instructions": {"path": str(self.c.instructions), "bytes": len(text.encode()),
                                     "sha256": hashlib.sha256(text.encode()).hexdigest()}}
        run = self.store.update_run(run.id, state="preparing", snapshot=json.dumps(snapshot),
                                    started_at=self.d.now().isoformat())
        self._waiting.pop(run.id, None)
        self.emit("preparing", task_id=run.id, run=run.n, repos=len(repos),
                  not_switched=[r["repo"] for r in repos if not r["switch"]] or None)
        self._dispatch(run)

    def _dispatch(self, run: AgentRun) -> None:
        """Switch the workspace to the code at the default branches and hand the run to the engine. Safe to repeat."""
        snap = json.loads(run.snapshot)
        ws = self.cfg.paths.workspace
        branch = snap["branch"]
        try:
            notes = project_notes(self.cfg.paths.project_notes)
        except ProjectNotesError as e:
            self._settled(run, None, f"the project's notes for the agent cannot be given to it ({e})")
            return
        self.d.prepare_home(self.d._group(), notes)
        for r in snap["repos"]:
            if r["switch"]:
                self.git.start_branch(ws / r["repo"], branch, r["sha"])
        extra = self._extra_dirs(run)
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("".join(f"{ws / r['repo']}\n" for r in snap["repos"]))
        # Read the code there, never edit it: no writable-directories.txt beside the list.
        (extra.parent / "writable-directories.txt").unlink(missing_ok=True)
        self.out_dir(run).mkdir(parents=True, exist_ok=True)
        reply = self.d.engine.dispatch(task_id=run.id, message_id=f"{self.KIND}-{run.id}", author=run.requested_by,
                                       source="other", branch=branch, base=HOME_BRANCH, repo=Path(ws),
                                       text=snap["text"], clearance=trusted(snap["text"]), extra_dirs=extra)
        if reply.accepted:
            self.store.update_run(run.id, state="running", started_at=self.d.now().isoformat())
            self.emit("dispatched", task_id=run.id, run=run.n, source=run.source, by=run.requested_by,
                      tier=reply.tier, **self.cfg.bulava.model_profile())
        elif reply.rc == 6 and not self._overdue(run):
            self.emit("busy", task_id=run.id, run=run.n, reason=reply.why())
        else:
            self._settled(run, None, f"the execution engine did not accept the run ({reply.why()})")

    def _overdue(self, run: AgentRun) -> bool:
        started = datetime.fromisoformat(run.started_at) if run.started_at else self.d.now()
        return self.d.now() - started > self.c.timeout

    # -- following one ------------------------------------------------------------------------------

    def reconcile(self) -> None:
        """Every run past its start, each where it stands: dispatched, followed, reviewed, put back, published."""
        for run in self.store.runs(self.KIND, "preparing", "running", "reviewing", "restoring", "publishing"):
            try:
                if run.state == "preparing":
                    if run.stop_requested:
                        self._settled(run, None, self.stopped)
                    elif self.d.engine.show(run.id) is not None:  # dispatched before a crash, never recorded
                        self.store.update_run(run.id, state="running")
                    else:
                        self._dispatch(run)
                elif run.state == "running":
                    self._follow(run)
                elif run.state == "reviewing":
                    self._review(run)
                elif run.state == "restoring":
                    self._restore(run)
                else:
                    self._publish(run)
            except (ServiceError, EngineError, GitError, OSError) as e:
                self.emit("step_failed", task_id=run.id, run=run.n, step=run.state, reason=str(e))

    def _follow(self, run: AgentRun) -> None:
        show = self.d.engine.show(run.id)
        if show is None:
            self._settled(run, None, "the execution engine lost track of the run")
            return
        state = show.get("state")
        if state in ("queued", "running"):
            if run.stop_requested:
                self.d.engine.stop(Path(self.cfg.paths.workspace))
                self._settled(run, show, self.stopped)
                return
            if state == "queued" or show.get("attempt_live"):
                if self._overdue(run):
                    self.d.engine.stop(Path(self.cfg.paths.workspace))
                    hours = self.c.timeout.total_seconds() / 3600
                    self._settled(run, show, f"the run did not finish in {hours:g} h and was stopped")
                return
            attempt = len(show.get("attempts") or [])
            reply = self.d.engine.send(task_id=run.id, message_id=f"resume-{run.id}-{attempt}", source="other",
                                       author=run.requested_by, repo=Path(self.cfg.paths.workspace), text=self.RESUME,
                                       clearance=trusted(self.RESUME), extra_dirs=self._extra_dirs(run))
            if not reply.accepted and reply.rc != 6:
                self._settled(run, show, f"the execution engine did not take the run up again ({reply.why()})")
            return
        self._settled(run, show)

    def _settled(self, run: AgentRun, show: dict | None, problem: str | None = None) -> None:
        """The run is over (or never took place): keep what it found, then review it or put the workspace back."""
        found, problems = self._read(run)
        fatal = [problem] if problem else []
        if show and not problem:
            if show.get("state") in ("needs_input", "blocked"):
                fatal.append("the agent stopped to ask instead of finishing")
            elif not self._finished(show):
                fatal.append(f"the run did not finish as it should ({show.get('state')}"
                             + (f", {show['state_reason']}" if show.get("state_reason") else "") + ")")
        stopped = problem == self.stopped
        outcome, doubts = self._outcome(run, found, problems, [] if stopped else fatal, stopped)
        result = {**found, **(self._engine_notes(show) if show and not problem else {}), "problems": problems,
                  "fatal": fatal, "doubts": doubts, "engine": (show or {}).get("state"),
                  "settled_at": self.d.now().isoformat()}
        why = [*fatal, *doubts]
        reviewing = self._wants_review(run, outcome, result)
        run = self.store.update_run(run.id, state="reviewing" if reviewing else "restoring",
                                    result=json.dumps(result), outcome=outcome, reason=why[0] if why else None)
        self.emit("finished", task_id=run.id, run=run.n, outcome=outcome, engine_state=result["engine"],
                  findings=len(found.get("findings") or []), problems=[*why, *problems] or None)
        if reviewing:
            self._review(run)
        else:
            self._restore(run)

    def _restore(self, run: AgentRun) -> None:
        """Every repository back on the branch it was on, what the run left stashed, the run's branch gone.
        Each step looks at where things are first, so a crash halfway is finished on the next tick."""
        snap = json.loads(run.snapshot)
        ws = self.cfg.paths.workspace
        branch = snap["branch"]
        for r in snap["repos"]:
            path = ws / r["repo"]
            if not r["switch"] or not (path / ".git").exists():
                continue
            if self.git.current_branch(path) == branch:
                if not self.git.is_clean(path):
                    stash = self.git.stash_all(path, f"arceus: what {self.NOUN} {run.id} left")
                    self.emit("leftovers_stashed", task_id=run.id, run=run.n, repo=r["repo"], stash=stash)
                back = r.get("prev")
                self.git.switch(path, back if back and self.git.branch_exists(path, back) else r["prev_head"])
            sha = self.git.branch_sha(path, branch)
            if sha:
                if sha != r["sha"]:
                    self.emit("committed", task_id=run.id, run=run.n, repo=r["repo"], sha=sha)
                self.git.delete_branch_at(path, branch, sha)
        if (ws / ".git").exists():
            if self.git.current_branch(ws) == branch:
                back = snap.get("home_prev")
                self.git.home_on(ws, back if back and back != branch and self.git.branch_exists(ws, back)
                                 else HOME_BRANCH)
            sha = self.git.branch_sha(ws, branch)
            if sha:
                self.git.delete_branch_at(ws, branch, sha)
        run = self.store.update_run(run.id, state="publishing")
        self.emit("workspace_restored", task_id=run.id, run=run.n)
        self._publish(run)

    def _close(self, run: AgentRun, outcome: str, why: str, extra: dict | None = None) -> None:
        """A run that never reached the agent: it still gets its report, saying why."""
        result = {**self._empty(), "problems": [], "fatal": [] if outcome in self.QUIET else [why], "engine": None,
                  "settled_at": self.d.now().isoformat()}
        fields = {"snapshot": json.dumps(extra)} if extra else {}
        run = self.store.update_run(run.id, state="publishing", result=json.dumps(result), outcome=outcome,
                                    reason=why, **fields)
        self.emit("finished", task_id=run.id, run=run.n, outcome=outcome, engine_state=None, findings=0,
                  problems=None if outcome in self.QUIET else [why])
        self._publish(run)

    # -- the report ---------------------------------------------------------------------------------

    def _publish(self, run: AgentRun) -> None:
        """The run's report and the list of runs, written again from what is stored (safe to repeat); then the
        agent's folder in the workspace goes, and the run is done."""
        folder = self.run_dir(run)
        folder.mkdir(parents=True, exist_ok=True)
        finished = run.finished_at or self.d.now().isoformat()
        self._published(run)
        run = self.store.run(run.id)
        view = self.view(run, finished)
        write_atomic(folder / "result.json", json.dumps(view, ensure_ascii=False, indent=1))
        write_atomic(folder / "report.html", self._render(view))
        out = self.out_dir(run)
        if out.is_dir() and not out.is_symlink():
            shutil.rmtree(out)
        run = self.store.update_run(run.id, state="done", finished_at=finished)
        self.write_index()
        result = json.loads(run.result or "{}")
        self.emit("published", task_id=run.id, run=run.n, outcome=run.outcome,
                  findings=len(result.get("findings") or []), url=self.report_url(run))

    def view(self, run: AgentRun, finished: str | None) -> dict:
        """Everything a report shows of a run."""
        snap = json.loads(run.snapshot or "{}")
        result = json.loads(run.result or "{}")
        return {"n": run.n, "id": run.id, "source": run.source, "requested_by": run.requested_by, "slot": run.slot,
                "outcome": run.outcome, "reason": run.reason, "created_at": run.created_at,
                "started_at": run.started_at, "finished_at": finished,
                "instructions": snap.get("instructions"),
                "repos": [{"repo": r["repo"], "default": r["default"], "sha": r["sha"], "switch": r["switch"]}
                          for r in snap.get("repos") or []],
                **{k: result.get(k) for k in ("problems", "fatal", "doubts")}}

    def write_index(self) -> None:
        """The list as a file beside the runs, for a copy of the folder; the server builds it fresh on every visit."""
        self.c.reports.mkdir(parents=True, exist_ok=True)
        write_atomic(self.c.reports / "index.html", self._render_index())

    # -- the kind's own -----------------------------------------------------------------------------

    QUIET: tuple[str, ...] = ()  # outcomes of a run closed before the agent that are no failure (nothing to look at)

    def _instructions(self) -> tuple[str | None, str]:
        return read_instructions(self.c.instructions)

    def _scope(self, run: AgentRun, repos: list[dict]) -> tuple[dict, tuple[str, str] | None]:
        """What the run is to look at, kept in its snapshot; or the outcome and reason of a run with nothing to
        look at, which then closes without the agent."""
        return {}, None

    def _compose(self, run: AgentRun, instructions: str, repos: list[dict], extra: dict) -> str:
        raise NotImplementedError

    def _empty(self) -> dict:
        """What a run that found nothing has in its result besides the runner's notes."""
        raise NotImplementedError

    def _read(self, run: AgentRun) -> tuple[dict, list[str]]:
        """What the agent reported, as the report may show it, and one line per thing left out."""
        raise NotImplementedError

    def _outcome(self, run: AgentRun, found: dict, problems: list[str], fatal: list[str],
                 stopped: bool) -> tuple[str, list[str]]:
        raise NotImplementedError

    def _finished(self, show: dict) -> bool:
        """Whether the engine's record shows a run that ended as it should: as a code review does, having changed
        nothing (code_review.ReviewAgent._finished_well)."""
        return ReviewAgent._finished_well(show)

    def _engine_notes(self, show: dict) -> dict:
        """What of the engine's record the kind keeps with the result."""
        return {}

    def _wants_review(self, run: AgentRun, outcome: str, result: dict) -> bool:
        """Whether the run has a step between the agent and the report (state `reviewing`, `_review`)."""
        return False

    def _review(self, run: AgentRun) -> None:
        self.store.update_run(run.id, state="restoring")
        self._restore(self.store.run(run.id))

    def _published(self, run: AgentRun) -> None:
        """What the kind records once a run is over, before its report is written. Safe to repeat."""

    def _render(self, view: dict) -> str:
        raise NotImplementedError

    def _render_index(self) -> str:
        raise NotImplementedError
