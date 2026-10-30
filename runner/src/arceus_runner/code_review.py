"""The review agent: a person asks for a code review on the ticket, and a separate Bulava task does it.

Off unless `review_agent.enabled`; with it off, nothing here runs and every comment goes where it went
before. With it on, for a ticket whose task has merge requests:

- **Who it is for.** A comment that starts with `review_agent.command` (`/review`), or that mentions the
  tracker bot first and then says `review` (`@agent-runner review`), is a request to the reviewer. The
  runner tells it apart by those words alone, before any other handler reads the comment: it never goes
  to the agent that wrote the code (review.py, Delivery.check_answers), and nothing else goes to the
  reviewer. A merge request note is never a request; the same words there are not sent to the author
  either. Only people on `allowed_users` may ask, and only while the ticket is in Code Review.
- **A task of its own.** The review is the engine task `<KEY>.review-<n>`, never a follow-up of the
  author's session, so the author does not review itself. It gets the ticket whole (description and
  every comment, oldest first, nothing cut: an overlong text goes to a person through the security
  screen instead) and the open MRs, each repository checked out at the commit the code host has, on a
  local branch `arceus-review/<KEY>-<n>` that is never pushed. Its repositories are not writable for it:
  it may build and run tests, not change the code. It asks nobody anything, and writes its verdict to
  `.arceus/reviews/<KEY>/<id>.json`.
- **The workspace.** From the switch to the end of the run it holds the shared workspace like a task does
  (db.REVIEW_HOLDS): nothing else changes a branch there, cleanup counts it as the ticket's work. Then the
  runner stashes what the run left, checks every repository out where it was and frees the workspace. A
  crash at any point resumes from the stored state.
- **The runner decides, the model advises.** An MR is approved only when the reviewer found it ready
  (requirements met, build and tests passed, no blocker), the code host's CI passed on the reviewed
  commit (`require_green_ci`), the MR still points at that commit and the same target, the ticket is
  still in Code Review with the text the review read, and the feature is still on. The approver account
  then approves exactly that commit, and the MR gets a note that it is ready to merge and needs no
  further review. When a changed file matches `attention_paths` (the security boundary, secrets) or the
  reviewer named something a person has to decide (its prompt, point 3), it is approved all the
  same, and the note says instead which changes a person has to look at before merge. What only whoever
  deploys it has to know (an environment variable, a runtime upgrade the ticket asked for) is listed in the
  note under its own heading and leaves the outcome as it is. Any other outcome gets a note that says what has to change or why it could not be confirmed,
  and an approval the approver gave before is taken back.
- **The fix loop.** What has to change goes to the author as a round of its own (`fix_rounds` per ticket),
  and once the author has pushed new commits the runner asks for the same review again on them, nobody
  writing anything. A round that changes no code ends the loop: its answer is for a person. Past the
  limit, or with `fix_rounds: 0`, people pass findings on the usual way. A request for commits that
  already have a final review is answered with that review.

Every external step goes through the action journal and carries a marker, so a restart neither loses
the result nor posts it twice.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from arceus_runner.config import DOC_PATHS, TEST_PATHS
from arceus_runner.db import ACTIVE_STATES, ReviewRequest, ReviewStore, Task
from arceus_runner.engine import EngineError
from arceus_runner.gitops import HOME_BRANCH, GitError
from arceus_runner.http import ServiceError
from arceus_runner.integrations import Approval, Comment, IssueDetails, key_part
from arceus_runner.replies import decisions_file, unattributed
from arceus_runner.screening import is_decision, trusted

REVIEWS_DIR = Path(".arceus") / "reviews"
BRANCH_PREFIX = "arceus-review/"
# A ticket may ask for a review while its task waits on people: the MRs are open, nobody works on them.
REVIEWABLE = ("code_review", "needs_input", "failed")
FIX_KIND = "code_review"   # the signal that carries a review's findings to the author
AUTO_PREFIX = "auto-"      # the request the runner makes itself once the author pushed fixes
# A review with one of these outcomes stands for the commits it read: asking again for them gets it back.
FINAL = ("approved", "attention", "changes")
# "attention" is an approval that also asks a person to look at named changes; "ready" is one that was refused.
ORDER = ("changes", "unverified", "ready", "attention", "approved")  # the overall outcome is the first one found
RESUME = "The session was interrupted (a reboot or a crash). Continue the review from where you stopped."
MAX_FILE_BYTES = 512 * 1024
MAX_TEXT = 3000
MAX_ITEMS = 30
VERDICTS = ("ready", "changes", "unsure")
BUILD = ("passed", "failed", "not_run")
TESTS = ("passed", "failed", "not_run", "none")
_MARKER = re.compile(r"<!--\s*arceus", re.IGNORECASE)
_TRACKER_MARK = re.compile(r"\s*\[arceus:[^\]]*\]\s*$")
_WORD = re.compile(r"[\w-]")
_REVIEW_WORD = re.compile(r"(?:code[ -]?)?review\b", re.IGNORECASE)

SCHEMA_EXAMPLE = """\
{"summary": "Two or three sentences for the ticket.",
 "mrs": [{"mr": "M1", "verdict": "ready", "requirements_met": true, "build": "passed", "tests": "passed",
          "checked": "make verify: 132 tests passed",
          "findings": [{"severity": "suggestion", "file": "app/login.py", "line": 40, "text": "…"}],
          "attention": [{"file": "app/accounts.py", "why": "…"}],
          "deploy": [{"file": "Dockerfile", "what": "…"}]}]}"""


def parse_request(text: str, command: str, mention: str | None) -> str | None:
    """What a comment asks of the reviewer, or None when it is not a request to the reviewer.

    `/review …` at the start; or the bot mentioned first, then `/review` or the word `review`
    (`@agent-runner review, mostly the migration`). What follows is the request's own text.
    """
    s = text.strip()
    if mention and s[:len(mention)].lower() == mention.lower() and not _WORD.match(s[len(mention):len(mention) + 1]):
        rest = s[len(mention):].lstrip(" \t,:")
        word = _REVIEW_WORD.match(rest)
        if word:
            return rest[word.end():].lstrip(" \t,:.").strip()
        s = rest
    if s[:len(command)].lower() == command and not _WORD.match(s[len(command):len(command) + 1]):
        return s[len(command):].lstrip(" \t,:.").strip()
    return None


FIX_SIGNAL = "fix-"


def _fix_signal(row) -> str:
    return f"{FIX_SIGNAL}{row.id}"


def _auto_of(row) -> str:
    """The review whose fixes a request the runner made itself reviews again: auto-<commits>-<review id>."""
    return row.comment_id.split("-", 2)[2]


def _glob(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE)


def attention_matches(files: list[str], patterns: list[str]) -> dict[str, str]:
    """Changed file → the `attention_paths` pattern it matches. A pattern without `/` matches the file name."""
    compiled = [(p, _glob(p)) for p in patterns]
    out: dict[str, str] = {}
    for f in files:
        name = f.rsplit("/", 1)[-1]
        for p, rx in compiled:
            if rx.match(f if "/" in p else name):
                out[f] = p
                break
    return out


def _clean(value: object, limit: int = MAX_TEXT) -> str:
    """Text the reviewer wrote, as the runner may post it: plain, no reply markers, no model labels, bounded."""
    if not isinstance(value, str):
        return ""
    text = unattributed(_MARKER.sub("<!-- ", value)).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def load_verdict(path: Path, labels: set[str], max_suggestions: int) -> tuple[dict[str, dict], str, list[str]]:
    """The reviewer's verdict for each MR of the review, its summary, and one line per thing left out.

    The file is the reviewer's, so it is read defensively: only the MRs of this review, known values
    only (anything else reads as not shown), plain text, a bounded number of entries. A file shaped in
    a way nobody foresaw is no verdict, never an error that would keep the workspace held."""
    try:
        return _load_verdict(path, labels, max_suggestions)
    except (TypeError, AttributeError, ValueError, OSError) as e:
        return {}, "", [f"the verdict file could not be read ({type(e).__name__})"]


def _load_verdict(path: Path, labels: set[str], max_suggestions: int) -> tuple[dict[str, dict], str, list[str]]:
    if not path.is_file() or path.is_symlink():
        return {}, "", ["the reviewer wrote no verdict file"]
    if path.stat().st_size > MAX_FILE_BYTES:
        return {}, "", [f"the verdict file is larger than {MAX_FILE_BYTES} bytes"]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        return {}, "", [f"the verdict file is not JSON ({type(e).__name__})"]
    if not isinstance(data, dict) or not isinstance(data.get("mrs"), list):
        return {}, "", ["the verdict file has no list of merge requests"]
    out: dict[str, dict] = {}
    problems: list[str] = []
    for i, e in enumerate(data["mrs"]):
        label = e.get("mr") if isinstance(e, dict) else None
        if not isinstance(label, str) or label not in labels:
            problems.append(f"entry {i}: {label!r} is not a merge request of this review")
            continue
        if label in out:
            # Two verdicts for one MR: which one the reviewer meant cannot be told, so neither is a pass.
            out[label]["doubts"].append("the reviewer gave it more than one verdict")
            continue
        # What the reviewer wrote for this MR and the runner could not read: the verdict is then not a pass.
        doubts: list[str] = []
        for field in ("findings", "attention", "deploy"):
            if e.get(field) is not None and not isinstance(e.get(field), list):
                doubts.append(f"its `{field}` is not a list, so what the reviewer meant there is not known")
        findings, suggestions = [], 0
        for f in e.get("findings") if isinstance(e.get("findings"), list) else []:
            if not isinstance(f, dict) or not _clean(f.get("text")):
                doubts.append("one of its findings has no readable text")
                continue
            # Anything but an explicit suggestion stops the merge: an unknown severity is not a pass.
            severity = "suggestion" if f.get("severity") == "suggestion" else "blocker"
            if severity == "suggestion":
                suggestions += 1
                if suggestions > max_suggestions:
                    continue
            line = f.get("line") if isinstance(f.get("line"), int) and not isinstance(f.get("line"), bool) else None
            findings.append({"severity": severity, "text": _clean(f.get("text"), 1500),
                             "file": _clean(f.get("file"), 300), "line": line})
        attention = []
        for a in e.get("attention") if isinstance(e.get("attention"), list) else []:
            if not isinstance(a, dict) or not (_clean(a.get("file")) or _clean(a.get("why"))):
                doubts.append("one of its points for a person's attention is not readable")
                continue
            attention.append({"file": _clean(a.get("file"), 300), "why": _clean(a.get("why"), 800)})
        deploy = []
        for a in e.get("deploy") if isinstance(e.get("deploy"), list) else []:
            what = _clean(a.get("what") or a.get("why"), 800) if isinstance(a, dict) else ""
            if not what:
                doubts.append("one of its notes for whoever deploys it is not readable")
                continue
            deploy.append({"file": _clean(a.get("file"), 300), "what": what})
        met = e.get("requirements_met")
        out[label] = {
            "verdict": e.get("verdict") if e.get("verdict") in VERDICTS else "unsure",
            "requirements_met": met if isinstance(met, bool) else None,
            "build": e.get("build") if e.get("build") in BUILD else "not_run",
            "tests": e.get("tests") if e.get("tests") in TESTS else "not_run",
            "checked": _clean(e.get("checked"), 800),
            "findings": findings[:MAX_ITEMS],
            "attention": attention[:MAX_ITEMS],
            "deploy": deploy[:MAX_ITEMS],
            "doubts": list(dict.fromkeys(doubts)),
        }
    return out, _clean(data.get("summary")), problems


def _digest(issue: IssueDetails) -> str:
    return hashlib.sha256(f"{issue.summary}\n{issue.description}".encode()).hexdigest()


def _when(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M UTC")


class ReviewAgent:
    def __init__(self, delivery):
        self.d = delivery
        self.cfg = delivery.cfg
        self.rc = delivery.cfg.review_agent
        self.store: ReviewStore = delivery.store
        self.events = delivery.events
        self.host = delivery.host
        self.tracker = delivery.tracker
        self._waiting: dict[str, tuple] = {}
        self._held_back: dict[str, str] = {}  # review id: why its findings did not go to the author

    @property
    def enabled(self) -> bool:
        return self.rc.enabled

    # -- who a comment is for --------------------------------------------------------------------

    def request(self, text: str) -> str | None:
        """A ticket comment's request to the reviewer, or None (not one, or the feature is off)."""
        if not self.enabled:
            return None
        return parse_request(text, self.rc.command, self.tracker.mention(self.cfg.tracker.bot_user))

    def claims(self, task_id: str, comment: Comment) -> bool:
        """The comment is the reviewer's: a request now, or one recorded while the feature was on."""
        return self.request(comment.text) is not None or self.store.review_of_comment(task_id, comment.id) is not None

    def is_mr_command(self, text: str) -> bool:
        """The review command written on a merge request: not a request (only the ticket starts a review),
        and not something for the author either."""
        return self.enabled and parse_request(text, self.rc.command, f"@{self.cfg.code_host.bot_user}") is not None

    # -- the workspace ----------------------------------------------------------------------------

    def holding(self) -> Task | None:
        """The ticket whose review holds the shared workspace now, if any."""
        row = self.store.review_holding()
        return self.store.task(row.task_id) if row else None

    def waiting(self) -> bool:
        """A request waits for the workspace: it goes before new tickets. One held for a security reviewer
        does not keep them waiting."""
        return any((t := self.store.task(r.task_id)) and t.state != "security_hold" for r in self.store.reviews("pending"))

    def _branch(self, row: ReviewRequest) -> str:
        return f"{BRANCH_PREFIX}{row.task_id}-{row.n}"

    def _ticket_comments(self, task: Task) -> list[Comment]:
        """The ticket's comments as the reviewer reads them: without the security screen's own exchange (the
        runner's hold notice, a reviewer's decision, the release), so a request a reviewer released is the
        same text when it is screened again, and is let through instead of held once more."""
        bot = self.cfg.tracker.bot_user
        return [x for x in self.tracker.comments(task.issue_db_id)
                if not is_decision(x.text) and not (x.author == bot and "[arceus:security" in x.text)]

    def _context(self, task: Task, issue: IssueDetails) -> str:
        """What people asked for, as one digest: the summary, the description and every comment people wrote
        (by id, author and text, so an edit or a deletion counts), the requests to the reviewer left out."""
        bot = self.cfg.tracker.bot_user
        people = [[str(x.id), x.author, x.text] for x in self._ticket_comments(task)
                  if x.author != bot and self.request(x.text) is None]
        return hashlib.sha256(json.dumps([_digest(issue), people]).encode()).hexdigest()

    def _ticket_problem(self, task: Task, snap: dict) -> str | None:
        """Why the ticket no longer backs the review's verdict, or None."""
        c = self.cfg.tracker
        issue = self.tracker.issue(task.task_id)
        if issue.state != c.states.code_review:
            return f"the ticket is no longer in {c.states.code_review} (it is in {issue.state or 'no state'})"
        if self._context(task, issue) != snap["ticket"]["context"]:
            return "the ticket's description or comments changed while the review ran"
        return None

    def _verdict_file(self, row: ReviewRequest) -> Path:
        return self.cfg.paths.workspace.resolve() / REVIEWS_DIR / row.task_id / f"{row.id}.json"

    def _extra_dirs(self, row: ReviewRequest) -> Path:
        return self.cfg.paths.db.parent / "tasks" / row.id / "extra-dirs.txt"

    def _note_waiting(self, row: ReviewRequest, reason: str, **fields) -> None:
        seen = (reason, *sorted((k, str(v)) for k, v in fields.items()))
        if self._waiting.get(row.id) != seen:
            self._waiting[row.id] = seen
            self.events.emit("review_waits", task_id=row.task_id, review=row.id, reason=reason, **fields)

    # -- requests (the tracker's poll) ------------------------------------------------------------

    def check_requests(self) -> None:
        if not self.enabled:
            return
        c = self.cfg.tracker
        # A task the author is fixing is read too: a request written meanwhile is answered now (the ticket is
        # not in Code Review), not left lying until the task comes back and then run on other commits.
        for task in self.store.tasks(*REVIEWABLE, *ACTIVE_STATES):
            if not self.store.task_mrs(task.task_id):
                continue  # the runner opened no merge request for this ticket: nothing of its own to review
            if task.state == "failed" and not (task.failed_at_ms and self.d.now() - datetime.fromtimestamp(
                    task.failed_at_ms / 1000, UTC) <= self.cfg.limits.continue_window):
                continue
            try:
                comments = self.tracker.comments(task.issue_db_id)
                for x in comments:
                    if x.author == c.bot_user or x.created < (task.published_at_ms or 0):
                        continue
                    asked = self.request(x.text)
                    if asked is None or self.store.review_of_comment(task.task_id, x.id):
                        continue
                    if self.store.signal(f"yt-{x.id}"):
                        continue  # read as an ordinary comment before the reviewer was switched on
                    self._record(task, x, asked)
                self._review_again(task)  # after people's requests: one of theirs reviews the new commits instead
            except ServiceError as e:
                self.events.emit("reconcile_failed", task_id=task.task_id, reason=str(e))
                return

    def _record(self, task: Task, x: Comment, asked: str) -> None:
        c = self.cfg.tracker
        who = self.tracker.mention(x.author)
        refusal = None
        if x.author not in c.allowed_users:
            refusal = ("not_allowed", f"{who}, only people on the project's list can ask for a code review; "
                                      "none was started.")
        # Only reviews that were taken count against the limit: a refused request (someone off the list, a ticket
        # outside Code Review, a held text rejected) started nothing and must not use up the ticket's reviews.
        elif sum(r.state != "refused" for r in self.store.reviews(task_id=task.task_id)) >= self.rc.max_per_ticket:
            refusal = ("limit", f"{who}, this ticket has had all the code reviews it may have "
                                f"({self.rc.max_per_ticket}); none was started. A person reviews it from here.")
        elif self.store.reviews("pending", "preparing", "running", "restoring", "publishing", task_id=task.task_id):
            refusal = ("busy", f"{who}, a code review of this ticket is already under way; its result will be "
                               "posted here.")
        else:
            state = self.tracker.issue(task.task_id).state
            if state != c.states.code_review:
                refusal = ("not_in_code_review", f"{who}, a code review starts only while the ticket is in "
                                                 f"{c.states.code_review}; it is in {state or 'no state'}. "
                                                 f"Move it there and write `{self.rc.command}` again.")
        if refusal is None:
            row = self.store.add_review(task.task_id, x.id, x.author, x.created, asked)
            if row:
                self.events.emit("review_requested", task_id=task.task_id, review=row.id, author=x.author)
            return
        with self.store.transaction():
            row = self.store.add_review(task.task_id, x.id, x.author, x.created, asked, state="refused",
                                        outcome="refused", reason=refusal[0])
            if row:
                self._comment_intent(task, f"review:{row.id}", refusal[1])
        if row:
            self.events.emit("review_refused", task_id=task.task_id, review=row.id, author=x.author,
                             reason=refusal[0])
            self.d.retry_pending()

    def _comment_intent(self, task: Task, kind: str, text: str) -> None:
        """A ticket comment journaled with the change that implies it; retry_pending() posts it."""
        self.store.action_intent(f"{task.task_id}:comment:{kind}", task.task_id, "comment",
                                 json.dumps({"kind": kind, "text": text}))

    def _refuse(self, row: ReviewRequest, task: Task | None, reason: str, text: str,
                outcome: str = "refused") -> None:
        with self.store.transaction():
            self.store.update_review(row.id, state="refused" if outcome == "refused" else "done", outcome=outcome,
                                     reason=reason)
            if task:
                self._comment_intent(task, f"review:{row.id}", f"{self.tracker.mention(row.author)}, {text}")
        self.events.emit("review_refused", task_id=row.task_id, review=row.id, reason=reason)
        self.d.retry_pending()

    # -- starting one (the local poll) --------------------------------------------------------------

    def start(self, can_take: bool) -> None:
        """The oldest waiting request, when the workspace is free: prepared, screened, dispatched."""
        for row in self.store.reviews("pending"):
            task = self.store.task(row.task_id)
            if not self.enabled:
                self._refuse(row, task, "switched_off", "the code review was switched off before it started; "
                                                        "nothing was reviewed.")
                continue
            if task is None or task.state in ("done", "stopped"):
                self._refuse(row, task, "task_over", "the ticket's work is over, so there is nothing to review.")
                continue
            if task.state not in REVIEWABLE:
                continue  # the author is working on it, or a security reviewer decides: the review waits
            if not can_take:
                return
            holder = self.d.workspace_taken()
            if holder is not None:
                self._note_waiting(row, "workspace_taken", holder=holder.task_id)
                return
            try:
                self._prepare(row, task)
            except (ServiceError, EngineError, GitError) as e:
                self.events.emit("review_step_failed", task_id=row.task_id, review=row.id, step="prepare",
                                 reason=str(e))
            return  # one at a time

    def _prepare(self, row: ReviewRequest, task: Task) -> None:
        issue = self.tracker.issue(task.task_id)
        if issue.state != self.cfg.tracker.states.code_review:
            self._refuse(row, task, "not_in_code_review", f"the ticket left {self.cfg.tracker.states.code_review} "
                                                          "before the review could start; none was run.")
            return
        open_mrs = []
        for m in self.store.task_mrs(task.task_id):
            st = self.host.mr_status(m.project_id, m.iid)
            if st.state == "opened":
                open_mrs.append((m, st))
        if not open_mrs:
            self._refuse(row, task, "nothing", "this ticket has no open merge request, so there is nothing to "
                                               "review.", outcome="nothing")
            return
        group = {r.relative_path: r for r in self.d._group()}
        ws = self.cfg.paths.workspace
        branch = self._branch(row)
        mrs, dirty = [], []
        for i, (m, st) in enumerate(open_mrs, 1):
            repo = group.get(m.repo)
            if repo is None:
                self._refuse(row, task, "repo_gone", f"{m.repo} is no longer in the {self.host.label} "
                                                     f"{self.host.scope_kind}; no review was run.",
                             outcome="unverified")
                return
            path = ws / m.repo
            self.d.ensure_repo(repo, path)
            if self.git.current_branch(path) != branch and not self.git.is_clean(path):
                dirty.append(m.repo)
                continue
            target = st.target or repo.default_branch or "main"
            self.git.fetch_branch(path, task.branch)
            sha = st.sha or self.git.remote_sha(path, task.branch) or ""
            if not sha or not self.git.has_commit(path, sha):
                self._refuse(row, task, "no_commit", f"the head of {m.url} could not be fetched; no review was run.",
                             outcome="unverified")
                return
            current = self.git.current_branch(path)
            mrs.append({"label": f"M{i}", "repo": m.repo, "project_id": m.project_id, "iid": m.iid, "url": m.url,
                        "sha": sha, "target": target, "source": task.branch,
                        "files": self.git.changed_files(path, f"origin/{target}", sha),
                        "prev": current if current != branch else None, "prev_head": self.git.head(path)})
        home = ws
        if (home / ".git").exists() and self.git.current_branch(home) != branch and not self.git.is_clean(home):
            dirty.append(".")
        if dirty:
            self._note_waiting(row, "uncommitted_changes", repos=dirty)
            return
        # The same commits, merged into the same branches, against the same ticket: the earlier verdict stands.
        context = self._context(task, issue)
        reviewed = ({(m["repo"], m["sha"], m["target"]) for m in mrs}, context)
        for earlier in self.store.reviews("done", task_id=task.task_id):
            then = json.loads(earlier.snapshot)
            if earlier.outcome in FINAL and ({(m["repo"], m["sha"], m["target"]) for m in then.get("mrs", [])},
                                             then.get("ticket", {}).get("context")) == reviewed:
                self._refuse(row, task, "already_reviewed", f"these commits were already reviewed (see the review "
                                                            f"{earlier.id} above); nothing changed since, so it "
                                                            "stands.", outcome="refused")
                return
        comments = self._ticket_comments(task)
        text = self._compose(row, task, issue, comments, mrs)
        authors = [row.author, *issue.writers, *(x.author for x in comments)]
        clearance = self.d.gate.clear(task, "review_request", text, authors=authors, resume_state=task.state,
                                      ref=row.id)
        if clearance is None:
            return  # held for a security reviewer, refused, or the screen is down: nothing switched
        snapshot = {"branch": branch, "text": text, "authors": authors,
                    "home_prev": self.git.current_branch(home) if (home / ".git").exists() else None,
                    "ticket": {"context": context}, "mrs": mrs}
        row = self.store.update_review(row.id, state="preparing", snapshot=json.dumps(snapshot),
                                       started_at=self.d.now().isoformat())
        self._waiting.pop(row.id, None)
        self.events.emit("review_preparing", task_id=row.task_id, review=row.id, mrs=[m["url"] for m in mrs])
        self._dispatch(row)

    @property
    def git(self):
        return self.d.git

    def _dispatch(self, row: ReviewRequest) -> None:
        """Switch the workspace to what is reviewed and hand the review to the engine. Safe to repeat."""
        snap = json.loads(row.snapshot)
        task = self.store.task(row.task_id)
        ws = self.cfg.paths.workspace
        branch = snap["branch"]
        for m in snap["mrs"]:
            self.git.start_branch(ws / m["repo"], branch, m["sha"])
        extra = self._extra_dirs(row)
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("".join(f"{ws / m['repo']}\n" for m in snap["mrs"]))
        # Read, build and test there, never edit: no writable-directories.txt beside the list.
        (extra.parent / "writable-directories.txt").unlink(missing_ok=True)
        self._verdict_file(row).parent.mkdir(parents=True, exist_ok=True)
        clearance = self.d.gate.clear(task, "review_request", snap["text"], authors=snap["authors"],
                                      resume_state=task.state, ref=row.id)
        if clearance is None:
            return  # the screen is down right now: the switched workspace stays held, tried again next tick
        reply = self.d.engine.dispatch(task_id=row.id, message_id=f"review-{row.id}", author=row.author,
                                       source=self.tracker.engine_source, branch=branch, base=HOME_BRANCH,
                                       repo=Path(ws), text=snap["text"], clearance=clearance, extra_dirs=extra)
        if reply.accepted:
            self.store.update_review(row.id, state="running", started_at=self.d.now().isoformat())
            self.events.emit("review_dispatched", task_id=row.task_id, review=row.id, tier=reply.tier,
                             **self.cfg.bulava.model_profile())
        elif reply.rc == 6 and not self._overdue(row):
            self.events.emit("review_busy", task_id=row.task_id, review=row.id, reason=reply.why())
        else:
            self._settled(row, None, f"the execution engine did not accept the review ({reply.why()})")

    def _overdue(self, row: ReviewRequest) -> bool:
        started = datetime.fromisoformat(row.started_at) if row.started_at else self.d.now()
        return self.d.now() - started > self.rc.timeout

    # -- following one ------------------------------------------------------------------------------

    def reconcile(self) -> None:
        """Every review past its start, each where it stands: dispatched, followed, put back, published."""
        for row in self.store.reviews("preparing", "running", "restoring", "publishing"):
            try:
                if row.state == "preparing":
                    if self.d.engine.show(row.id) is not None:  # dispatched before a crash, never recorded
                        self.store.update_review(row.id, state="running")
                    else:
                        self._dispatch(row)
                elif row.state == "running":
                    self._follow(row)
                elif row.state == "restoring":
                    self._restore(row)
                else:
                    self._publish(row)
            except (ServiceError, EngineError, GitError) as e:
                self.events.emit("review_step_failed", task_id=row.task_id, review=row.id, step=row.state,
                                 reason=str(e))

    def _follow(self, row: ReviewRequest) -> None:
        show = self.d.engine.show(row.id)
        if show is None:
            self._settled(row, None, "the execution engine lost track of the review")
            return
        state = show.get("state")
        if state in ("queued", "running"):
            if state == "queued" or show.get("attempt_live"):
                if self._overdue(row):
                    self.d.engine.stop(Path(self.cfg.paths.workspace))
                    hours = self.rc.timeout.total_seconds() / 3600
                    self._settled(row, show, f"the review gave no verdict in {hours:g} h and was stopped")
                return
            attempt = len(show.get("attempts") or [])
            reply = self.d.engine.send(task_id=row.id, message_id=f"resume-{row.id}-{attempt}", source="other",
                                       author=self.cfg.tracker.bot_user, repo=Path(self.cfg.paths.workspace),
                                       text=RESUME, clearance=trusted(RESUME), extra_dirs=self._extra_dirs(row))
            if not reply.accepted and reply.rc != 6:
                self._settled(row, show, f"the execution engine did not take the review up again ({reply.why()})")
            return
        self._settled(row, show)

    def _settled(self, row: ReviewRequest, show: dict | None, problem: str | None = None) -> None:
        """The run is over (or never took place): keep what it found, then put the workspace back."""
        snap = json.loads(row.snapshot)
        found, summary, problems = load_verdict(self._verdict_file(row), {m["label"] for m in snap["mrs"]},
                                                self.rc.max_suggestions)
        # What makes the run's verdict untrustworthy as a whole: it never finished as asked.
        fatal = [problem] if problem else []
        if show and show.get("state") in ("needs_input", "blocked"):
            fatal.append("the reviewer stopped to ask instead of finishing")
        elif show and not problem and not self._finished_well(show):
            fatal.append(f"the review run did not finish as it should ({show.get('state')}"
                         + (f", {show['state_reason']}" if show.get("state_reason") else "") + ")")
        problems = [*fatal, *problems]
        verdict = {"mrs": found, "summary": summary, "problems": problems, "fatal": fatal,
                   "engine": (show or {}).get("state"), "settled_at": self.d.now().isoformat()}
        row = self.store.update_review(row.id, state="restoring", verdict=json.dumps(verdict))
        self.events.emit("review_finished", task_id=row.task_id, review=row.id, engine_state=verdict["engine"],
                         verdicts={k: v["verdict"] for k, v in found.items()}, problems=problems or None)
        self._restore(row)

    @staticmethod
    def _finished_well(show: dict) -> bool:
        """The run ended the way a review ends: settled as a result (`done`), or banked as debt only because
        the engine cannot verify a run that changed nothing while its review passed it. Anything else (a
        failure, a stall, commits waiting to be published) leaves the verdict file unbacked."""
        if show.get("state") == "done":
            return True
        receipt = show.get("receipt") or {}
        why = receipt.get("review_why") or ""
        # The engine's review gate banks a research run with an empty diff as debt without asking Codex
        # (hooks/review-gate.sh: "Declared '…' with an empty diff — no machine verification …"); that is
        # how every review that changed nothing ends. No repository may hold a commit of the run. A dirty
        # tree alone does not count: files another task's .gitignore hid turn up as untracked at the MR's
        # commit, and whatever the run left is stashed by _restore and never reaches the MR.
        unchanged = all(r.get("reviewed_sha") == r.get("base_sha") for r in receipt.get("repos") or [])
        return (show.get("state") == "failed" and show.get("state_reason") == "debt"
                and (show.get("outcome") or {}).get("result") in ("succeeded_research", "succeeded_no_change")
                and ("VERDICT: PASS" in why or (unchanged and "with an empty diff" in why)))

    def _restore(self, row: ReviewRequest) -> None:
        """Every repository back on the branch it was on, what the run left stashed, the review branch gone.
        Each step looks at where things are first, so a crash halfway is finished on the next tick."""
        snap = json.loads(row.snapshot)
        ws = self.cfg.paths.workspace
        branch = snap["branch"]
        for m in snap["mrs"]:
            path = ws / m["repo"]
            if not (path / ".git").exists():
                continue
            if self.git.current_branch(path) == branch:
                if not self.git.is_clean(path):
                    stash = self.git.stash_all(path, f"arceus: what the review {row.id} left")
                    self.events.emit("review_leftovers_stashed", task_id=row.task_id, review=row.id, repo=m["repo"],
                                     stash=stash)
                back = m.get("prev")
                self.git.switch(path, back if back and self.git.branch_exists(path, back) else m["prev_head"])
            sha = self.git.branch_sha(path, branch)
            if sha:
                if sha != m["sha"]:
                    self.events.emit("review_committed", task_id=row.task_id, review=row.id, repo=m["repo"], sha=sha)
                self.git.delete_branch_at(path, branch, sha)
        if (ws / ".git").exists():
            if self.git.current_branch(ws) == branch:
                back = snap.get("home_prev")
                self.git.home_on(ws, back if back and back != branch and self.git.branch_exists(ws, back)
                                 else HOME_BRANCH)
            sha = self.git.branch_sha(ws, branch)
            if sha:
                self.git.delete_branch_at(ws, branch, sha)
        row = self.store.update_review(row.id, state="publishing")
        self.events.emit("review_workspace_restored", task_id=row.task_id, review=row.id)
        self._publish(row)

    # -- the result ---------------------------------------------------------------------------------

    def _publish(self, row: ReviewRequest) -> None:
        task = self.store.task(row.task_id)
        if row.result:
            result = json.loads(row.result)
        else:
            result = self._decide(row, task)
            if result is None:
                return  # CI is still running on the reviewed commit: decided on a later tick
            row = self.store.update_review(row.id, result=json.dumps(result))
        # The flag as it is now, not as it was when the result was decided: a retry after it was switched off
        # touches no merge request.
        off = result["off"] or not self.enabled
        if not off:
            if any(mr["kind"] == "changes" for mr in result["mrs"]):
                self._send_back(row, task, result)
            for mr in result["mrs"]:
                self._act(row, task, mr)
        result = {**result, "off": off}
        outcomes = [self._mr_outcome(row, task, mr) for mr in result["mrs"] if mr["kind"] != "closed"]
        overall = "switched_off" if off else next((o for o in ORDER if o in outcomes), "nothing")
        self.d.comment_once(task, f"review:{row.id}", self._ticket_text(row, task, result, overall))
        self.store.update_review(row.id, state="done", outcome=overall)
        self.events.emit("review_published", task_id=row.task_id, review=row.id, outcome=overall,
                         mrs={mr["url"]: self._mr_outcome(row, task, mr) for mr in result["mrs"]})

    # -- the fix loop: what has to change goes to the author, and its new commits are reviewed again ----------

    def _send_back(self, row: ReviewRequest, task: Task, result: dict) -> None:
        """The review's findings as a round for the agent that wrote the code, while the ticket has fix rounds
        left and the findings still describe the code and the ticket. Recorded once per review: a retry of the
        publication sends nothing twice."""
        if self.store.signal(_fix_signal(row)) is not None:
            return
        used = len(self.store.signals_of_kind(task.task_id, FIX_KIND))
        if not self.rc.fix_rounds or used >= self.rc.fix_rounds:
            return
        held = None
        if task.state != "code_review":
            held = "the task is not waiting in Code Review"  # failed or waiting for an answer: a person decides
        elif problem := self._ticket_problem(task, json.loads(row.snapshot)):
            held = problem
        else:
            for mr in result["mrs"]:
                st = self.host.mr_status(mr["project_id"], mr["iid"])
                if mr["kind"] == "changes" and st.sha and st.sha != mr["sha"]:
                    held = f"{mr['url']} has new commits since the review"
        if held:
            self._held_back[row.id] = held
            self.events.emit("review_not_sent_back", task_id=task.task_id, review=row.id, reason=held)
            return
        if self.store.add_signal(_fix_signal(row), task.task_id, FIX_KIND, self.host.reviewer or row.author,
                                 self._fix_text(row, result), self.d.now().isoformat()):
            self.events.emit("review_sent_back", task_id=task.task_id, review=row.id, round=used + 1,
                             of=self.rc.fix_rounds)

    def _fix_text(self, row: ReviewRequest, result: dict) -> str:
        parts = []
        for mr in result["mrs"]:
            if mr["kind"] != "changes":
                continue
            v = mr["verdict"] or {}
            items = [self._finding(f) for f in v.get("findings", []) if f.get("severity") == "blocker"]
            if v.get("requirements_met") is False and not items:
                items.append("It does not do what the ticket asks.")
            for what in ("build", "tests"):
                if v.get(what) == "failed":
                    items.append(f"The {what} failed when the reviewer ran them.")
            items += [f"Note: {r}." for r in mr["reasons"]]
            parts.append(f"{mr['repo']} ({mr['url']}), reviewed at `{mr['sha'][:12]}`:\n"
                         + "\n".join(f"  - {i}" for i in items or ["see the review note on the MR"]))
        return (f"The code review {row.id} found these; the whole review is the note on the MR.\n"
                + "\n".join(parts) + "\n  Fix each one with new commits. If one is wrong, change nothing for it and "
                "say why in your result. The runner reviews your new commits again.")

    def _fix_round(self, row: ReviewRequest, task: Task) -> int | None:
        """Which fix round this review's findings went to the author as, or None if they did not."""
        ids = [s.id for s in self.store.signals_of_kind(task.task_id, FIX_KIND)]
        return ids.index(_fix_signal(row)) + 1 if _fix_signal(row) in ids else None

    def _sent_back_words(self, row: ReviewRequest, task: Task) -> str:
        n = self._fix_round(row, task)
        if n is not None:
            return (f"Sent to the agent to fix (round {n} of {self.rc.fix_rounds}). Its new commits are reviewed "
                    "again without anyone asking.")
        if not self.rc.fix_rounds:
            why = "findings go back only when a person passes them on"
        elif len(self.store.signals_of_kind(task.task_id, FIX_KIND)) >= self.rc.fix_rounds:
            why = f"the {self.rc.fix_rounds} automatic fix rounds of this ticket are used up"
        else:
            why = self._held_back.get(row.id) or "the findings may no longer match the code or the ticket"
        return ("Nothing was sent to the agent (" + why + "): to have it make these changes, reply in a thread "
                f"here mentioning the bot (`@{self.cfg.code_host.bot_user}`), or comment on the ticket.")

    def _review_again(self, task: Task) -> None:
        """The agent pushed commits after a review's findings went to it: the same request again, on them.
        Once per fix round; a re-review the runner refused (the commits were not there yet) may go again."""
        if not self.rc.fix_rounds or task.state != "code_review":
            return
        rows = self.store.reviews(task_id=task.task_id)
        if any(r.state not in ("done", "refused") for r in rows):
            return  # one under way, or a person's request waiting: that one reviews the new commits
        sent = [s for s in self.store.signals_of_kind(task.task_id, FIX_KIND)
                if s.batch_id and (b := self.store.batch(s.batch_id)) and b.sent_at and b.sent_at != "stale"]
        if not sent:
            return
        first = self.store.review(sent[-1].id[len(FIX_SIGNAL):])
        if first is None:
            return
        heads = {m.repo: m.published_sha for m in self.store.task_mrs(task.task_id)}
        if heads == {m["repo"]: m["sha"] for m in json.loads(first.snapshot or "{}").get("mrs", [])}:
            return  # the round changed no code: the agent's answer is for a person to read
        for r in rows:
            if r.state == "done" and (r.comment_id.startswith(AUTO_PREFIX) and _auto_of(r) == first.id
                                      or {m["repo"]: m["sha"] for m in json.loads(r.snapshot or "{}").get("mrs", [])}
                                      == heads):
                return  # this fix round was reviewed again, or these commits were reviewed anyway
        if self.tracker.issue(task.task_id).state != self.cfg.tracker.states.code_review:
            return
        if sum(r.state != "refused" for r in rows) >= self.rc.max_per_ticket:
            self.d.comment_once(task, f"review-again:{first.id}",
                                f"{self.tracker.mention(first.author)}, the agent pushed fixes for the review "
                                f"{first.id}, but this ticket has had all the code reviews it may have "
                                f"({self.rc.max_per_ticket}). A person reviews it from here.")
            return
        digest = hashlib.sha256(json.dumps(sorted(heads.items())).encode()).hexdigest()[:12]
        row = self.store.add_review(task.task_id, f"{AUTO_PREFIX}{digest}-{first.id}", first.author,
                                    int(self.d.now().timestamp() * 1000), first.request)
        if row:
            self.events.emit("review_requested", task_id=task.task_id, review=row.id, author=first.author,
                             after=first.id)

    def _decide(self, row: ReviewRequest, task: Task) -> dict | None:
        """Per MR: what the review found (kind) and whether every condition for an approval holds. The
        conditions are the runner's own reading of the code host and the ticket, never the reviewer's."""
        snap = json.loads(row.snapshot)
        verdict = json.loads(row.verdict or "{}")
        found = verdict.get("mrs") or {}
        ticket = self._ticket_problem(task, snap)
        fatal = verdict.get("fatal") or []
        settled = datetime.fromisoformat(verdict.get("settled_at") or self.d.now().isoformat())
        can_wait = self.d.now() - settled < self.rc.timeout
        mrs = []
        for m in snap["mrs"]:
            st = self.host.mr_status(m["project_id"], m["iid"])
            v = found.get(m["label"])
            risky = self._always_attention(m["files"])
            reasons: list[str] = []
            if st.state != "opened":
                kind = "closed"
            elif v is None:
                kind = "unverified"
                reasons.append("the reviewer left no verdict for it")
            elif v["verdict"] == "changes" or v["requirements_met"] is False or v["build"] == "failed" \
                    or v["tests"] == "failed" or any(f["severity"] == "blocker" for f in v["findings"]):
                kind = "changes"
                reasons += v.get("doubts", [])
            elif v["verdict"] == "unsure" or v["requirements_met"] is None or v["build"] != "passed" \
                    or v["tests"] not in ("passed", "none"):
                kind = "unverified"
                if v["verdict"] == "unsure":
                    reasons.append("the reviewer could not tell whether it is right")
                if v["requirements_met"] is None:
                    reasons.append("the reviewer did not say whether it meets the ticket")
                if v["build"] != "passed":
                    reasons.append("the build was not run")
                if v["tests"] not in ("passed", "none"):
                    reasons.append("the tests were not run")
            elif fatal or v.get("doubts"):
                # A verdict written before the run hung or stopped to ask, or one the runner could not read
                # whole, is not a pass.
                kind = "unverified"
                reasons += [*fatal, *v.get("doubts", [])]
            else:
                kind = "ready"
            moved = bool(st.sha) and st.sha != m["sha"]
            retargeted = bool(st.target) and st.target != m["target"]
            if moved:
                reasons.append(f"it has new commits since the review (now at `{st.sha[:12]}`), which nobody reviewed")
            if retargeted:
                reasons.append(f"it now merges into {st.target}, not {m['target']}")
            ci = self._ci(st, m["sha"])
            if kind == "ready":
                if self.rc.require_green_ci and ci != "passed":
                    if ci == "running" and not moved and can_wait:
                        return None
                    reasons.append(f"CI {self._ci_words(ci)}")
                if ticket:
                    reasons.append(ticket)
                if not getattr(self.host, "supports_approval", False):
                    reasons.append(f"Arceus has no approver account on {self.host.label}, so it cannot approve")
            mrs.append({k: m[k] for k in ("label", "repo", "project_id", "iid", "url", "sha", "target")} | {
                "kind": kind, "approve": kind == "ready" and not reasons and self.enabled, "reasons": reasons,
                "risky": risky, "ci": ci, "verdict": v})
        return {"off": not self.enabled, "mrs": mrs, "summary": verdict.get("summary") or "",
                "problems": verdict.get("problems") or []}

    @staticmethod
    def _ci(st, sha: str) -> str:
        if st.ci_id is None:
            return "none"
        if st.ci_sha != sha:
            return "other_commit"
        return {"success": "passed", "failed": "failed", "running": "running"}.get(st.ci_status or "", "other")

    @staticmethod
    def _ci_words(ci: str) -> str:
        return {"passed": "passed", "failed": "failed", "running": "is still running", "none": "has no pipeline",
                "other_commit": "has no run for the reviewed commit",
                "other": "did not finish (cancelled, skipped or manual)"}[ci]

    def _keys(self, row: ReviewRequest, mr: dict) -> str:
        return f"{row.task_id}:review:{row.id}:{key_part(mr['project_id'])}:{key_part(mr['iid'])}"

    def _act(self, row: ReviewRequest, task: Task, mr: dict) -> None:
        """Approve (or take back an earlier approval), then the note: each once, across crashes."""
        if mr["kind"] == "closed" or not self.enabled:
            return
        key = self._keys(row, mr)
        if mr["approve"]:
            act = self.store.action(f"{key}:approve")
            if not act or act[0] != "done":
                if not act:
                    self.store.action_intent(f"{key}:approve", task.task_id, "review-approve")
                # The decision may be older than this tick (a retry after a failure or a crash): every condition
                # is read again right before the approval, never taken from what was stored.
                late = self._approval_blocked(row, task, mr)
                a = Approval(False, late, stale=late.startswith("it has new commits")) if late else \
                    self.host.approve(mr["project_id"], mr["iid"], mr["sha"])
                self.store.action_done(f"{key}:approve", json.dumps({"done": a.done, "detail": a.detail,
                                                                       "stale": a.stale, "by": self.host.reviewer}))
                self.events.emit("review_approved" if a.done else "review_approval_refused", task_id=task.task_id,
                                 review=row.id, url=mr["url"], sha=mr["sha"], detail=a.detail or None)
        elif getattr(self.host, "supports_approval", False):
            act = self.store.action(f"{key}:revoke")
            if not act or act[0] != "done":
                if not act:
                    self.store.action_intent(f"{key}:revoke", task.task_id, "review-revoke")
                revoked = self.host.revoke(mr["project_id"], mr["iid"])
                self.store.action_done(f"{key}:revoke", json.dumps({"revoked": revoked}))
                if revoked:
                    self.events.emit("review_approval_revoked", task_id=task.task_id, review=row.id, url=mr["url"])
        note = f"{key}:note"
        act = self.store.action(note)
        if act and act[0] == "done":
            return
        approval = self._approval(row, task, mr)
        if approval and approval["done"]:
            # Approved, the note not posted yet (a failure or a crash in between): it says "ready to merge" only
            # while that still holds. If the MR moved on meanwhile, the approval is taken back and the note says why.
            late = self._approval_blocked(row, task, mr)
            if late:
                revoked = self.host.revoke(mr["project_id"], mr["iid"])
                self.store.action_done(f"{key}:approve", json.dumps({
                    "done": False, "detail": f"{late}; the approval given before was taken back" if revoked else late,
                    "stale": late.startswith("it has new commits")}))
                self.events.emit("review_approval_revoked", task_id=task.task_id, review=row.id, url=mr["url"],
                                 reason=late)
        marker = f"<!-- arceus:{row.id}:{mr['label']} -->"
        if not act:
            self.store.action_intent(note, task.task_id, "review-note")
        signer = {self.host.reviewer, self.cfg.code_host.bot_user}  # the bot posts it when the approver cannot
        if not any(marker in n.body and n.author in signer
                   for n in self.host.notes(mr["project_id"], mr["iid"], mr["url"])):
            self.host.note_review(mr["project_id"], mr["iid"], f"{self._mr_text(row, task, mr)}\n\n{marker}")
        self.store.action_done(note)

    def _approval_blocked(self, row: ReviewRequest, task: Task, mr: dict) -> str:
        """Why an MR decided ready may not be approved now, or "" when it still may."""
        if not self.enabled:
            return "the code review was switched off"
        st = self.host.mr_status(mr["project_id"], mr["iid"])
        if st.state != "opened":
            return f"it is {st.state} now"
        if st.sha and st.sha != mr["sha"]:
            return f"it has new commits since the review (now at `{st.sha[:12]}`), which nobody reviewed"
        if st.target and st.target != mr["target"]:
            return f"it now merges into {st.target}, not {mr['target']}"
        ci = self._ci(st, mr["sha"])
        if self.rc.require_green_ci and ci != "passed":
            return f"CI {self._ci_words(ci)}"
        return self._ticket_problem(task, json.loads(row.snapshot)) or ""

    def approval_outlived(self, task: Task, project_id, iid, head: str) -> None:
        """The newest finished review approved this MR at a commit it no longer points at: take the approval back,
        once, and say so on the MR. GitLab keeps an approval across pushes unless the project resets approvals on
        push, which not every edition offers; the runner's own approval stands for one commit only."""
        if not head or not getattr(self.host, "supports_approval", False):
            return
        done = [r for r in self.store.reviews("done", task_id=task.task_id) if r.result]
        for row in reversed(done):
            mr = next((m for m in json.loads(row.result).get("mrs", [])
                       if str(m["project_id"]) == str(project_id) and str(m["iid"]) == str(iid)), None)
            if mr is None:
                continue
            approval = self._approval(row, task, mr)
            if not (approval and approval["done"]) or mr["sha"] == head:
                return  # the newest review of this MR gave no approval, or the approval is for this commit
            key = f"{self._keys(row, mr)}:outlived"
            act = self.store.action(key)
            if act and act[0] == "done":
                return
            if not act:
                self.store.action_intent(key, task.task_id, "review-revoke")
            by = approval.get("by") or self.host.reviewer
            revoked = self.host.revoke(mr["project_id"], mr["iid"])
            if revoked or by != self.host.reviewer:
                # An approval given by an account the runner no longer approves as (the approver was changed in the
                # config) cannot be taken back by the runner: the note says so and who has to remove it.
                marker = f"<!-- arceus:{row.id}:{mr['label']}:outlived -->"
                signer = {self.host.reviewer, self.cfg.code_host.bot_user}  # the bot posts it when the approver cannot
                head_line = (f"It was given for commit `{mr['sha'][:12]}`; the merge request now has new commits "
                             f"(head `{head[:12]}`) that nobody reviewed.")
                text = (f"**Code review: the approval was taken back.** {head_line}" if revoked else
                        f"**Code review: the approval by {by} is out of date.** {head_line} The runner now approves "
                        f"as {self.host.reviewer} and cannot take it back: {by}, remove it on the merge request.")
                if not any(marker in n.body and n.author in signer
                           for n in self.host.notes(mr["project_id"], mr["iid"], mr["url"])):
                    self.host.note_review(mr["project_id"], mr["iid"],
                                          f"{text} Write `{self.rc.command}` on the ticket to review them.\n\n{marker}")
                self.events.emit("review_approval_revoked" if revoked else "review_approval_outdated",
                                 task_id=task.task_id, review=row.id, url=mr["url"],
                                 reason="new commits since the review", sha=head, by=by)
            self.store.action_done(key)
            return

    def _approval(self, row: ReviewRequest, task: Task, mr: dict) -> dict | None:
        act = self.store.action(f"{self._keys(row, mr)}:approve")
        return json.loads(act[1]) if act and act[0] == "done" and act[1] else None

    def _mr_outcome(self, row: ReviewRequest, task: Task, mr: dict) -> str:
        if mr["kind"] != "ready":
            return mr["kind"]
        approval = self._approval(row, task, mr)
        if not (approval and approval["done"]):
            return "ready"
        return "attention" if self._attention(mr) else "approved"

    def _always_attention(self, files: list[str]) -> dict[str, str]:
        """Changed files that go to a person whatever the reviewer says (`attention_paths`). Ordinary test code and
        prose are left out of the default list; a list set in config.yaml is taken as it is."""
        if "attention_paths" not in self.rc.model_fields_set:
            skip = attention_matches(files, TEST_PATHS + DOC_PATHS)
            files = [f for f in files if f not in skip]
        return attention_matches(files, self.rc.attention_paths)

    @staticmethod
    def _attention(mr: dict) -> list[tuple[str, str]]:
        """The changes a person has to look at: files matching `attention_paths`, then what the reviewer named."""
        return [(f, f"matches `{p}`") for f, p in mr["risky"].items()] + \
            [(a["file"], a["why"]) for a in (mr["verdict"] or {}).get("attention", []) if a["file"] or a["why"]]

    # -- what people read ---------------------------------------------------------------------------

    def _mr_text(self, row: ReviewRequest, task: Task, mr: dict) -> str:
        v = mr["verdict"] or {}
        outcome = self._mr_outcome(row, task, mr)
        approval = self._approval(row, task, mr)
        head = {
            "approved": "**Code review: ready to merge.**",
            "ready": "**Code review: no problems found, but not approved.**",
            "attention": "**Code review: approved, but a person needs to look at some of its changes before merge.**",
            "changes": "**Code review: changes are needed before merge.**",
            "unverified": "**Code review: this merge request could not be confirmed.**",
        }[outcome]
        why = (f"again after the agent's fixes for {_auto_of(row)}"
               if row.comment_id.startswith(AUTO_PREFIX) else f"as {row.author} asked on the ticket")
        lines = [head, "", f"Reviewed at commit `{mr['sha'][:12]}` against {task.task_id}, {why}.", ""]
        if v:
            met = {True: "met", False: "not met", None: "not judged"}[v["requirements_met"]]
            lines += [f"- Requirements of the ticket: {met}",
                      f"- Build: {v['build'].replace('_', ' ')}",
                      f"- Tests: {'none in the repository' if v['tests'] == 'none' else v['tests'].replace('_', ' ')}"]
            if v["checked"]:
                lines.append(f"- What was run: {v['checked']}")
        lines += [f"- CI: {self._ci_words(mr['ci'])}", ""]
        blockers = [f for f in v.get("findings", []) if f["severity"] == "blocker"]
        suggestions = [f for f in v.get("findings", []) if f["severity"] == "suggestion"]
        attention = self._attention(mr)
        if outcome == "approved":
            lines.append("Approved. This merge request is ready to merge and needs no further review.")
        elif outcome == "ready":
            why = list(mr["reasons"])
            if approval and not approval["done"]:
                why.append(approval["detail"] or f"{self.host.label} did not take the approval")
            lines += ["Not approved:", *(f"- {r}" for r in why)]
        elif outcome == "attention":
            lines += ["Approved: the review found nothing that has to change. Before merge, a person has to decide "
                      "on or act on these:",
                      *(f"- `{f}`" + (f": {why}" if why else "") for f, why in attention)]
        elif outcome == "changes":
            lines += ["What has to change:", *(f"- {self._finding(f)}" for f in blockers)]
            if v and v["requirements_met"] is False and not blockers:
                lines.append("- It does not do what the ticket asks.")
            if v and "failed" in (v["build"], v["tests"]):
                lines.append(f"- The {'build' if v['build'] == 'failed' else 'tests'} failed.")
            lines += ["", "Not approved. " + self._sent_back_words(row, task)]
        else:
            lines += ["Why:", *(f"- {r}" for r in mr["reasons"] or ["the review did not finish"]), "",
                      f"Not approved. Write `{self.rc.command}` on the ticket to review it again."]
        if outcome == "changes" and mr["reasons"]:
            lines += ["", *(f"Note: {r}." for r in mr["reasons"])]
        if attention and outcome in ("ready", "changes", "unverified"):
            lines += ["", "Also for a person to look at:", *(f"- `{f}`" + (f": {why}" if why else "")
                                                               for f, why in attention)]
        deploy = [(d["file"], d["what"]) for d in v.get("deploy", [])]
        if deploy:
            lines += ["", "For whoever deploys it (no decision needed before merge):",
                      *(f"- `{f}`: {what}" if f else f"- {what}" for f, what in deploy)]
        if suggestions:
            lines += ["", "Suggestions (not blocking):", *(f"- {self._finding(f)}" for f in suggestions)]
        return "\n".join(lines)[:60000]

    @staticmethod
    def _finding(f: dict) -> str:
        where = f["file"] + (f":{f['line']}" if f["file"] and f["line"] else "")
        return (f"`{where}`: " if where else "") + f["text"].replace("\n", " ")

    def _ticket_text(self, row: ReviewRequest, task: Task, result: dict, overall: str) -> str:
        who = self.tracker.mention(row.author)
        if result["off"]:
            given = [mr["url"] for mr in result["mrs"] if (self._approval(row, task, mr) or {}).get("done")]
            if given:
                return (f"{who}, the code review was switched off before it finished; nothing more was posted on "
                        "the merge requests. Approved before that: " + ", ".join(given) + ".")
            return (f"{who}, the code review was switched off while it ran; nothing was posted on the merge "
                    "requests and nothing was approved.")
        words = {"approved": "ready to merge, approved; no further review is needed",
                 "ready": "no problems found, but not approved (the merge request says why)",
                 "attention": "approved, but a person needs to look at some of its changes before merge "
                              "(the merge request lists them)",
                 "changes": "changes are needed before merge",
                 "unverified": "the review could not confirm it",
                 "closed": "merged or closed while the review ran; nothing posted there"}
        lines = [f"{who}, code review of the merge requests:", ""]
        for mr in result["mrs"]:
            lines.append(f"- {mr['repo']}: {words[self._mr_outcome(row, task, mr) if mr['kind'] != 'closed' else 'closed']}"
                         f". {mr['url']}")
        if result["summary"]:
            lines += ["", result["summary"]]
        if result["problems"] and overall in ("unverified", "nothing"):
            lines += ["", "What got in the way: " + "; ".join(result["problems"][:5]) + "."]
        if overall == "changes":
            lines += ["", self._sent_back_words(row, task)]
        elif overall == "unverified":
            lines += ["", f"Write `{self.rc.command}` again to repeat the review."]
        return "\n".join(lines)

    def _compose(self, row: ReviewRequest, task: Task, issue: IssueDetails, comments: list[Comment],
                 mrs: list[dict]) -> str:
        """The reviewer's brief: the ticket whole, the MRs and how to answer. The same inputs give the same
        text, so a repeat is the same message for the engine and the same text for the screen."""
        bot = self.cfg.tracker.bot_user
        ws = self.cfg.paths.workspace.resolve()
        lines = [f"Code review of the merge requests of ticket {task.task_id}, asked for by {row.author}"
                 + (f" (again, on the fixes the author pushed for {_auto_of(row)})"
                    if row.comment_id.startswith(AUTO_PREFIX) else "") + ".", "",
                 "You are the reviewer. Another agent wrote this code; you check it and report what you find. You do "
                 "not fix anything: do not edit files, do not commit, do not push. Do not ask anyone a question: "
                 "nobody answers during this run. When something is unclear, decide what you can, say what you "
                 "assumed, and mark that merge request \"unsure\" instead of stopping.", ""]
        if row.request:
            lines += [f"What {row.author} asked of the review: {row.request}", ""]
        lines += ["## The ticket", "", f"{issue.key}: {issue.summary}", "", issue.description.strip() or "(no description)",
                  "", "## Comments on the ticket, oldest first", ""]
        shown = [x for x in comments if x.text.strip()]
        for x in shown:
            who = "Arceus (the runner)" if x.author == bot else x.author
            body = _TRACKER_MARK.sub("", x.text).strip().replace("\n", "\n  ")
            lines.append(f"- {who}, {_when(x.created)}: {body}")
        if not shown:
            lines.append("(none)")
        lines += ["", "The requirements are the description as the later comments changed or narrowed it.", "",
                  "## Merge requests to review", ""]
        for m in mrs:
            lines += [f"### {m['label']} · {m['repo']}", m["url"],
                      f"Branch {m['source']} into {m['target']}, at commit {m['sha']}. In the workspace, "
                      f"`{ws / m['repo']}` is checked out at that commit; the change is "
                      f"`git diff origin/{m['target']}...HEAD` there ({len(m['files'])} files).", ""]
            always = self._always_attention(m["files"])
            hints = [f for f in attention_matches(m["files"], self.rc.review_hint_paths) if f not in always]
            if always:
                lines += ["Listed for a person by the runner in any case (security, secrets), so do not repeat "
                          "them in `attention` unless you have something to add: "
                          + ", ".join(f"`{f}`" for f in always) + ".", ""]
            if hints:
                lines += ["Read these with extra care (build, CI, dependencies, configuration, migrations); they go to `attention` "
                          "or `deploy` only by the rules of point 3: " + ", ".join(f"`{f}`" for f in hints) + ".", ""]
        lines += [f"Why the author chose what it did, if it wrote it down: {decisions_file(ws, task.task_id)}.", "",
                  "## What to check in each merge request", "",
                  "1. Requirements. Does the change do what the ticket asks, all of it, and nothing it did not ask "
                  "for? Missing or wrong behaviour is a blocker.",
                  "2. Build and tests. Build the project and run its tests the way the repository does it (README, "
                  "Makefile, verify.sh, CI config). Say what you ran. A repository without tests is \"none\", not "
                  "\"passed\". If something cannot run here (a service, a secret, the network), say so and use "
                  "\"not_run\". Never report a result you did not see.",
                  "3. What a person has to decide before merge, and what whoever deploys it has to know. `attention` "
                  "stops the merge until a person looks, so it holds only a decision you cannot settle yourself by "
                  "reading and running the code:",
                  "   - a change of substance to a security boundary: who can do what, authentication, sessions, "
                  "secrets or credentials, input that reaches SQL, a shell or HTML;",
                  "   - a CI job or check that was removed, weakened or bypassed; privileges, exposed ports or hosts "
                  "that were widened;",
                  "   - a migration or start-up step that drops, renames, rewrites or narrows existing data or columns, "
                  "or a choice someone has to make about existing data (whose records these become, what a backfill "
                  "should write). One that only adds tables, columns or indexes is `deploy` (say: back up first);",
                  "   - behaviour other systems rely on (an API's status codes, fields or URLs) changed in a way the "
                  "ticket did not ask for or rules out;",
                  "   - a product decision the ticket left open, made in the change, that is hard to reverse later.",
                  "   `deploy` is for what the people who release it should know or do, when the change itself is right "
                  "as the ticket asked: an environment variable or proxy setting to set, a runtime, framework or "
                  "library upgrade the ticket asked for, a new dependency, a schema change applied at start-up that "
                  "only adds, a new health check or file the service writes, a version that is out of upstream "
                  "support and wants a follow-up. Each item says in `what` what to set, check or plan. It does not "
                  "stop the merge.",
                  "   Neither list is for changes the ticket asked for that you verified and that fit none of the "
                  "above: routine build or test tooling (a `make verify` target, test scripts, e2e setup), lock files, "
                  "styling and layout, refactors the tests cover, a development-only setting. Describe those in "
                  "`summary` if they matter; what should be better goes to `findings`. Not `attention` either: a change "
                  "that is correct and tested but sits on a hot or shared path (\"every request goes through it\", "
                  "\"a person should understand it\", \"recheck it on the next upgrade\"); a CI change that adds or "
                  "tightens a check; a fix that turns a 500 into a proper answer or closes an injection; a status, "
                  "schema or format change the ticket's own upgrade or feature brings. A missing safeguard (no foreign "
                  "key, no retention) is a finding. A known defect is a blocker, "
                  "not `attention`; something you could not check makes the verdict \"unsure\", not `attention`. "
                  "Each `attention` item's `why` is the question a person must answer before merge (\"Who should own "
                  "the existing links?\"); what you cannot put as such a question is `deploy`, `summary` or a "
                  "finding. Most merge requests have no "
                  "`attention` at all: when in doubt between the two lists, it is `deploy`.",
                  f"4. Improvements. Only what matters: bugs, missing tests for new behaviour, a clear maintainability "
                  f"problem. Style preferences are not findings. At most {self.rc.max_suggestions} suggestions per "
                  "merge request.", "",
                  "A blocker is something that must change before merge: wrong or missing behaviour, or a test the "
                  "ticket requires that is missing entirely. Tests that exist and pass but could be stronger are a "
                  "suggestion. Everything else is a suggestion and does not stop the merge.", "",
                  "## Your verdict", "",
                  f"Write it to {self._verdict_file(row)} as JSON, one entry per merge request:", "", "```json",
                  SCHEMA_EXAMPLE, "```", "",
                  "- `verdict`: \"ready\" (meets the ticket, builds, tests pass, no blocker), \"changes\" (at least one "
                  "blocker), or \"unsure\" (you could not tell).",
                  "- `requirements_met`: true or false. `build` (passed, failed, not_run) and `tests` (passed, failed, "
                  "not_run, none): what you saw yourself.",
                  "- `checked`: the commands you ran and what they showed, in a line or two.",
                  "- `findings`: `severity` is \"blocker\" or \"suggestion\"; `file` and `line` when it is about a "
                  "place in the code.",
                  "- `attention`: what a person has to decide before merge, by point 3, each `why` the question they "
                  "must answer; an empty list when there is nothing.",
                  "- `deploy`: what whoever deploys it should know or do, by point 3; an empty list when there is "
                  "nothing.",
                  "- `summary`: a few sentences for the ticket.",
                  "",
                  f"Write in English. The runner reads the file and posts the result; you cannot post on "
                  f"{self.tracker.label} or {self.host.label} yourself. Then finish with the outcome "
                  "succeeded_research and the same summary. Never end with needs_input or blocked."]
        return "\n".join(lines)
