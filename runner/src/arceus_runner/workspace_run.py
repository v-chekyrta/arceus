"""What the background agents share: a run queued by a command or a schedule that holds the shared workspace, reads
the code at each repository's default branch, and ends in a report of its own. The test runs of the platform (qa.py)
and the architecture reviews (architect.py) are this with their own brief, report and outcomes.

- **What starts a run.** `arceus-runner <kind> run` on the server or the console, or a schedule (days and a time of
  `schedule.timezone`): the kind's `schedule`, or for test runs each scenario's own (`_schedules`). Both queue the same
  run; only `source` tells them apart. A scheduled time is queued once per scenario (`slot`), also across restarts; a
  time missed by more than SLOT_GRACE (the runner was down) is not made up.
  Nothing comes from the issue tracker: no ticket, no comment, nothing posted there.
- **What it is told.** The operator's instructions file, a test scenario's own for a test run (its HTML comments are
  left out), read when the run starts
  and kept with the run, so a later edit never changes what a run was asked. The kind adds how to work and how to
  report (`_compose`).
- **A task of its own.** The run is the engine task `<kind>-<n>`, started in Bulava like a code review. It holds the
  shared workspace from the switch to the end (db.RUN_HOLDS): tickets wait, and a queued run goes before new
  tickets. Every repository of the group is put on a local branch `arceus-<kind>/<n>` at its default branch as last
  fetched; a repository with uncommitted work is left as it is and the agent is told which commit to read there
  with `git show`. The repositories are not writable for it. Then the runner stashes what the run left, checks
  every repository out where it was and frees the workspace. A crash at any point resumes from the stored state.
- **Or a folder of its own.** A kind given one (`_folder`: qa.workspace) holds nothing of the shared workspace and
  works beside a ticket, one run of the kind at a time. The folder holds `home`, the engine's project the agent runs
  in, and `repos` beside it. Each repository's default branch is fetched into a ref of the run's own
  (`refs/arceus/<kind>-<n>`) and checked out in `repos`, detached, as a repository of its own that borrows the
  clone's objects (gitops.checkout_at); nothing of the clone a ticket works in moves, and nothing the run's agent does
  in its checkout reaches it. The checkouts are outside the engine's project and not writable for the agent. The folder
  is kept in the run's snapshot (`workspace`, AgentRun.folder) before anything changes in git, so every later step of
  that run (a resume, a stop, the review, putting back) works there, whatever config.yaml says by then. Nothing is put
  back while the run's worker may still run: its tmux session is ended and checked gone first (`_worker_gone`,
  `_stop_worker`). Putting back removes the checkouts and the refs; the folder itself stays, trusted once.
- **What comes back.** The agent writes its findings into `.arceus/<kind>/<kind>-<n>/` in the folder the run works
  in. The kind reads them defensively, and the run's report goes into its folder under the kind's `reports`. The
  folder in the workspace is removed once the report is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from arceus_runner.code_review import ReviewAgent
from arceus_runner.config import Config
from arceus_runner.db import AgentRun, ReviewStore, Task
from arceus_runner.texts import ProjectNotesError, project_notes
from arceus_runner.engine import EngineError, ReportReview
from arceus_runner.gitops import HOME_BRANCH, GitError, restore_workspace
from arceus_runner.http import ServiceError
from arceus_runner.integrations import Repo
from arceus_runner.replies import unattributed
from arceus_runner.screening import MAX_CHARS, POLICY_VERSION, ScreenUnavailable, digest, mechanical_notes, trusted
from arceus_runner.workspace import AgentContext

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SLOT_GRACE = timedelta(hours=1)   # a scheduled time is queued within this long after it, never later
# Where the console shows a kind's reports (console/views_qa.py, views_arch.py, views_secops.py): its routes are at
# the root.
CONSOLE_PATHS = {"qa": "/qa/", "arch": "/architecture/", "secops": "/secops/"}
MAX_INSTRUCTIONS = 64 * 1024  # without comments; the brief is one argv string, and Linux caps one at 128 KB
INSTRUCTIONS = "instructions.md"  # in a run's folder under the kind's reports: the instructions it started with


def encodable(value):
    """Text as UTF-8 can hold it, in lists and dicts too: a lone surrogate, which JSON lets through, becomes U+FFFD.
    Text from outside is made so where it comes in, so no write of it fails later."""
    if isinstance(value, str):
        return re.sub("[\ud800-\udfff]", "\ufffd", value)
    if isinstance(value, list):
        return [encodable(v) for v in value]
    return {encodable(k): encodable(v) for k, v in value.items()} if isinstance(value, dict) else value


def clean_text(value: object, limit: int) -> str:
    """Text the agent wrote, as a report may show it: plain, encodable, no model labels, bounded. Escaped when
    rendered."""
    if not isinstance(value, str):
        return ""
    text = unattributed(encodable(value)).strip()
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


def report_address(cfg: Config, kind: str, port: int, n: int | None = None) -> tuple[str, bool]:
    """Where a kind's reports are read, and whether that is through the SSH tunnel: the console's page while the
    console is on (the runner then serves no report pages), the runner's page on localhost otherwise. No run number:
    the list of them."""
    tail = f"{n}/" if n is not None else ""
    if cfg.console.enabled:
        return cfg.console.public_url.rstrip("/") + CONSOLE_PATHS[kind] + tail, False
    return f"http://localhost:{port}/{tail}", True


def link_code(refs: list[dict], snap: dict, host) -> None:
    """Each code reference ({repo, file, line}) given the commit the run read (`sha`) and its link on the code host
    (`url`, "" when there is none)."""
    repos = {r["repo"]: r for r in snap.get("repos") or []}
    file_url = getattr(host, "file_url", None)
    for c in refs:
        r = repos.get(c["repo"])
        c["sha"] = r["sha"] if r else ""
        c["url"] = ""
        if r and r["sha"] and file_url:
            c["url"] = file_url(Repo(id=0, path=r["repo"].rsplit("/", 1)[-1], relative_path=r["repo"],
                                     default_branch=r["default"], archived=False, http_url=r["http_url"]),
                                r["sha"], c["file"], c["line"])


HOME_DIR = "home"    # in a folder of a kind's own: the engine's project, where the agent writes its evidence
REPOS_DIR = "repos"  # beside it: the checkouts it reads, outside that project
FOLDER_MARK = ".arceus-folder"  # in a folder of a kind's own (qa.workspace): the runner's, everything in it too
FOLDER_MARK_TEXT = ("The Arceus runner's folder for test runs (config qa.workspace). The runner makes and removes "
                    "checkouts here; keep nothing of your own in it.\n")


def _when(stamp: str) -> datetime:
    """An ISO time as the engine or the runner wrote it (`Z` or an offset)."""
    when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def own_git(home: Path) -> bool:
    """Whether `home` has a git folder of its own, or none yet: never a link, nor a `.git` file (`gitdir:`) that would
    send every git command run there into another repository's metadata (a ticket's)."""
    git_dir = home / ".git"
    if not git_dir.exists() and not git_dir.is_symlink():
        return True
    return git_dir.is_dir() and not git_dir.is_symlink()


def folder_problem(root: Path) -> str:
    """Why `root` cannot be a folder of the runner's own, or "": one that does not exist yet, or an empty one, can be
    made the runner's (claim_folder); one with the mark is the runner's already. Anything else (a folder with files
    and no mark, a link, a file) is someone else's: the runner removes checkouts in its folder, and never in theirs.
    Looks only, changes nothing."""
    if root.is_symlink():
        return f"qa.workspace {root} is a link; give the folder itself"
    if not root.exists():
        return ""
    if not root.is_dir():
        return f"qa.workspace {root} is not a folder"
    mark = root / FOLDER_MARK
    if (mark.is_file() and not mark.is_symlink()) or not any(root.iterdir()):
        return ""
    return (f"qa.workspace {root} holds files and is not the runner's (no {FOLDER_MARK} in it): give an empty "
            "folder, or one that does not exist yet")


def claim_folder(root: Path) -> str:
    """`root` made the runner's (folder_problem says when it cannot be), or why not."""
    why = folder_problem(root)
    if why:
        return why
    if not (root / FOLDER_MARK).is_file():
        root.mkdir(parents=True, exist_ok=True)
        write_atomic(root / FOLDER_MARK, FOLDER_MARK_TEXT)
    return ""


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
    # What the engine's Codex judges the agent's closing report by, in place of the server's rule about data changes,
    # and how many rounds it gets; at the limit the report is accepted with the reviewer's notes (`_disputed`).
    # None: the engine's own review of a report, as for a ticket.
    REPORT_CRITERIA: str | None = None
    REPORT_ROUNDS = 3
    REPORT_FILE = ""  # the agent's report in out_dir, which that review reads (review_link), part by part (_parts)
    PART_REVIEW = ("The reviewer has finished with {done}. Your report has more parts: it now reads {name}, as "
                   "`{link}`. Change nothing unless its objections ask for it, and then correct {name}. Finish again "
                   "with the outcome succeeded_research, its summary starting with `{link}`.")
    AGAIN_REVIEW = ("The reviewer has read every part of your report, but {name} changed after its review. It now "
                    "reads {name} again, as `{link}`. Change nothing unless its objections ask for it, and then "
                    "correct {name}. Finish again with the outcome succeeded_research, its summary starting with "
                    "`{link}`.")

    def __init__(self, ctx: AgentContext, settings):
        self.d = ctx
        self.cfg = ctx.cfg
        self.c = settings  # the kind's section of config.yaml
        self.store: ReviewStore = ctx.store
        self.events = ctx.events
        self._waiting: dict[str, tuple] = {}
        self._schedule_told: set[tuple[int, str]] = set()

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

    def apart(self) -> Task | None:
        """The run at work in a folder of its own now, as `holding` gives a run: it holds nothing of the shared
        workspace, but the loop and cleanup still count it as work under way (no full cleanup pass meanwhile)."""
        run = self.store.run_apart(self.KIND)
        if run is None:
            return None
        return Task(task_id=run.id, issue_db_id="", state="dispatched", summary=f"{self.TITLE}, run {run.n}",
                    tag_author=run.requested_by, repo="", repo_path=run.folder or "", branch=self._branch(run),
                    base_branch=HOME_BRANCH, question_id=None, question_at_ms=None, reason=None, mr_urls="[]",
                    attempt_started=run.started_at, extra_repos="[]")

    def waiting(self) -> bool:
        """A queued run waits for the workspace: it goes before new tickets. Not one that will work in a folder of its
        own: tickets do not wait for it."""
        if self._folder() is not None:
            return False
        return any(not r.stop_requested for r in self.store.runs(self.KIND, "pending"))

    def _folder(self) -> Path | None:
        """The folder of the kind's own its runs are prepared in from now on, or None: the shared workspace."""
        return None

    def root(self, run: AgentRun) -> Path:
        """The folder the engine runs the agent in: the shared workspace, or, for a run in a folder of its own, that
        folder's `home` (HOME_DIR), its evidence and report links in it. Its checkouts lie beside it, in `repos`
        (REPOS_DIR): outside the engine's project, where the engine lets the agent read and never edit."""
        return Path(run.folder) / HOME_DIR if run.folder else self.cfg.paths.workspace

    def _owned(self, run: AgentRun) -> bool:
        """Whether the folder a run works in is the runner's still: the very folder (no link in its place) with the
        runner's mark in it. Checked again before anything in it is written or removed."""
        root = Path(run.folder) if run.folder else None
        if root is None:
            return False
        mark = root / FOLDER_MARK
        if not (root.is_dir() and not root.is_symlink() and mark.is_file() and not mark.is_symlink()):
            return False
        # Its `home` and `repos` are folders of its own too: a link in either's place would send what is written
        # or removed there, or the engine's stop of the folder, somewhere else (a ticket's workspace).
        home = root / HOME_DIR
        chain = (home, root / REPOS_DIR, home / ".arceus", home / ".arceus" / self.KIND, self.out_dir(run),
                 home / "artifacts", home / "artifacts" / run.id)
        if not all(not p.is_symlink() and (p.is_dir() or not p.exists()) for p in chain):
            return False
        return self._home_git_ok(home)

    def _home_git_ok(self, home: Path) -> bool:
        """`home` has no git yet, or a git of its own: its `.git` a folder (no link, no `gitdir:` file) that git itself
        takes as both the repository's own and its common folder (no `commondir` sending refs, config or objects'
        bookkeeping into another repository's, a ticket's)."""
        if not own_git(home):
            return False
        git_dir = home / ".git"
        if not git_dir.exists():
            return True
        if (git_dir / "commondir").exists() or (git_dir / "gitdir").exists():
            return False
        try:
            found = self.git.git_dirs(home)
        except GitError:
            return False
        want = os.path.realpath(git_dir)
        return all(os.path.realpath(p) == want for p in found)

    def _pinned(self, run: AgentRun) -> str:
        """The ref a run in a folder of its own keeps each repository's commit in, in the clone."""
        return f"refs/arceus/{run.id}"

    def _home(self, run: AgentRun) -> Path:
        """`root` as a path to write under: for a run in a folder of its own its home as kept (the folder was resolved
        when the run was prepared), never followed through a link put there since; `_owned` says whether every
        folder on the way is still the runner's."""
        return Path(os.path.normpath(Path(run.folder) / HOME_DIR)) if run.folder else self.cfg.paths.workspace.resolve()

    def repos_dir(self, run: AgentRun) -> Path:
        """Where a run in a folder of its own has its checkouts: beside the engine's project (`root`)."""
        return Path(os.path.normpath(Path(run.folder) / REPOS_DIR)) if run.folder else self.cfg.paths.workspace

    def _checkout(self, run: AgentRun, repo: str) -> Path:
        """Where a run in a folder of its own has `repo` checked out: below its `repos`, by the path as it is written,
        never through a link. A link anywhere from the run's folder down (its `repos` too, or a group's folder) is an
        OSError: whatever it leads to is not the runner's to write in or remove."""
        top = Path(os.path.normpath(run.folder)) if run.folder else self.cfg.paths.workspace
        base = top / REPOS_DIR if run.folder else top
        dest = Path(os.path.normpath(base / repo))
        if base not in dest.parents:
            raise OSError(f"{repo} is not a folder below {base}")
        for p in [base, *reversed([q for q in dest.parents if base in q.parents]), dest]:
            if p.is_symlink():
                raise OSError(f"{p} is a link: nothing of {repo} is written or removed through it")
        return dest

    def _branch(self, run: AgentRun) -> str:
        return f"arceus-{self.KIND}/{run.n}"

    def out_dir(self, run: AgentRun) -> Path:
        """Where the agent writes what it found: the runner's own folder where the run works, outside every
        repository."""
        return self._home(run) / ".arceus" / self.KIND / run.id

    def run_dir(self, run: AgentRun) -> Path:
        return self.c.reports / str(run.n)

    def _extra_dirs(self, run: AgentRun) -> Path:
        return self.cfg.paths.db.parent / "tasks" / run.id / "extra-dirs.txt"

    def report_address(self, run: AgentRun) -> tuple[str, bool]:
        return report_address(self.cfg, self.KIND, self.c.port, run.n)

    def _note_waiting(self, run: AgentRun, reason: str, **fields) -> None:
        seen = (reason, *sorted((k, str(v)) for k, v in fields.items()))
        if self._waiting.get(run.id) != seen:
            self._waiting[run.id] = seen
            self.emit("waits", task_id=run.id, run=run.n, reason=reason, **fields)

    def _cleared(self, text: str, ref: str, kind: str, message: dict) -> str:
        """Why a text that is not a ticket's may not reach the agent, or "" when the security screen clears it.
        `message` says to the classifier where it comes from (screening.Gate). The verdict is kept per text, so the
        same text is screened once; one the screen does not clear, or cannot read now, is left out, never held: there
        is no ticket to hold it on."""
        if len(text) > MAX_CHARS:
            return f"it is longer than the security screen reads ({MAX_CHARS} characters)"
        key = digest(text)
        row = self.store.screening(key)
        if row is None:
            gate = getattr(self.d, "gate", None)
            if gate is None:
                return "no security screen runs here"
            notes, hold = mechanical_notes(text)
            try:
                verdict = gate.classify({**message, "notes": notes, "payload": text})
            except (ScreenUnavailable, ValueError):
                return "the security screen did not answer"
            decision = "review" if hold and verdict.decision == "allow" else verdict.decision
            row = self.store.save_screening(key, ref, kind, decision, list(verdict.categories), verdict.reason,
                                            verdict.model, POLICY_VERSION, self.d.now().isoformat())
            self.emit("screened", ref=ref, kind=kind, code=key[:12], verdict=decision)
        return "" if row["verdict"] == "allow" else "the security screen did not clear it"

    def _check_refs(self, snap: dict, items: list[dict], problems: list[str], name, bare=lambda f: False) -> list[dict]:
        """Every code reference (`where`) against the commit the run read: a file that is not there, or a line past
        its end, goes. An item with none left goes too, unless `bare` lets it stand without naming code. `name` says
        which item a problem is about."""
        ws = self.cfg.paths.workspace
        shas = {r["repo"]: r["sha"] for r in snap.get("repos") or []}
        kept = []
        for f in items:
            good = []
            for c in f["where"]:
                sha = shas.get(c["repo"])
                try:
                    lines = self.git.blob_lines(ws / c["repo"], sha, c["file"]) if sha else None
                except GitError:
                    lines = None
                if lines is None or (c["line"] and c["line"] > max(lines, 1)):
                    where = f"{c['repo']}/{c['file']}" + (f":{c['line']}" if c["line"] else "")
                    problems.append(f"{name(f)}: {where} is not in the code at the commit read")
                    continue
                good.append(c)
            if not good and not (bare(f) and not f["where"]):
                problems.append(f"{name(f)} is left out: " + ("none of the code it names exists at the commit read"
                                                              if f["where"] else "it names no code"))
                continue
            kept.append({**f, "where": good})
        return kept

    # -- the schedule ---------------------------------------------------------------------------------

    def queue_scheduled(self) -> None:
        """Each schedule's time of today, once it has come and for SLOT_GRACE after: a run, unless one of the same
        kind and scenario is queued or under way (it would look at the same thing twice in a row). Runs of different
        scenarios due at once are all queued, and start one after the other."""
        if not self.enabled:
            return
        now = self.d.now().astimezone(self.cfg.schedule.tz)
        for scenario, days, at in self._schedules():
            if DAYS[now.weekday()] not in days:
                continue
            slot = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
            if not slot <= now < slot + SLOT_GRACE:
                continue
            key = slot.isoformat()
            told = {"scenario": scenario} if scenario else {}
            try:
                run, busy = self.store.queue_run(self.KIND, "schedule", "schedule", slot=key, scenario=scenario)
            except LookupError:
                continue  # deleted between reading the schedules and now
            if busy is not None:
                if (scenario, key) not in self._schedule_told:
                    self._schedule_told.add((scenario, key))
                    self.emit("schedule_skipped", slot=key, busy=busy.id, **told)
            elif run:
                self.emit("queued", task_id=run.id, run=run.n, source="schedule", slot=key, **told)

    def _schedules(self) -> list[tuple[int, tuple[str, ...], time]]:
        """What queues runs by itself: the scenario (0 for a kind without them), its days and its time."""
        s = self.c.schedule
        return [(0, tuple(s.days), s.at)] if s is not None else []

    # -- starting one (the local poll) ------------------------------------------------------------

    def start(self, can_take: bool) -> None:
        """The oldest queued run, when the workspace is free: prepared and dispatched. One that will work in a folder
        of its own waits only for a run of its kind under way."""
        for run in self.store.runs(self.KIND, "pending"):
            if run.stop_requested or not self.enabled:
                self._close(run, "stopped", self.stopped if run.stop_requested
                            else f"{self.KIND} was switched off before it started")
                continue
            if not can_take:
                return
            mine = self.holding() or self.apart()  # one run of a kind at a time, wherever it works
            held = None if self._folder() is not None else self.d.workspace.taken()
            if held is not None or mine is not None:
                self._note_waiting(run, "workspace_taken" if held is not None else "run_under_way",
                                   holder=(held or mine).task_id)
                return
            try:
                self._prepare(run)
            except (ServiceError, EngineError, GitError, OSError) as e:
                self.emit("step_failed", task_id=run.id, run=run.n, step="prepare", reason=str(e))
            return  # one at a time

    def _prepare(self, run: AgentRun) -> None:
        text, why, source = self._brief_source(run)
        if text is None:
            self._close(run, "unfinished", why)
            return
        ws = self.cfg.paths.workspace
        branch = self._branch(run)
        group = self.d.workspace.group()
        # A run that began to be prepared in a folder of its own keeps it, whatever config.yaml says by now.
        own = Path(run.folder) if run.folder else self._folder()
        if own is not None:
            why = claim_folder(own)  # the path as given: a link there is refused, never followed
            if why:
                self._close(run, "unfinished", why)
                return
            own = own.resolve()
            linked = [p for p in (own / HOME_DIR, own / REPOS_DIR) if p.is_symlink() or (p.exists() and not p.is_dir())]
            if not linked and not self._home_git_ok(own / HOME_DIR):
                linked = [own / HOME_DIR / ".git"]
            if linked:
                self._close(run, "unfinished", f"{linked[0]} is a link or not a folder: the runner writes and removes "
                                               f"only in folders of its own under {own}")
                return
            if self._foreign_worker(run, own / HOME_DIR):
                return
            if not run.folder:
                # Kept before anything changes in git: whatever this run does in a clone from here on, a later pass
                # (after a crash, a stop) finds it named here.
                run = self.store.update_run(run.id, snapshot=json.dumps({"workspace": str(own), "pinned": {}}))
            repos = self._pin(run, group)
        else:
            repos = []
            for repo in group:
                path = ws / repo.relative_path
                self.d.workspace.ensure_repo(repo, path)
                default = repo.default_branch or "main"
                sha = self.git.ref_sha(path, f"origin/{default}")
                current = self.git.current_branch(path)
                # A repository with uncommitted work (a ticket waiting on people) is not switched: that work stays
                # where it is, and the agent reads the default branch there with git show.
                switch = bool(sha) and (current == branch or self.git.is_clean(path))
                repos.append({"repo": repo.relative_path, "default": default, "sha": sha or "", "switch": switch,
                              "http_url": repo.http_url, "prev": current if current != branch else None,
                              "prev_head": self.git.head(path) if switch else ""})
        home = ws
        if own is None and (home / ".git").exists() and self.git.current_branch(home) != branch \
                and not self.git.is_clean(home):
            self._note_waiting(run, "uncommitted_changes", repos=["."])
            return
        extra, nothing = self._scope(run, repos)
        if nothing:
            # Nothing for the agent to look at: the run is reported as such, without the engine or a branch.
            self._close(run, nothing[0], nothing[1], extra)
            return
        folder = self.run_dir(run)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / INSTRUCTIONS).write_text(text, encoding="utf-8")
        # The run's clock starts here and its times are fixed now: the brief names them, the runner stops the run by
        # them and the console shows them, whatever config.yaml says later.
        now = self.d.now().astimezone(timezone.utc)
        until = self._test_until(now)
        run = replace(run, started_at=now.isoformat(), deadline=(now + self.c.timeout).isoformat(),
                      test_until=until.isoformat() if until else None)
        # Every ref this run made, also of a repository that left the group between two tries: put back by them all.
        pinned = json.loads(self.store.run(run.id).snapshot or "{}").get("pinned") if own is not None else None
        snapshot = {"branch": branch, "repos": repos, **extra, "text": self._compose(run, text, repos, extra),
                    **({"workspace": str(own), "pinned": pinned or {}} if own is not None else {}),
                    "home_prev": self.git.current_branch(home) if own is None and (home / ".git").exists() else None,
                    "instructions": {"path": str(source), "bytes": len(text.encode()),
                                     "sha256": hashlib.sha256(text.encode()).hexdigest()}}
        run = self.store.update_run(run.id, state="preparing", snapshot=json.dumps(snapshot),
                                    started_at=run.started_at, deadline=run.deadline, test_until=run.test_until)
        self._waiting.pop(run.id, None)
        self.emit("preparing", task_id=run.id, run=run.n, repos=len(repos),
                  not_switched=[r["repo"] for r in repos if not r["switch"]] or None,
                  **({"folder": str(own)} if own is not None else {}))
        self._dispatch(run)

    def _pin(self, run: AgentRun, group: list) -> list[dict]:
        """For a run in a folder of its own: each repository's default branch fetched into the run's ref in the clone,
        as the snapshot keeps it. The clone may be a ticket's at work: only that ref changes there; a repository not
        cloned yet is cloned as a ticket would clone it. Each ref is named in the run's snapshot (`pinned`) before it
        is made and its commit after, so a pass after a crash goes on with the commits already chosen, and a run
        stopped before it starts takes away every ref it made (`_unpin`)."""
        ws = self.cfg.paths.workspace
        ref = self._pinned(run)
        snap = json.loads(run.snapshot)
        pinned: dict[str, str | None] = dict(snap.get("pinned") or {})
        repos: list[dict] = []
        for repo in group:
            name = repo.relative_path
            path = ws / name
            default = repo.default_branch or "main"
            if not (path / ".git").exists():
                self.d.workspace.ensure_repo(repo, path)
            # Whatever is left at the checkout's place (a run before that never got put back) goes first.
            self.git.drop_checkout(path, self._checkout(run, name))
            sha = pinned.get(name)
            if sha and self.git.ref_sha(path, ref) != sha:
                if self.git.has_commit(path, sha):
                    self.git.set_ref(path, ref, sha)  # the commit chosen before the crash, not today's
                else:
                    self.emit("pin_lost", task_id=run.id, run=run.n, repo=name, sha=sha)
                    sha = None
            if sha is None and name in pinned:
                # Named, and maybe made, before a crash kept its commit: a ref of this run's own name is this run's,
                # and its commit is the one chosen, whatever the code host's branch says by now.
                sha = self.git.ref_sha(path, ref)
                if sha:
                    pinned[name] = sha
                    run = self.store.update_run(run.id, snapshot=json.dumps({**snap, "pinned": pinned}))
            if sha is None:
                pinned[name] = None  # named before the fetch makes it
                run = self.store.update_run(run.id, snapshot=json.dumps({**snap, "pinned": pinned}))
                sha = self.git.fetch_pinned(path, default, ref) or ""
                pinned[name] = sha
                run = self.store.update_run(run.id, snapshot=json.dumps({**snap, "pinned": pinned}))
            repos.append({"repo": name, "default": default, "sha": sha, "switch": bool(sha),
                          "http_url": repo.http_url, "prev": None, "prev_head": ""})
        return repos

    def _unpin(self, run: AgentRun) -> bool:
        """The refs a run that never started made in the clones, gone. False when git refused: the next pass tries
        again, and the run is not closed before."""
        pinned = json.loads(run.snapshot or "{}").get("pinned") or {}
        try:
            for name in pinned:
                self.git.drop_ref(self.cfg.paths.workspace / name, self._pinned(run))
        except (GitError, OSError) as e:
            self.emit("step_failed", task_id=run.id, run=run.n, step="unpin", reason=str(e))
            return False
        return True

    def _dispatch(self, run: AgentRun) -> None:
        """Switch the workspace to the code at the default branches and hand the run to the engine. Safe to repeat."""
        snap = json.loads(run.snapshot)
        ws = self.cfg.paths.workspace
        root = self.root(run)
        branch = snap["branch"]
        if run.folder and self._foreign_worker(run, root):
            return
        if self._overdue(run):
            # Its time ran out while the engine was busy (or the runner was down): it never starts late.
            hours = self._given(run).total_seconds() / 3600
            self._settled(run, None, f"the run could not start in its {hours:g} h: the engine stayed busy")
            return
        try:
            notes = project_notes(self.cfg.paths.project_notes)
        except ProjectNotesError as e:
            self._settled(run, None, f"the project's notes for the agent cannot be given to it ({e})")
            return
        if run.folder:
            if not self._owned(run):
                self._settled(run, None, f"{run.folder} is no longer the runner's folder (its {FOLDER_MARK} is gone, or "
                                         f"a link stands in its place or in its {HOME_DIR} or {REPOS_DIR})")
                return
            self.d.workspace.prepare_home(self.d.workspace.group(), notes, home=root, reading=True)
            where = {}
            for r in snap["repos"]:
                where[r["repo"]] = dest = self._checkout(run, r["repo"])
                if r["switch"]:
                    self.git.checkout_at(ws / r["repo"], dest, r["sha"])
                    self.d.workspace.prepare_checkout(root, dest)
            listed = [where[r["repo"]] for r in snap["repos"] if r["switch"]]
        else:
            self.d.workspace.prepare_home(self.d.workspace.group(), notes)
            for r in snap["repos"]:
                if r["switch"]:
                    self.git.start_branch(ws / r["repo"], branch, r["sha"])
            listed = [ws / r["repo"] for r in snap["repos"]]
        extra = self._extra_dirs(run)
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("".join(f"{p}\n" for p in listed))
        # Read the code there, never edit it: no writable-directories.txt beside the list.
        (extra.parent / "writable-directories.txt").unlink(missing_ok=True)
        self.out_dir(run).mkdir(parents=True, exist_ok=True)
        report_review = self._report_review(run)
        if run.folder:
            # The worker's session as the engine will name it, kept before the engine is asked: a crash between its
            # answer and the runner's record of it leaves a run that says which worker to wait for.
            snap = json.loads(self.store.run(run.id).snapshot or "{}")
            if not snap.get("session"):
                snap["session"] = self.d.engine.folder_session(Path(root))
                run = self.store.update_run(run.id, snapshot=json.dumps(snap))
        before = None
        if run.folder:
            # Which start of the folder's session is there before this run asks, if any (tmux's own time it was
            # made): one the engine starts for this run is another. Asked of tmux before the last look at the clock.
            before = self.d.engine.session_created(json.loads(self.store.run(run.id).snapshot)["session"])
        if self._overdue(run):
            # Last, right before the engine is asked: getting the checkouts ready and asking tmux take time, and a
            # run never starts late. It has not asked: a worker in the folder now is another's, and nothing is put
            # back while it is there.
            if run.folder and self._foreign_worker(run, root):
                return
            hours = self._given(run).total_seconds() / 3600
            self._settled(run, None, f"the run could not start in its {hours:g} h: the engine stayed busy")
            return
        if run.folder:
            # Kept before the engine is asked: only a run that asked, and was not told `busy`, may take the folder's
            # session for its own after a crash (reconcile); `session_before` is the start of it that was not.
            run = self._set_snapshot(run, asked=True, asked_at=self.d.now().astimezone(timezone.utc).isoformat(),
                                     session_before=before)
        reply = self.d.engine.dispatch(task_id=run.id, message_id=f"{self.KIND}-{run.id}", author=run.requested_by,
                                       source="other", branch=branch, base=HOME_BRANCH, repo=Path(root),
                                       text=snap["text"], clearance=trusted(snap["text"]), extra_dirs=extra,
                                       report_review=report_review)
        if reply.accepted:
            self.store.update_run(run.id, state="running")
            self.emit("dispatched", task_id=run.id, run=run.n, source=run.source, by=run.requested_by,
                      tier=reply.tier, **self.cfg.bulava.model_profile())
        elif reply.rc == 6:
            # Busy: the engine started nothing for this run. The folder's session, if any, is another task's: it is
            # never stopped nor taken for this run's.
            if run.folder:
                run = self._set_snapshot(run, asked=False)
            if self._overdue(run):
                # Out of time, but the folder may be another worker's now: nothing is put back while it is there.
                # A later pass (reconcile, `_dispatch`) waits for it, then closes the run without asking again.
                if run.folder and self._foreign_worker(run, root):
                    return
                hours = self._given(run).total_seconds() / 3600
                self._settled(run, None, f"the run could not start in its {hours:g} h: the engine stayed busy")
            else:
                self.emit("busy", task_id=run.id, run=run.n, reason=reply.why())
        else:
            why = f"the execution engine did not accept the run ({reply.why()})"
            if run.folder:
                # The engine may have started the folder's session before it refused. The stop is kept, and the run
                # followed as running, in one write before the engine is asked anything more: whatever happens next
                # (tmux not answering, a crash), a later pass goes on with this stop and never dispatches again.
                snap = {**json.loads(run.snapshot or "{}"), "stopping": {"why": why}}
                run = self.store.update_run(run.id, state="running", snapshot=json.dumps(snap))
                if self._stop_worker(run, why):
                    self._settled(run, None, why)
                return
            self._settled(run, None, why)

    def _report_review(self, run: AgentRun) -> ReportReview | None:
        """The kind's review budget for the engine (REPORT_CRITERIA). The criteria go to a file beside the extra
        directories, outside the workspace (a file in it would make the tree dirty); the engine copies it when it
        accepts the task, and a later attempt is reviewed by that copy."""
        if not self.REPORT_CRITERIA:
            return None
        path = self._extra_dirs(run).parent / "report-criteria.md"
        write_atomic(path, self.REPORT_CRITERIA)
        self._link_report(run)
        return ReportReview(rounds=self.REPORT_ROUNDS, criteria=path)

    def review_link(self, run: AgentRun, k: int = 1) -> Path:
        """The name under which the engine's review reads part k of the agent's report. It reads a report the worker
        names in its summary, or else the newest one, in `artifacts/` of the folder the session starts in, and only
        as html, md or txt (engine/bin/pipeline-tool.py find_report, report_text); the agent's report is JSON in its
        own folder. A link there, to that file, makes every round read the part as it is then."""
        return self._home(run) / "artifacts" / run.id / (Path(self._part_file(k)).stem + ".txt")

    def _part_file(self, k: int) -> str:
        """The agent's file of part k of its report. One part unless the kind says otherwise."""
        return self.REPORT_FILE

    def _parts(self, run: AgentRun) -> list[str]:
        """The parts of the report the agent wrote, in order; the engine's review reads one per attempt."""
        return [self.REPORT_FILE] if self.REPORT_FILE else []

    def _link_report(self, run: AgentRun, k: int = 1) -> None:
        """Make review_link of part k, relative so that it holds wherever the workspace is. Safe to repeat. Never
        through an `artifacts` that is itself a link: whatever it points at is not the workspace's to write in."""
        link = self.review_link(run, k)
        ws = self._home(run)
        art = ws / "artifacts"
        if art.is_symlink() or (art / run.id).is_symlink():
            self.emit("review_link_skipped", task_id=run.id, run=run.n, reason=f"{art} is a link")
            return
        link.parent.mkdir(parents=True, exist_ok=True)
        target = os.path.relpath(self.out_dir(run) / self._part_file(k), link.parent)
        if link.is_symlink() and os.readlink(link) == target:
            return
        tmp = link.with_name(f".{link.name}.tmp")
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(target)
        os.replace(tmp, link)

    def _overdue(self, run: AgentRun) -> bool:
        """Past the time the run was given when it was prepared. A run prepared before runs kept their deadline
        counts its timeout from its start."""
        if run.deadline:
            return self.d.now() > datetime.fromisoformat(run.deadline)
        started = datetime.fromisoformat(run.started_at) if run.started_at else self.d.now()
        return self.d.now() - started > self.c.timeout

    def _given(self, run: AgentRun) -> timedelta:
        """How long the run was given."""
        if run.deadline and run.started_at:
            return datetime.fromisoformat(run.deadline) - datetime.fromisoformat(run.started_at)
        return self.c.timeout

    # -- following one ------------------------------------------------------------------------------

    def reconcile(self) -> None:
        """Every run past its start, each where it stands: dispatched, followed, reviewed, put back, published."""
        for run in self.store.runs(self.KIND, "preparing", "running", "reviewing", "restoring", "publishing"):
            try:
                if run.state == "preparing":
                    # The engine first: a dispatch it took before a crash was never recorded, and a stop then is a
                    # stop of a live worker, followed as one (`_follow`), never a cleanup under it. The engine says
                    # the same of a task it has no record of and of one whose record it cannot read: then the
                    # worker's session, kept before the dispatch, says whether one runs.
                    snap = json.loads(run.snapshot or "{}")
                    if snap.get("stopping"):
                        # A stop begun (an older runner's refused dispatch left it so): seen through, never a dispatch.
                        self.store.update_run(run.id, state="running")
                    elif self.d.engine.show(run.id) is not None:
                        self.store.update_run(run.id, state="running")
                    elif snap.get("asked") and snap.get("session") and self.d.engine.session_alive(snap["session"]) \
                            and self._ours(run, snap["session"]):
                        self.store.update_run(run.id, state="running")
                    elif run.stop_requested:
                        if not (run.folder and self._foreign_worker(run, self.root(run))):
                            self._settled(run, None, self.stopped)
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
        stopping = json.loads(run.snapshot or "{}").get("stopping")
        if show is None:
            # The engine has no readable record of the run (gone, or corrupt: it says the same for both). Its worker
            # is found by its session all the same. While it runs it is left to work: it is ended only when the run
            # is stopped or out of time, and nothing is put back before it is gone.
            why = stopping["why"] if stopping else self.stopped if run.stop_requested else self._timed_out(run)
            if why:
                if self._stop_worker(run, why):
                    self._settled(run, None, why)
                return
            if run.folder and self.d.engine.session_alive(self._session(run)):
                self._note_waiting(run, "record_unreadable_worker_alive", session=self._session(run))
                return
            self._settled(run, None, "the execution engine lost track of the run")
            return
        state = show.get("state")
        live = state in ("queued", "running")
        # Every stop is seen through before anything of the run is read or sent: one begun before (`stopping`),
        # whatever the engine's record says by then (it may end the attempt while the worker's session goes on,
        # Engine.stop_worker); one asked for; the end of its time; or its folder, or a folder on the way to its
        # report, replaced (a link put in its place), when nothing more is written there or handed to the engine by
        # that path and its worker is ended by its session's name. Nothing is put back under a worker that still runs.
        why = stopping["why"] if stopping else None
        if why is None and run.folder and not self._owned(run):
            why = f"{run.folder} is no longer the runner's folder: the run was stopped"
        if why is None and run.stop_requested:  # also when the record already says the attempt ended
            why = self.stopped
        if why is None and live:
            why = self._timed_out(run)
        if why is not None:
            if self._stop_worker(run, why):
                self._settled(run, show, why)
            return
        run = self._note_progress(run)
        if self._review_pending(run):
            return
        if live:
            if state == "queued" or show.get("attempt_live"):
                return
            attempt = len(show.get("attempts") or [])
            text = self._resume_text(run)
            reply = self.d.engine.send(task_id=run.id, message_id=f"resume-{run.id}-{attempt}", source="other",
                                       author=run.requested_by, repo=Path(self.root(run)), text=text,
                                       clearance=trusted(text), extra_dirs=self._extra_dirs(run))
            if not reply.accepted and reply.rc != 6 and self._worker_gone(run):
                self._settled(run, show, f"the execution engine did not take the run up again ({reply.why()})")
            return
        if self._next_part(run, show):
            return
        # The engine writes its verdict before it lets the worker's session go (a few seconds later): the run's
        # checkouts stay until it has.
        if self._worker_gone(run):
            self._settled(run, show)

    def _foreign_worker(self, run: AgentRun, home: Path) -> bool:
        """Whether a worker that is not this run's is at work in the folder (`home`): the engine's session of that
        folder is alive and the engine does not name this run as its task (`task-id`), or this run never asked the
        engine to start, or was answered `busy`. Then nothing of
        the folder is touched, its checkouts, its home's branch and index, until it has gone: the run waits, and an
        error asking tmux is an error, never "free". True while one is there."""
        session = json.loads(run.snapshot or "{}").get("session") or self.d.engine.folder_session(Path(home))
        if not self.d.engine.session_alive(session):
            return False
        if self._ours(run, session):
            return False
        self._note_waiting(run, "folder_busy", session=session)
        return True

    def _ours(self, run: AgentRun, session: str) -> bool:
        """Whether the folder's live session (by its kept name) is the worker this run started: told by that start of
        the session, never by the clock of a verdict.
        - The run never asked the engine to start: not its.
        - The very session that was there before the run asked (tmux's time it was made, kept with the ask): not
          its. The engine starts the run's worker in a session of its own making, after it has found the folder free.
        - The engine names this run as the folder's task (`task-id`): its.
        - A start is under way in the folder (the engine's folder lock held by a live process): not its, until that
          start has named its task.
        - The engine names no task, and no start is under way: its (a start cut off between the session and that
          record is still the run's).
        - It names another task: that task's if the engine shows it at work there, or if its last attempt began
          when or after this run asked (it took the folder in the meantime: the busy answer); an earlier run's,
          left from before (also one the engine still calls running with no live attempt), and so this run's own
          start, otherwise.
        An engine or tmux that cannot say is an EngineError: nothing is done meanwhile."""
        snap = json.loads(run.snapshot or "{}")
        if not snap.get("asked"):
            return False
        created = self.d.engine.session_created(session)
        if created is not None and snap.get("session_before") is not None and created == snap["session_before"]:
            return False
        named = self.d.engine.session_task(session)
        if named == run.id:
            return True
        if self.d.engine.folder_starting(session):
            # A start is under way in the folder right now (the engine's folder lock held by a live process): the
            # session is whoever holds it, and it names its task when it is done. Never taken meanwhile.
            return False
        if named is None:
            return True  # the start that made it is over and named no task: this run's own, cut off
        other = self.d.engine.show(named)
        if other is None:
            return True  # the engine keeps no record of it: no task at work there
        if other.get("state") in ("queued", "running") and other.get("attempt_live") is True:
            return False
        began = [a.get("started_at") for a in other.get("attempts") or []]
        asked = snap.get("asked_at")
        if not asked or not began or not all(began):
            return False  # when it began is not known: never taken for this run's
        try:
            # Whole seconds on the engine's side: an attempt that began in the second this run asked, or later, is
            # taken as the one that made the engine answer busy.
            return max(_when(b) for b in began) < _when(asked).replace(microsecond=0)
        except (TypeError, ValueError):
            return False  # a time it cannot read: never taken for this run's

    def _set_snapshot(self, run: AgentRun, **fields) -> AgentRun:
        snap = {**json.loads(self.store.run(run.id).snapshot or "{}"), **fields}
        return self.store.update_run(run.id, snapshot=json.dumps(snap))

    def _session(self, run: AgentRun) -> str:
        """The run's worker's tmux session: as kept before it was dispatched, else as the engine names its folder's
        (a run in the shared workspace, or one dispatched by an older runner)."""
        kept = json.loads(run.snapshot or "{}").get("session")
        if kept:
            return kept
        if run.folder and not self._owned(run):
            raise OSError(f"{run.folder} is no longer the runner's folder and the run kept no session: its worker "
                          "cannot be told apart from another's")
        return self.d.engine.folder_session(Path(self.root(run)))

    def _timed_out(self, run: AgentRun) -> str | None:
        """Why the run is stopped for its time, or None while it has time left."""
        if not self._overdue(run):
            return None
        return f"the run did not finish in {self._given(run).total_seconds() / 3600:g} h and was stopped"

    def _worker_gone(self, run: AgentRun) -> bool:
        """For a run in a folder of its own: its worker's session ended (Engine.end_session), the attempt being over
        in the engine. True at once for a run in the shared workspace, whose next holder's dispatch takes over the
        folder's session."""
        if not run.folder:
            return True
        session = self._session(run)
        if self.d.engine.end_session(Path(self.root(run)), session):
            self.emit("worker_gone", task_id=run.id, run=run.n, session=session)
            return True
        self.emit("worker_still_running", task_id=run.id, run=run.n)
        return False

    def _stop_worker(self, run: AgentRun, why: str) -> bool:
        """Stop the run in the engine and end its worker. True once the worker's session is gone and the engine
        counts no attempt of it as live; until then nothing is put back under it, and every later pass, a restarted
        runner's too, tries again. Why the run stops is kept with it before anything is asked of the engine
        (`stopping`), so a later pass goes on with the same stop whatever the engine's record says by then."""
        snap = json.loads(run.snapshot or "{}")
        if not snap.get("stopping"):
            snap["stopping"] = {"why": why}
            run = self.store.update_run(run.id, snapshot=json.dumps(snap))
        session = self._session(run)
        if run.folder and not self._owned(run):
            # The folder was replaced or a link stands in it: its path may lead to another folder (a ticket's), so
            # the engine is not asked to stop it by path; the run's own session, by its kept name, is ended.
            gone = self.d.engine.end_session(Path(run.folder), session)
        else:
            gone = self.d.engine.stop_worker(Path(self.root(run)), session)
        show = self.d.engine.show(run.id)
        if gone and not (show and show.get("attempt_live")):
            self.emit("worker_gone", task_id=run.id, run=run.n, session=session)
            return True
        self.emit("stop_unconfirmed", task_id=run.id, run=run.n,
                  reason="its worker is still running" if not gone else "the engine still counts its attempt as live")
        return False

    # -- the closing review, part by part -----------------------------------------------------------
    #
    # The engine's review reads one file of the report per attempt, and only so much of it. A report in several parts
    # is reviewed one part per attempt: when an attempt ends `done` (its part passed, or was let through with notes)
    # and parts are left, the runner links the next one and asks the agent to finish again naming it
    # (`worker-send --relation continue`, which the engine reviews by the task's stored budget). Answering an objection
    # on a later part can change one already reviewed (its totals or its summary): once the last part is done, each
    # part that changed since its review is read once more the same way (`again`), and only a part that changed after
    # that is reported as changed after its review. The run's snapshot keeps where this stands under "review": the part
    # under review, the part read again if any, whether the message for it was sent, the parts as they were when the
    # first was reviewed, a digest of each part as its review saw it (`_review_digest`), the parts read again, and the
    # objections left on the parts done. The message is counted before it is sent and marked sent after; the engine
    # takes a message id once, so a runner that restarts in between sends it again and nothing more.

    def _review_state(self, run: AgentRun) -> dict:
        return json.loads(run.snapshot or "{}").get("review") or {}

    def _set_review_state(self, run: AgentRun, review: dict) -> AgentRun:
        snap = json.loads(run.snapshot or "{}")
        return self.store.update_run(run.id, snapshot=json.dumps({**snap, "review": review}))

    def _digest(self, run: AgentRun, name: str) -> str:
        try:
            return self._review_digest(name, (self.out_dir(run) / name).read_bytes())
        except OSError:
            return ""

    def _changed(self, run: AgentRun, name: str, digest: str) -> bool:
        """Whether a part differs from how its review saw it. A digest kept before `_review_digest` took its present
        form is the whole file's, and is matched as such."""
        try:
            data = (self.out_dir(run) / name).read_bytes()
        except OSError:
            return digest != ""
        return digest not in (self._review_digest(name, data), hashlib.sha256(data).hexdigest())

    def _review_digest(self, name: str, data: bytes) -> str:
        """What of a part a later change has to differ in to count as a change after its review: the whole file,
        unless the kind keeps out what a fix elsewhere is bound to change."""
        return hashlib.sha256(data).hexdigest()

    def _reviewing(self, rv: dict) -> int:
        """The number of the part the current attempt's review reads: the one read again, or the next in turn."""
        parts = rv.get("parts") or []
        again = rv.get("again")
        return parts.index(again) + 1 if again in parts else rv.get("part", 1)

    def _resume_text(self, run: AgentRun) -> str:
        rv = self._review_state(run)
        k = self._reviewing(rv)
        if k == 1 and not rv.get("again"):
            return self.RESUME
        link = self.review_link(run, k).relative_to(self._home(run)).as_posix()
        return f"{self.RESUME} The reviewer is reading {self._part_file(k)}: finish with your summary starting with `{link}`."

    def _next_part(self, run: AgentRun, show: dict) -> bool:
        """After an attempt whose review settled its part: on to the next part, or, after the last, to a part that
        changed since its review and has not been read again yet, while time is left. True while that is under way."""
        if not self.REPORT_FILE or run.stop_requested or show.get("state") != "done" or self._overdue(run):
            return False
        rv = self._review_state(run)
        if rv.get("refused"):
            return False
        k = rv.get("part", 1)
        parts = rv.get("parts") or self._parts(run)
        if len(parts) <= 1 or k > len(parts):
            return False
        again = rv.get("again")
        name = again or parts[k - 1]
        digests = {**(rv.get("digests") or {}), name: self._digest(run, name)}
        rechecked = [*(rv.get("rechecked") or []), *([again] if again else [])]
        if k < len(parts):
            k, again = k + 1, None
        else:
            changed = [n for n in parts if n in digests and n not in rechecked and self._changed(run, n, digests[n])]
            if not changed:
                return False  # the report is read whole: _review_notes keeps what this last attempt left
            again = changed[0]
        notes = [n for n in rv.get("notes") or [] if n["part"] != name]  # a part read again: only its latest review
        mine = self._engine_notes(show)
        if mine:
            notes.append({"part": name, "text": mine["engine_review"]})
        rv = {"part": k, "again": again, "sent": False, "parts": parts, "notes": notes, "digests": digests,
              "rechecked": rechecked}
        self._send_part(self._set_review_state(run, rv))
        return True

    def _review_pending(self, run: AgentRun) -> bool:
        """A part's message counted but not known to be sent: send it again (the engine takes it once)."""
        rv = self._review_state(run)
        if not rv or rv.get("sent", True):
            return False
        if self._overdue(run) or run.stop_requested:
            self._set_review_state(run, {**rv, "sent": True, "refused": "the run's time ran out" if not run.stop_requested
                                   else "the run was stopped"})
            return False
        self._send_part(run)
        return True

    def _send_part(self, run: AgentRun) -> None:
        rv = self._review_state(run)
        k = self._reviewing(rv)
        again = bool(rv.get("again"))
        ws = self._home(run)
        self._link_report(run, k)
        link = self.review_link(run, k).relative_to(ws).as_posix()
        if again:
            text = self.AGAIN_REVIEW.format(name=self._part_file(k), link=link)
        else:
            text = self.PART_REVIEW.format(done=", ".join(rv["parts"][:k - 1]), name=self._part_file(k), link=link)
        reply = self.d.engine.send(task_id=run.id, message_id=f"review-{run.id}-{'again-' if again else ''}{k}",
                                   source="other", author=run.requested_by, repo=Path(self.root(run)),
                                   text=text, clearance=trusted(text), extra_dirs=self._extra_dirs(run))
        if reply.accepted:
            self._set_review_state(run, {**rv, "sent": True})
            self.emit("review_part", task_id=run.id, run=run.n, part=self._part_file(k), of=len(rv["parts"]),
                      **({"again": True} if again else {}))
        elif reply.rc != 6:
            self._set_review_state(run, {**rv, "sent": True, "refused": f"the engine did not take it ({reply.why()})"})
            self.emit("review_part_refused", task_id=run.id, run=run.n, part=self._part_file(k), reason=reply.why())

    def _review_notes(self, run: AgentRun, show: dict | None, problem: str | None) -> dict:
        """What the closing review left on the report as a whole: the objections on every part, and the parts it never
        read or that changed after it did. A report in one part keeps the engine's words as they are."""
        if not self.REPORT_FILE:
            return self._engine_notes(show) if show and not problem else {}
        rv = self._review_state(run)
        k = rv.get("part", 1)
        again = rv.get("again")
        now = self._parts(run)
        parts = rv.get("parts") or now
        mine = self._engine_notes(show) if show and not problem else {}
        if len(parts) <= 1 and len(now) <= 1:
            return mine
        if rv.get("refused") or not rv.get("sent", True):
            mine = {}  # the engine's record is of the attempt before, whose objections are kept already
        # The part under review counts as read only when its attempt ended as a review ends.
        last_read = show is not None and not problem and not rv.get("refused") and rv.get("sent", True) and (
            show.get("state") == "done" or bool(mine))
        current = again or (parts[k - 1] if k <= len(parts) else self._part_file(k))
        notes = list(rv.get("notes") or [])
        digests = dict(rv.get("digests") or {})
        if again and last_read:  # read again to its end: its latest review is the one that counts
            notes = [n for n in notes if n["part"] != again]
            digests[again] = self._digest(run, again)
        if mine:
            notes.append({"part": current, "text": mine["engine_review"]})
        read = parts[:k] if last_read or again else parts[:k - 1]
        gaps = [f"{n} was not reviewed before the run ended" for n in [*parts, *[n for n in now if n not in parts]]
                if n not in read]
        if gaps and rv.get("refused"):
            gaps[0] += f" ({rv['refused']})"
        gaps += [f"{n} changed after its review" + (f" and was not reviewed again ({rv['refused']})"
                                                    if n == again and rv.get("refused") else "")
                 for n in read if n in digests and self._changed(run, n, digests[n])]
        if not notes and not gaps:
            return {}
        text = "\n\n".join([*(f"{n['part']}:\n{n['text'] or 'The review did not say what it objected to.'}"
                               for n in notes), *gaps])
        return {"engine_disputed": True, "engine_review": text}

    def _settled(self, run: AgentRun, show: dict | None, problem: str | None = None) -> None:
        """The run is over (or never took place): keep what it found, then review it or put the workspace back."""
        if run.folder and not self._owned(run):
            # Read through a link put in the folder's place, the "report" could be anything: none is read.
            found, problems = self._empty(), [f"{run.folder} is no longer the runner's folder: its report was not read"]
        else:
            found, problems = self._read(run)
            # Before the outcome: a kind may judge a run whose report was disputed differently (qa.outcome_of).
            found = {**found, **self._review_notes(run, show, problem)}
        fatal = [problem] if problem else []
        if show and not problem:
            if show.get("state") in ("needs_input", "blocked"):
                fatal.append("the agent stopped to ask instead of finishing")
            elif not self._finished(show):
                fatal.append(f"the run did not finish as it should ({show.get('state')}"
                             + (f", {show['state_reason']}" if show.get("state_reason") else "") + ")")
        stopped = problem == self.stopped
        outcome, doubts = self._outcome(run, found, problems, [] if stopped else fatal, stopped)
        result = {**found, "problems": problems,
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
        if run.folder:
            # Its checkouts and refs go; the clones a ticket may be working in are not touched otherwise. What the
            # run left in a checkout goes with it: nothing of it was asked for, and a stash would land in the
            # clone's own list, a ticket's.
            ws = self.cfg.paths.workspace
            owned = self._owned(run)
            names = [r["repo"] for r in snap["repos"]]
            names += [n for n in snap.get("pinned") or {} if n not in names]
            for name in names:
                dest = None
                if owned:
                    try:
                        dest = self._checkout(run, name)
                    except OSError as e:  # a link on the way: whatever it leads to is not the runner's to remove
                        self.emit("checkout_left", task_id=run.id, run=run.n, repo=name, reason=str(e))
                if dest is not None:
                    self._drop(run, ws / name, dest)
                else:
                    self.git.drop_ref(ws / name, self._pinned(run))
            if owned:
                restore_workspace(self.git, self.root(run), snap["branch"], [], None, stash_message="",
                                  on_stashed=lambda repo, stash: None, on_committed=lambda repo, sha: None)
            else:
                # The folder was replaced or moved since: nothing in it is removed, only the run's refs in the clones.
                self.emit("folder_not_ours", task_id=run.id, run=run.n, folder=run.folder)
        else:
            self._restore_shared(run, snap)
        run = self.store.update_run(run.id, state="publishing")
        self.emit("workspace_restored", task_id=run.id, run=run.n)
        self._publish(run)

    def _drop(self, run: AgentRun, clone: Path, dest: Path) -> None:
        """A run's checkout of `clone` at `dest` gone, and the run's ref in the clone. Safe to repeat."""
        left = self.git.drop_checkout(clone, dest)
        if left:
            self.emit("leftovers_dropped", task_id=run.id, run=run.n,
                      repo=str(dest.relative_to(self.repos_dir(run))), files=len(left))
        self.git.drop_ref(clone, self._pinned(run))

    def _restore_shared(self, run: AgentRun, snap: dict) -> None:
        restore_workspace(
            self.git, self.cfg.paths.workspace, snap["branch"], [r for r in snap["repos"] if r["switch"]],
            snap.get("home_prev"), stash_message=f"arceus: what {self.NOUN} {run.id} left",
            on_stashed=lambda repo, stash: self.emit("leftovers_stashed", task_id=run.id, run=run.n, repo=repo,
                                                     stash=stash),
            on_committed=lambda repo, sha: self.emit("committed", task_id=run.id, run=run.n, repo=repo, sha=sha))

    def _close(self, run: AgentRun, outcome: str, why: str, extra: dict | None = None) -> None:
        """A run that never reached the agent: it still gets its report, saying why. What it pinned goes first."""
        if not self._unpin(run):
            return
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
        mine = not run.folder or self._owned(run)  # never removed from a folder that is no longer the runner's
        out = self.out_dir(run)
        if mine and out.is_dir() and not out.is_symlink():
            shutil.rmtree(out)
        linked = self.review_link(run).parent if self.REPORT_FILE and mine else None
        if linked and linked.is_dir() and not linked.is_symlink() and not linked.parent.is_symlink():
            shutil.rmtree(linked)
        run = self.store.update_run(run.id, state="done", finished_at=finished)
        self.write_index()
        result = json.loads(run.result or "{}")
        url, tunnel = self.report_address(run)
        self.emit("published", task_id=run.id, run=run.n, outcome=run.outcome,
                  findings=len(result.get("findings") or []), url=url, tunnel=tunnel)

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

    def _brief_source(self, run: AgentRun) -> tuple[str | None, str, Path]:
        """The instructions this run is given, or None and why it cannot start, and the file they come from."""
        text, why = self._instructions()
        return text, why, self.c.instructions

    def _test_until(self, start: datetime) -> datetime | None:
        """When the kind's agent is told to stop its work and write the report, for a run that starts at `start`;
        None for a kind that names no such time."""
        return None

    def _note_progress(self, run: AgentRun) -> AgentRun:
        """What the kind keeps of the agent's progress while it works, read on every pass of a running run. Safe to
        repeat; the run as it is after."""
        return run

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

    @staticmethod
    def _disputed(show: dict) -> bool:
        """The engine's Codex read the agent's report and still objected at its round limit (engine/SERVER.md: a
        `succeeded_research` is reviewed by Codex). A task sent with REPORT_CRITERIA (`--report-at-limit accept`)
        then ends `done` with `review: passed`, `accepted_with_notes` and the notes in `review_why`; one sent
        without, or to an engine older than 1.20, ends `failed`, needs-user, with the review's `VERDICT: FAIL`.
        Either way the report is whole and changed nothing; what is disputed is how some of it is put. That is no
        reason to throw away everything the run found, nor to show it as accepted: the objections go into the
        report beside it. Any other `needs-user` (an unreadable verdict, a review that passed but left something
        for a person, a question) is not this."""
        receipt = show.get("receipt") or {}
        if receipt.get("result") != "succeeded_research" or receipt.get("commits"):
            return False
        if show.get("state") == "done":
            return receipt.get("review") == "passed" and receipt.get("accepted_with_notes") is True
        return (show.get("state") == "failed" and show.get("state_reason") == "needs-user"
                and receipt.get("review") == "failed" and "VERDICT: FAIL" in str(receipt.get("review_why") or ""))

    def _finished(self, show: dict) -> bool:
        """Whether the engine's record shows a run that ended as it should: as a code review does, having changed
        nothing (code_review.ReviewAgent._finished_well), or with its report disputed (`_disputed`)."""
        return ReviewAgent._finished_well(show) or self._disputed(show)

    def _engine_notes(self, show: dict) -> dict:
        """What of the engine's record the kind keeps with the result: that the report was disputed
        (`engine_disputed`, whether or not the review said why) and its objections, without the review's own heading,
        cut short and with anything secret-looking taken out."""
        if not self._disputed(show):
            return {}
        why = str((show.get("receipt") or {}).get("review_why") or "")
        # Markdown bold the report pages would show as literal asterisks.
        why = why.split("VERDICT: FAIL", 1)[-1].replace("**", "").strip()
        return {"engine_disputed": True, "engine_review": self.events.redact(clean_text(why, 4000))}

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
