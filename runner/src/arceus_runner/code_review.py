"""The review agent: a person asks for a code review on the ticket, and a separate Bulava task does it.

Off unless `review_agent.enabled`; with it off, nothing here runs and every comment goes where it went
before. With it on, for a ticket whose task has merge requests, or a ticket in Code Review whose merge requests
people wrote:

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
- **Merge requests people wrote.** A request on a ticket in Code Review that the runner never worked on makes it
  a task of kind `review` (Task.people_work): the runner reviews the merge requests the ticket links to (its
  description and people's comments) that are in the code host's scope, each at the head the host has for it
  (also one from a fork), and says which links it did not review and why. Nothing goes to an author: there is
  no agent session behind the code, so there is no fix loop, and the ticket is not in the runner's other
  work (answers, `/continue`, the channel). The runner lets go of it once the ticket leaves Code Review or none
  of its merge requests is open; a later request takes it up again. Given to the agent later (the queue tag,
  To Do), it becomes an ordinary task, its reviews kept.
- **The workspace.** From the switch to the end of the run it holds the shared workspace like a task does
  (db.REVIEW_HOLDS): nothing else changes a branch there, cleanup counts it as the ticket's work. Then the
  runner stashes what the run left, checks every repository out where it was and frees the workspace. A
  crash at any point resumes from the stored state.
- **The runner decides, the model advises.** An MR is approved only when the reviewer found it ready
  (requirements met, build passed, tests passed or fail only as they did on the target branch without the
  change, no blocker, what people see looked at or unchanged, and no finding of fit while the ticket has fix
  rounds left), the code host's CI passed on the reviewed
  commit (`require_green_ci`), the MR still points at that commit and the same target, the ticket is
  still in Code Review with the text the review read, and the feature is still on. The approver account
  then approves exactly that commit, and the MR gets a note that the review found nothing that has to
  change before merge. When a changed file matches `attention_paths` (the security boundary, secrets) or the
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

import fnmatch
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from arceus_runner import materials
from arceus_runner.config import DOC_PATHS, TEST_PATHS, RepoChecks
from arceus_runner.db import ACTIVE_STATES, REVIEW_UNDER_WAY, ReviewRequest, ReviewStore, Task
from arceus_runner.texts import ProjectNotesError
from arceus_runner.engine import EngineError, ReportReview
from arceus_runner.gitops import HOME_BRANCH, GitError, restore_workspace
from arceus_runner.http import AuthError, NotFound, ServiceError
from arceus_runner.integrations import Approval, Comment, Issue, IssueDetails, MRStatus, Note, key_part
from arceus_runner.replies import decisions_file, unattributed
from arceus_runner.screening import is_decision, trusted
from arceus_runner.ticket_progress import SEPARATOR as PROGRESS_SEPARATOR

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
# What the engine's Codex judges the reviewer's closing report by, in place of the server's rule about data changes: a
# review builds the MRs and runs their tests, which make test databases and containers by their nature, and the server's
# rule had Codex ask for an account of every table and record they made, round after round, until the review ended
# unverified. The reviewer is given the same text, so it writes to it from the start.
REPORT_CRITERIA = """\
This is the closing report of a code review: a reviewer read one or more merge requests against their ticket, built
them, ran their tests and checks, and wrote a verdict for each merge request. The report (the outcome's summary) puts
that verdict in words. The review changed no code. Judge the report by these points, and only these:

1. Each verdict follows from what the reviewer checked. "ready" needs the ticket's requirements met and the build and
   tests run and passing; a requirement not met, a failing test or a defect found rules it out. A verdict that the
   code or the reviewer's logs contradict is grounds for FAIL.
2. Every claim that something was run, passed or failed names the command and what it showed, or the file and line.
   A check that could not be run is said to be so, never called passed. What people see counts as looked at only
   when the report names the suites or the screens and states it looked at; otherwise it was not looked at.
3. A finding points to the place in the code it is about and says what is wrong there. A blocker also names the
   requirement or the working behaviour it breaks and how ordinary use reaches it; one that asks for more than the
   ticket and the people's comments do, or that only made-up input reaches, is grounds for FAIL. A "fit" finding
   names what it stands on: a rule quoted from the project's notes or a repository's CLAUDE.md or AGENTS.md, the
   existing code that already does the same job, the code in the change that does nothing or goes the long way
   round, or the change no line of the ticket or the people's comments asks for. It asks to remove the change or
   bring it in line with what is there, never to add to it; one that stands on nothing it names, or that asks for
   more than the ticket, is grounds for FAIL. A suggestion is not grounds for FAIL.
4. The report claims no more than the checks show, and the numbers it gives agree with what it lists.
5. Data and state the checks made in throwaway places (a test database, a temporary file, a container started for
   the tests) are expected: naming where they ran is enough. Counting the records or tables they made, recovering
   their contents or proving each one gone is not asked for. A change to the repositories under review, a push, or
   anything outside the machine (a real service, a shared database) is grounds for FAIL.

Do not fail the report for its style, for exact counts that do not bear on the verdict, or for checks the ticket did
not need."""
UNBACKED = ("the reviewer asks for changes without naming, for each, the requirement it breaks and how ordinary use "
            "reaches it, so a person decides whether they are needed")
REPORT_ROUNDS = 5
# On a server Codex checks the reviewer's closing report by REPORT_CRITERIA (the engine's report review, at its own
# round limit). When it still disputes the report at that limit, the run ends `failed`/`needs-user` (`--report-at-limit
# ask`) and nothing in it can be approved. The
# reviewer gets the objections back this many times before the review is settled as it stands: a report fixed, or a
# verdict changed, that Codex then passes counts like any other; one it still disputes is never approved.
REPORT_REPAIRS = 1
MAX_OBJECTIONS = 6000
REPORT_RETURNED = (
    "Codex checked your closing report against the code and your logs and did not accept it. Its objections are "
    "below. Answer each one: correct the report where it is wrong or unsupported; when an objection shows that your "
    "verdict itself is wrong (a requirement not met, a test that does not pass, a defect), change the verdict file "
    "too. State only what you verified, each claim with its evidence, and for anything you ran that changed data or "
    "state, where it ran. Then finish with the outcome succeeded_research again.\n\n"
    "## Codex's objections\n\n")
_REPORT_FAIL = re.compile(r"^\s*VERDICT:\s*FAIL\b", re.MULTILINE)
MAX_FILE_BYTES = 512 * 1024
MAX_TEXT = 3000
MAX_ITEMS = 30
VERDICTS = ("ready", "changes", "unsure")
# Whether the reviewer looked at what people see: "none" when the change alters nothing a person sees or operates.
UI = ("checked", "not_run", "none")
# What a "fit" finding stands on, so that it is the project's way and not the reviewer's taste: a rule written in the
# project's notes or a repository's CLAUDE.md / AGENTS.md, existing code that already does the same job, code in the
# change that does nothing or goes the long way round, a change nobody asked for.
FIT_BASES = ("rule", "existing", "needless", "unasked")
UNSEEN = "it changes what people see, and the reviewer could not look at it"
NO_UI = "the reviewer did not say whether it changes what people see and whether it looked"
FIT_WORDS = {"rule": "the project's rule", "existing": "already done by", "needless": "plainly",
             "unasked": "asked for by nobody"}
UI_WORDS = {"checked": "looked at", "not_run": "not looked at", "none": "nothing changed"}
TEST_WORDS = {"none": "none in the repository",
              "failed_before": "some fail, the same ones as on the target branch without this change, so not because "
                               "of it (see what was run)"}
BUILD = ("passed", "failed", "not_run")
# failed_before: tests fail, and the same ones fail on the target branch without the change: not the MR's doing.
TESTS = ("passed", "failed", "failed_before", "not_run", "none")
# How the runner judges a verdict. A final review stands for its commits only under the rules it was judged by: after
# a change of them (2: tests that failed before the change no longer stop it; 3: each repository's build and tests are
# judged by its own review_agent.repos; 5: a change people see is not approved unseen, and "fit" findings go back to
# the author), the same commits may be reviewed again.
RULES = 5
_MARKER = re.compile(r"<!--\s*arceus", re.IGNORECASE)
_TRACKER_MARK = re.compile(r"\s*\[arceus:[^\]]*\]\s*$")
_WORD = re.compile(r"[\w-]")
_REVIEW_WORD = re.compile(r"(?:code[ -]?)?review\b", re.IGNORECASE)
# A link to a merge request (GitLab), a pull request (GitHub, Gitea) or a Bitbucket one, wherever it points.
_MR_LINK = re.compile(r"https?://[^\s<>()\[\]{}\"'`|]+?/(?:merge_requests|pull|pull-requests)/\d+", re.IGNORECASE)

SCHEMA_EXAMPLE = """\
{"summary": "Two or three sentences for the ticket.",
 "mrs": [{"mr": "M1", "verdict": "ready", "requirements_met": true, "build": "passed", "tests": "passed",
          "checked": "make verify: 132 tests passed", "ui": "checked", "ui_checked": "…",
          "scope": [{"what": "…", "asked_by": "AC2: …"}, {"what": "…", "existing": "app/validators.py:12"}],
          "findings": [{"severity": "blocker", "file": "app/login.py", "line": 40, "text": "…",
                        "breaks": "…", "reached_by": "…"},
                       {"severity": "fit", "file": "app/forms.py", "line": 30, "text": "…",
                        "basis": "existing", "evidence": "app/validators.py:12 validate_postcode"},
                       {"severity": "suggestion", "file": "app/forms.py", "line": 12, "text": "…"}],
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


def path_glob(pattern: str) -> re.Pattern:
    """A path pattern as a regular expression: `**` crosses folders, `*` and `?` do not; case is ignored."""
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
    compiled = [(p, path_glob(p)) for p in patterns]
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
    if len(text) <= limit:
        return text
    # Cut at the last whole word, so a name in the text is never left half there; a text with no space in its
    # second half (a path, a long identifier) is cut where the limit falls.
    head = text[:limit]
    if not text[limit].isspace():
        space = max(head.rfind(" "), head.rfind("\n"))
        if space > limit // 2:
            head = head[:space]
    return head.rstrip() + " …"


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
            # A blocker counts only with what it breaks and how ordinary use reaches it. Without both, or with an
            # unknown severity, it is "unsupported": not a pass, and not a fix for the agent either.
            breaks, reached_by = _clean(f.get("breaks"), 800), _clean(f.get("reached_by"), 800)
            # A "fit" finding goes to the author only with what it stands on; without it, it is the reviewer's taste
            # and reads as a suggestion.
            basis = f.get("basis") if f.get("basis") in FIT_BASES else ""
            evidence = _clean(f.get("evidence"), 800) if basis else ""
            at = f.get("line")
            fit = f.get("severity") == "fit" and bool(_clean(f.get("file"), 300)) and _backed(basis, evidence) \
                and isinstance(at, int) and not isinstance(at, bool) and at > 0
            if f.get("severity") == "suggestion" or f.get("severity") == "fit" and not fit:
                severity = "suggestion"
                suggestions += 1
                if suggestions > max_suggestions:
                    continue
            elif fit:
                severity = "fit"
            elif f.get("severity") == "blocker" and breaks and reached_by:
                severity = "blocker"
            else:
                severity = "unsupported"
            line = f.get("line") if isinstance(f.get("line"), int) and not isinstance(f.get("line"), bool) else None
            findings.append({"severity": severity, "text": _clean(f.get("text"), 1500),
                             "file": _clean(f.get("file"), 300), "line": line,
                             "breaks": breaks, "reached_by": reached_by} |
                            ({"basis": basis, "evidence": evidence} if severity == "fit" else {}))
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
        scope = [{"what": _clean(x.get("what"), 400), "asked_by": _clean(x.get("asked_by"), 400),
                  "existing": _clean(x.get("existing"), 400)}
                 for x in (e.get("scope") if isinstance(e.get("scope"), list) else []) if isinstance(x, dict)]
        ui = e.get("ui") if e.get("ui") in UI else None
        if ui is None:
            doubts.append(NO_UI)
        elif ui == "checked" and not _clean(e.get("ui_checked")):
            doubts.append("the reviewer says it looked at what people see, but not at what")
        out[label] = {
            "verdict": e.get("verdict") if e.get("verdict") in VERDICTS else "unsure",
            "requirements_met": met if isinstance(met, bool) else None,
            "build": e.get("build") if e.get("build") in BUILD else "not_run",
            "tests": e.get("tests") if e.get("tests") in TESTS else "not_run",
            # The commands and what they showed: a careful review lists many, and a note has room for them.
            "checked": _clean(e.get("checked")),
            "unsure_because": _clean(e.get("unsure_because"), 800),
            "ui": ui or "not_run",
            "ui_checked": _clean(e.get("ui_checked"), 1500),
            "scope": scope[:MAX_ITEMS * 2],
            "findings": findings[:MAX_ITEMS],
            "attention": attention[:MAX_ITEMS],
            "deploy": deploy[:MAX_ITEMS],
            "doubts": list(dict.fromkeys(doubts)),
        }
    return out, _clean(data.get("summary")), problems


def _in_change(f: dict, mr: dict, near: int = 3) -> bool:
    """Whether a finding points at a line the change adds or changes (or a few lines from it), in a file it touches.
    A review read before the changed lines were kept is judged by the file alone."""
    if f["file"] not in mr["files"]:
        return False
    if "lines" not in mr:
        return True
    return f["line"] is not None and any(a - near <= f["line"] <= b + near for a, b in mr["lines"].get(f["file"], []))


def _fit_key(mr: dict, f: dict) -> tuple:
    """A finding of fit as the same one in a later review: its merge request and target, where it is and what kind,
    not how it is worded."""
    return mr["repo"], str(mr.get("iid")), mr.get("target"), f.get("file"), f.get("line"), f.get("basis")


def _judged(snap: dict) -> tuple:
    """What a review judged: the commits, and the ticket and people's comments it read them against."""
    return ({(m["repo"], str(m.get("iid")), m["sha"], m.get("target"), m.get("target_sha")) for m in snap.get("mrs", [])},
            snap.get("ticket", {}).get("context"), snap.get("mr_comments"))


_PLACE = re.compile(r"[\w./-]*\w:[1-9]\d*\b")
_QUOTE = re.compile(r"[\"“”'`«»]")
# Something in the code named: a `quoted` name, a path, a call, a snake_case or dotted name.
_CODE = re.compile(r"`[^`]+`|[\w-]+/[\w./-]+|\w+\(|\b\w+_\w+\b|\b\w+\.\w+\b")


def _backed(basis: str, evidence: str) -> bool:
    """Whether a finding of fit's evidence has the shape its basis needs: a rule quoted with the file it is in, the
    place of the existing code, or the plain form or the change beyond the ticket in words."""
    if basis == "rule":
        return bool(_quoted(evidence)) and bool(re.search(r"[\w-]+\.(md|txt|rst|toml|ya?ml|cfg)\b|/", evidence))
    if basis == "existing":
        return bool(_PLACE.search(evidence))
    return bool(_CODE.search(evidence)) and len(evidence.split()) >= 3


_QUOTED = re.compile(r'"([^"]+)"|“([^”]+)”|«([^»]+)»|`([^`]+)`')


def _plain(text: str) -> str:
    """Text as a rule is compared: markdown marks and line breaks gone, one space between words, lower case."""
    return " ".join(re.sub(r"[*_#>`]", " ", text).split()).lower()


def _quoted(evidence: str) -> str:
    """The longest quote of four words or more in the evidence, as `_plain` spells it; "" when there is none."""
    quotes = [_plain(next(g for g in m.groups() if g)) for m in _QUOTED.finditer(evidence)]
    quotes = [q for q in quotes if len(q.split()) >= 4]
    return max(quotes, key=len) if quotes else ""


def _fit_only(mr: dict) -> bool:
    """A call for changes made only of findings of fit: nothing is broken, nothing failed."""
    v = mr.get("verdict") or {}
    return "failed" not in (v.get("build"), v.get("tests")) \
        and not any(f.get("severity") == "blocker" for f in v.get("findings", []))


def _said(mr_comments: list[tuple[str, Note]]) -> str:
    """What people wrote on the merge requests, as one digest: an edit or a deletion counts."""
    return hashlib.sha256(json.dumps([[repo, str(n.id), n.author, n.body] for repo, n in mr_comments])
                          .encode()).hexdigest()


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
        self._read: dict[str, int] = {}  # ticket people worked on: when it had changed as its comments were read

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

    def _mr_comments(self, mrs: list[dict]) -> list[tuple[str, Note]]:
        """What people wrote on the merge requests under review, oldest first, with each one's repository: a decision
        made there narrows the ticket as much as one on the ticket. The runner's notes and the review's are left out."""
        ours = {self.cfg.code_host.bot_user, self.host.reviewer}
        said = [(m["repo"], n) for m in mrs for n in self.host.notes(m["project_id"], m["iid"], m["url"])
                if not n.system and n.author not in ours and n.body.strip()]
        return sorted(said, key=lambda x: x[1].created_at)

    def _context(self, task: Task, issue: IssueDetails) -> str:
        """What people asked for, as one digest: the summary, the description and every comment people wrote
        (by id, author and text, so an edit or a deletion counts), the requests to the reviewer left out."""
        bot = self.cfg.tracker.bot_user
        people = [[str(x.id), x.author, x.text] for x in self._ticket_comments(task)
                  if x.author != bot and self.request(x.text) is None]
        # The files people attached count too: a mockup replaced or removed is a changed requirement.
        files = [[a.id, a.name, a.size, a.created, a.comment_id] for a in self._attachments(task) if a.author != bot]
        return hashlib.sha256(json.dumps([_digest(issue), people, *([files] if files else [])]).encode()).hexdigest()

    def _requirements(self, task: Task, issue: IssueDetails) -> str:
        """What the ticket asks for, without the conversation: the summary, the description and the files people
        attached. An approval outlives a comment ("merged, thanks"), not a change of these."""
        bot = self.cfg.tracker.bot_user
        files = [[a.id, a.name, a.size, a.created, a.comment_id] for a in self._attachments(task) if a.author != bot]
        return hashlib.sha256(json.dumps([_digest(issue), files]).encode()).hexdigest()

    def _attachments(self, task: Task) -> list:
        return self.tracker.attachments(task.issue_db_id) if getattr(self.tracker, "reads_attachments", False) else []

    def _files(self, task: Task, comments: list[Comment]) -> list[materials.Material]:
        """The ticket's files for the reviewer, as the author got them at the start."""
        bot = self.cfg.tracker.bot_user
        return self.d.ticket_files(task, None, [x for x in comments if x.author != bot])

    def _ticket_problem(self, task: Task, snap: dict) -> str | None:
        """Why the ticket no longer backs the review's verdict, or None."""
        c = self.cfg.tracker
        issue = self.tracker.issue(task.task_id)
        if issue.state != c.states.code_review:
            return f"the ticket is no longer in {c.states.code_review} (it is in {issue.state or 'no state'})"
        return self._context_problem(task, snap, issue)

    def _context_problem(self, task: Task, snap: dict, issue: IssueDetails | None = None) -> str | None:
        """What people asked for changed while the review ran, so its verdict answers an older question; or None."""
        issue = issue or self.tracker.issue(task.task_id)
        if self._context(task, issue) != snap["ticket"]["context"]:
            return "the ticket's description or comments changed while the review ran"
        if "mr_comments" in snap and _said(self._mr_comments(snap["mrs"])) != snap["mr_comments"]:
            return "people commented on the merge requests while the review ran"
        return None

    def _verdict_file(self, row: ReviewRequest) -> Path:
        return self.cfg.paths.workspace.resolve() / REVIEWS_DIR / row.task_id / f"{row.id}.json"

    def _extra_dirs(self, row: ReviewRequest) -> Path:
        return self.cfg.paths.db.parent / "tasks" / row.id / "extra-dirs.txt"

    def _report_review(self, row: ReviewRequest) -> ReportReview:
        """How the engine's Codex reviews the closing report: by REPORT_CRITERIA, and still disputed at the last
        round, `ask` (failed / needs-user), so that nothing it disputes is approved (_return_report). The criteria
        go beside the extra directories, outside the workspace; the engine copies them when it takes the task."""
        path = self._extra_dirs(row).parent / "report-criteria.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(REPORT_CRITERIA, encoding="utf-8")
        tmp.replace(path)
        return ReportReview(rounds=REPORT_ROUNDS, criteria=path, at_limit="ask")

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
            if task.people_work:
                continue  # read below, with the other tickets in Code Review
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
        self._people_requests()

    def _people_requests(self) -> None:
        """Requests on tickets in Code Review the runner did not work on: people wrote their merge requests. A
        ticket's comments are read only when it changed since they were last read (and once after a start). A
        request older than `limits.continue_window` is left alone: a person has answered it by now."""
        find = getattr(self.tracker, "in_state", None)
        if find is None or getattr(self.host, "mr_link", None) is None:
            return  # the tracker cannot list a state, or the host cannot read a link: only the runner's own MRs
        c = self.cfg.tracker
        oldest = int((self.d.now() - self.cfg.limits.continue_window).timestamp() * 1000)
        try:
            for issue in find(c.states.code_review):
                task = self.store.task(issue.key)
                if task is not None and not task.people_work:
                    continue  # the runner's own ticket: read above, with its task
                if self._read.get(issue.key) == issue.updated:
                    continue
                for x in self.tracker.comments(issue.db_id):
                    if x.author == c.bot_user or x.created < oldest:
                        continue
                    asked = self.request(x.text)
                    if asked is None or (task and self.store.review_of_comment(task.task_id, x.id)):
                        continue
                    task = self._people_task(issue, task, x)
                    self._record(task, x, asked)
                self._read[issue.key] = issue.updated
        except ServiceError as e:
            self.events.emit("reconcile_failed", task_id=None, reason=str(e))

    def _people_task(self, issue: Issue, task: Task | None, x: Comment) -> Task:
        """The `review` task of a ticket people worked on: made at its first request, taken up again by a later one."""
        if task is None:
            task = self.store.create_task(
                task_id=issue.key, issue_db_id=issue.db_id, state="code_review", summary=issue.summary[:300],
                tag_author=x.author, repo="", repo_path=str(self.cfg.paths.workspace), branch="", base_branch="",
                question_id=None, question_at_ms=None, reason=None, mr_urls="[]", attempt_started=None,
                extra_repos="[]", kind="review")
            self.events.emit("review_ticket_watched", task_id=task.task_id, author=x.author)
        elif task.state == "done":
            task = self.store.update_task(task.task_id, state="code_review", reason=None)
            self.events.emit("review_ticket_watched", task_id=task.task_id, author=x.author, again=True)
        return task

    def watch(self, task: Task) -> None:
        """A ticket whose merge requests people wrote, on the code host's poll: an approval that no longer holds is
        taken back, a merge the review did not let through is reported, and the runner lets go of the ticket once no review of it is under way and it has left Code
        Review or none of its merge requests is open."""
        opened = False
        merged = []
        for m in self.store.task_mrs(task.task_id):
            st = self.host.mr_status(m.project_id, m.iid)
            if st.state != m.state:
                self.store.update_mr(task.task_id, m.repo, state=st.state)
            if st.state == "opened":
                opened = True
                self.approval_outlived(task, m.project_id, m.iid, st)
            elif st.state == "merged":
                merged.append(m)
                self.merged_check(task, m.project_id, m.iid, st)
        if self.store.reviews(*REVIEW_UNDER_WAY, task_id=task.task_id):
            return
        state = self.tracker.issue(task.task_id).state
        in_review = state == self.cfg.tracker.states.code_review
        if opened and in_review:
            return
        why = "no merge request of it is open" if in_review else f"the ticket is in {state or 'no state'}"
        keys = []
        with self.store.transaction():
            self.store.update_task(task.task_id, state="done", reason=why)
            if in_review and merged and len(merged) == len(self.store.task_mrs(task.task_id)):
                # Every MR merged while the ticket waited in Code Review: tracker.tags_on.merged, as for the agent's
                # own work (the runner never moves a ticket people worked on). One closed without merge, or a ticket
                # a person moved on, gets none. The merged MRs tell a later round of the same ticket from this one.
                keys = self.d.journal_tags_on(task, "merged", "-".join(
                    f"{key_part(m.project_id)}.{key_part(m.iid)}" for m in sorted(merged, key=lambda m: m.repo)))
        self.events.emit("review_ticket_released", task_id=task.task_id, reason=why)
        self.d.apply_tags_on(task, keys)

    def _people_mrs(self, task: Task, issue: IssueDetails, comments: list[Comment]) -> list[dict]:
        """The merge requests a ticket people worked on links to, kept as its task's; the links that are not
        reviewed come back with why. The description and people's comments count, the runner's own do not."""
        bot = self.cfg.tracker.bot_user
        group = self.d.workspace.group()
        found: dict[str, tuple] = {}  # repository: (its Repo, the MR's number, the link, the MR's state)
        skipped: list[dict] = []
        texts = [issue.description, *(x.text for x in comments if x.author != bot)]
        for url in dict.fromkeys(_MR_LINK.findall("\n".join(texts))):
            hit = self.host.mr_link(url, group)
            if hit is None:
                skipped.append({"url": url, "why": f"it is not a merge request of the {self.host.label} "
                                                   f"{self.host.scope_kind} {self.host.scope}"})
                continue
            repo, iid = hit
            try:
                st = self.host.mr_status(repo.id, iid)
            except (NotFound, AuthError):
                skipped.append({"url": url, "why": f"{self.host.label} does not show it to the bot (deleted, or no "
                                                   "access)"})
                continue
            mr = (repo, iid, url, st.state)
            other = found.get(repo.relative_path)
            if other and str(other[1]) != str(iid):
                # One head per repository can be checked out at a time: the first open one is reviewed.
                keep, drop = (mr, other) if other[3] != "opened" and st.state == "opened" else (other, mr)
                found[repo.relative_path] = keep
                if drop[3] == "opened":
                    skipped.append({"url": drop[2], "why": f"{keep[2]} in the same repository is reviewed this "
                                                           "time; ask again once that one is merged or closed"})
                continue
            found[repo.relative_path] = mr
        with self.store.transaction():
            self.store.drop_mrs(task.task_id, keep=set(found))
            for path, (repo, iid, url, _) in found.items():
                # Nothing of it was pushed by the runner: no published commit.
                self.store.upsert_mr(task.task_id, path, repo.id, iid, url, "")
        return skipped

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
            with self.store.transaction():
                row = self.store.add_review(task.task_id, x.id, x.author, x.created, asked)
                if row:
                    self.d.taken(task, x.id)
            if row:
                self.events.emit("review_requested", task_id=task.task_id, review=row.id, author=x.author)
                self.d.retry_pending()
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
            holder = self.d.workspace.taken()
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
        skipped = self._people_mrs(task, issue, self._ticket_comments(task)) if task.people_work else []
        open_mrs = []
        for m in self.store.task_mrs(task.task_id):
            st = self.host.mr_status(m.project_id, m.iid)
            if st.state == "opened":
                open_mrs.append((m, st))
        if not open_mrs:
            where = (f"no open merge request of the {self.host.label} {self.host.scope_kind} {self.host.scope} is "
                     "linked on the ticket" if task.people_work else "this ticket has no open merge request")
            self._refuse(row, task, "nothing", f"{where}, so there is nothing to review."
                         + "".join(f"\n- Not reviewed: {x['url']}: {x['why']}." for x in skipped), outcome="nothing")
            return
        group = {r.relative_path: r for r in self.d.workspace.group()}
        try:
            self.d.workspace.task_notes(task)  # the task's own, for the reviewer to judge by the rules the work had
        except ProjectNotesError as e:
            self._refuse(row, task, "project_notes", f"the project's notes for the agent cannot be given to the "
                                                     f"reviewer ({e}); no review was run.", outcome="unverified")
            return
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
            self.d.workspace.ensure_repo(repo, path)
            if self.git.current_branch(path) != branch and not self.git.is_clean(path):
                dirty.append(m.repo)
                continue
            target = st.target or repo.default_branch or "main"
            if task.people_work:
                # The head the host has for the MR, from the target repository: also a fork's, which origin has no
                # branch of. Nothing else stands in for it.
                self.git.fetch_ref(path, self.host.mr_head_ref(m.iid))
                sha, source = st.sha, st.source or f"merge request {m.iid}"
            else:
                self.git.fetch_branch(path, task.branch)
                sha, source = st.sha or self.git.remote_sha(path, task.branch) or "", task.branch
            if not sha or not self.git.has_commit(path, sha):
                self._refuse(row, task, "no_commit", f"the head of {m.url} could not be fetched; no review was run.",
                             outcome="unverified")
                return
            current = self.git.current_branch(path)
            mrs.append({"label": f"M{i}", "repo": m.repo, "project_id": m.project_id, "iid": m.iid, "url": m.url,
                        "sha": sha, "target": target, "source": source,
                        "files": self.git.changed_files(path, f"origin/{target}", sha),
                        "lines": self.git.changed_lines(path, f"origin/{target}", sha),
                        "target_sha": self.git.ref_sha(path, f"origin/{target}"),
                        "prev": current if current != branch else None, "prev_head": self.git.head(path)})
        home = ws
        if (home / ".git").exists() and self.git.current_branch(home) != branch and not self.git.is_clean(home):
            dirty.append(".")
        if dirty:
            self._note_waiting(row, "uncommitted_changes", repos=dirty)
            return
        # The same commits, merged into the same branches, against the same ticket: the earlier verdict stands.
        context = self._context(task, issue)
        mr_comments = self._mr_comments(mrs)
        said = _said(mr_comments)
        reviewed = ({(m["repo"], m["sha"], m["target"], m["target_sha"]) for m in mrs}, context, said)
        for earlier in self.store.reviews("done", task_id=task.task_id):
            then = json.loads(earlier.snapshot)
            # Findings of fit alone stand only while they can still go back: asked again for the same commits, the
            # review is run again and judges them by where the fix rounds are now.
            fit_only = earlier.outcome == "changes" and all(
                _fit_only(m) for m in json.loads(earlier.result or "{}").get("mrs", []) if m["kind"] == "changes")
            if earlier.outcome in FINAL and not fit_only and then.get("rules", 1) == RULES and (
                    {(m["repo"], m["sha"], m["target"], m.get("target_sha")) for m in then.get("mrs", [])},
                    then.get("ticket", {}).get("context"), then.get("mr_comments")) == reviewed:
                self._refuse(row, task, "already_reviewed", f"these commits were already reviewed (see the review "
                                                            f"{earlier.id} above); nothing changed since, so it "
                                                            "stands.", outcome="refused")
                return
        comments = self._ticket_comments(task)
        files = self._files(task, comments)
        text = self._compose(row, task, issue, comments, mrs, mr_comments)
        if files:
            texts = [("the description", None, issue.description),
                     *((f"the comment by {x.author} of {_when(x.created)}", str(x.id), x.text) for x in comments)]
            text, files = materials.with_section(text, files, self.d._files_folder(task), texts)
        authors = [row.author, *issue.writers, *(x.author for x in comments), *(n.author for _, n in mr_comments),
                   *(m.attachment.author for m in files)]
        clearance = self.d.gate.clear(task, "review_request", text, authors=authors, resume_state=task.state,
                                      ref=row.id, files=materials.screen_files(files))
        if clearance is None:
            return  # held for a security reviewer, refused, or the screen is down: nothing switched
        snapshot = {"branch": branch, "text": text, "authors": authors, "skipped": skipped,
                    "home_prev": self.git.current_branch(home) if (home / ".git").exists() else None,
                    "ticket": {"context": context, "requirements": self._requirements(task, issue)}, "mrs": mrs,
                    "mr_comments": said, "rules": RULES}
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
        # The review's branch of the home starts from main: main gets the task's notes, not the newest.
        try:
            notes = self.d.workspace.task_notes(task)
        except ProjectNotesError as e:  # kept when the review was prepared, then removed by hand
            self._settled(row, None, f"the project's notes for the agent cannot be given to the reviewer ({e})")
            return
        self.d.workspace.prepare_home(self.d.workspace.group(), notes)
        for m in snap["mrs"]:
            self.git.start_branch(ws / m["repo"], branch, m["sha"])
        extra = self._extra_dirs(row)
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("".join(f"{ws / m['repo']}\n" for m in snap["mrs"]))
        # Read, build and test there, never edit: no writable-directories.txt beside the list.
        (extra.parent / "writable-directories.txt").unlink(missing_ok=True)
        self._verdict_file(row).parent.mkdir(parents=True, exist_ok=True)
        # The files the brief names, fetched again (or kept from the fetch above): screened with it once more.
        files = self._files(task, self._ticket_comments(task)) if materials.SECTION_HEAD in snap["text"] else []
        files = materials.given_in(snap["text"], files, self.d._files_folder(task))
        clearance = self.d.gate.clear(task, "review_request", snap["text"], authors=snap["authors"],
                                      resume_state=task.state, ref=row.id, files=materials.screen_files(files))
        if clearance is None:
            return  # the screen is down right now: the switched workspace stays held, tried again next tick
        self.d.publish_files(task, files)
        reply = self.d.engine.dispatch(task_id=row.id, message_id=f"review-{row.id}", author=row.author,
                                       source=self.tracker.engine_source, branch=branch, base=HOME_BRANCH,
                                       repo=Path(ws), text=snap["text"], clearance=clearance, extra_dirs=extra,
                                       report_review=self._report_review(row))
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
        if self._report_disputed(show) and self._return_report(row, show):
            return
        self._settled(row, show)

    @staticmethod
    def _report_disputed(show: dict) -> bool:
        """The run ended because Codex did not accept the reviewer's closing report at its round limit. A review
        commits nothing, so the engine's report review is the only gate that can end it this way."""
        receipt = show.get("receipt") or {}
        return (show.get("state") == "failed" and show.get("state_reason") == "needs-user"
                and receipt.get("result") == "succeeded_research"
                and bool(_REPORT_FAIL.search(receipt.get("review_why") or "")))

    def _return_report(self, row: ReviewRequest, show: dict) -> bool:
        """Codex's objections back to the reviewer for one more attempt, at most REPORT_REPAIRS times. True while
        that is under way (sent, or waiting for the screen or the engine); False when the review is to be settled.

        The attempt is counted before it is sent and marked sent after: a runner that restarts in between sends the
        same message again (the engine takes a message id once), never a second attempt."""
        snap = json.loads(row.snapshot)
        n = int(snap.get("report_repairs", 0))
        if snap.get("report_repair_sent", True):
            if n >= REPORT_REPAIRS or self._overdue(row):
                return False
            n += 1
            snap = {**snap, "report_repairs": n, "report_repair_sent": False}
            row = self.store.update_review(row.id, snapshot=json.dumps(snap))
        elif self._overdue(row):
            return False
        why = ((show.get("receipt") or {}).get("review_why") or "").strip()[:MAX_OBJECTIONS]
        text = REPORT_RETURNED + why
        task = self.store.task(row.task_id)
        # Codex's words quote the repository and the reviewer's logs: screened like any text from outside.
        clearance = self.d.gate.clear(task, "review_request", text, authors=[], resume_state=task.state,
                                      ref=row.id)
        if clearance is None:
            return True  # held for a security reviewer or the screen is down: tried again next tick, until overdue
        reply = self.d.engine.send(task_id=row.id, message_id=f"report-{row.id}-{n}", source="other",
                                   author=self.cfg.tracker.bot_user, repo=Path(self.cfg.paths.workspace), text=text,
                                   clearance=clearance, extra_dirs=self._extra_dirs(row))
        if reply.rc == 6:
            return True   # busy: the same message again next tick
        if not reply.accepted:
            return False
        self.store.update_review(row.id, snapshot=json.dumps({**snap, "report_repair_sent": True}))
        self.events.emit("review_report_returned", task_id=row.task_id, review=row.id, attempt=n)
        return True

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
        restore_workspace(
            self.git, self.cfg.paths.workspace, snap["branch"], snap["mrs"], snap.get("home_prev"),
            stash_message=f"arceus: what the review {row.id} left",
            on_stashed=lambda repo, stash: self.events.emit("review_leftovers_stashed", task_id=row.task_id,
                                                            review=row.id, repo=repo, stash=stash),
            on_committed=lambda repo, sha: self.events.emit("review_committed", task_id=row.task_id, review=row.id,
                                                            repo=repo, sha=sha))
        row = self.store.update_review(row.id, state="publishing")
        self.events.emit("review_workspace_restored", task_id=row.task_id, review=row.id)
        self._publish(row)

    # -- the result ---------------------------------------------------------------------------------

    def _publish(self, row: ReviewRequest) -> None:
        task = self.store.task(row.task_id)
        result = json.loads(row.result) if row.result else None
        if result is None or result.get("rules") != RULES:
            # Decided by an older runner (a publication cut off by the upgrade): decided again by today's rules before
            # anything is approved or posted.
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
        if task.people_work or self.store.signal(_fix_signal(row)) is not None:
            return  # people wrote the code: no agent to send it to
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
        sent = self._sent_fits(self.store.task(row.task_id), json.loads(row.snapshot))
        parts = []
        for mr in result["mrs"]:
            if mr["kind"] != "changes":
                continue
            v = mr["verdict"] or {}
            items = [self._finding(f) for f in v.get("findings", []) if f.get("severity") == "blocker"]
            for what in ("build", "tests"):
                if v.get(what) == "failed":
                    items.append(f"The {what} failed when the reviewer ran them.")
            items += [f"To fit the project: {self._finding(f)} Remove it or bring it in line with what it names; add "
                      "nothing. If a person on the ticket or the merge request decided it this way, keep it and say "
                      "so in your result." for f in v.get("findings", [])
                      if f.get("severity") == "fit" and _fit_key(mr, f) not in sent]
            items += [f"Note: {r}." for r in mr["reasons"]]
            if v.get("verdict") == "unsure" and v.get("unsure_because"):
                items.append(f"Left open for people, not for you to decide in code: {v['unsure_because']} Keep the "
                             "change to what the ticket asks and say in your result what stays open.")
            parts.append(f"{mr['repo']} ({mr['url']}), reviewed at `{mr['sha'][:12]}`:\n"
                         + "\n".join(f"  - {i}" for i in items or ["see the review note on the MR"]))
        return (f"The code review {row.id} found these; the whole review is the note on the MR.\n"
                + "\n".join(parts) + "\n  Fix each one with new commits, changing only what it names: add nothing "
                "the ticket does not ask for. If one is wrong, change nothing for it and say why in your result. The "
                "runner reviews your new commits again.")

    def _fix_round(self, row: ReviewRequest, task: Task) -> int | None:
        """Which fix round this review's findings went to the author as, or None if they did not."""
        ids = [s.id for s in self.store.signals_of_kind(task.task_id, FIX_KIND)]
        return ids.index(_fix_signal(row)) + 1 if _fix_signal(row) in ids else None

    def _sent_back_words(self, row: ReviewRequest, task: Task) -> str:
        if task.people_work:
            return (f"People wrote this code, so nothing goes to an agent: its author makes these changes and writes "
                    f"`{self.rc.command}` on the ticket for another review.")
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
        if not self.rc.fix_rounds or task.state != "code_review" or task.people_work:
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
        changed = self._context_problem(task, snap) if ticket else None
        fatal = verdict.get("fatal") or []
        settled = datetime.fromisoformat(verdict.get("settled_at") or self.d.now().isoformat())
        can_wait = self.d.now() - settled < self.rc.timeout
        mrs = []
        for m in snap["mrs"]:
            st = self.host.mr_status(m["project_id"], m["iid"])
            v = found.get(m["label"])
            if v is not None and "ui" not in v:
                # Read before the review had to say whether it looked at what people see: not a pass.
                v = {**v, "ui": "not_run", "ui_checked": "", "doubts": [*v.get("doubts", []), NO_UI]}
            risky = self._always_attention(m["files"])
            reasons: list[str] = []
            checks = self.rc.checks(m["repo"])
            # What the reviewer could not run here, CI ran on the reviewed commit: those jobs count instead.
            in_ci: dict[str, list[str]] = {}
            if v is not None and st.state == "opened":
                for what, patterns in (("build", checks.ci_build_jobs), ("tests", checks.ci_test_jobs)):
                    if v[what] != "not_run" or not patterns:
                        continue
                    jobs = self._ci_jobs(st, m, patterns)
                    if jobs is None and can_wait:
                        return None  # those jobs are still running on the reviewed commit: decided on a later tick
                    if jobs:
                        in_ci[what] = jobs
            unseen = UNSEEN + (f" ({v['ui_checked']})" if v["ui_checked"] else "") \
                if v is not None and v["ui"] == "not_run" and not v.get("doubts") else ""
            if v is not None:
                # A finding of fit goes to the author only when the runner finds what it stands on: a line the change
                # touches, and the rule, the existing code or the entry of `scope` it names. Otherwise it is the
                # reviewer's idea, and a suggestion.
                v = {**v, "findings": [f if f["severity"] != "fit" or self._fit_holds(task, m, v, f)
                                       else {**f, "severity": "suggestion"} for f in v["findings"]]}
            fits = [(m, f) for f in v["findings"] if f["severity"] == "fit"] if v is not None else []
            build = "passed" if "build" in in_ci else v and v["build"]
            tests = "passed" if "tests" in in_ci else v and v["tests"]
            # What the repository's rules do not ask for stops nothing when it was not run; seen failing, it still does.
            not_needed = [w for w, need, got in (("build", checks.build, build), ("tests", checks.tests, tests))
                          if need == "none" and got == "not_run"]
            build_ok = build == "passed" or "build" in not_needed
            tests_ok = tests in ("passed", "none", "failed_before") or "tests" in not_needed
            if st.state != "opened":
                kind = "closed"
            elif v is None:
                kind = "unverified"
                reasons.append("the reviewer left no verdict for it")
            elif fatal:
                # A run that never finished as asked (a closing report Codex still disputes, a stop to ask) backs
                # none of its verdict: no approval, and none of its blockers go to the author either.
                kind = "unverified"
                reasons += [*fatal, *v.get("doubts", [])]
            elif build == "failed" or tests == "failed" or any(f["severity"] == "blocker" for f in v["findings"]):
                # Only what the agent can act on goes back to it: a failure seen, or a blocker that names what it
                # breaks and how ordinary use reaches it.
                kind = "changes"
                reasons += v.get("doubts", [])
            elif v["verdict"] in ("unsure", "changes") or v["requirements_met"] is not True or not build_ok \
                    or not tests_ok or any(f["severity"] == "unsupported" for f in v["findings"]):
                # "Unsure" with nothing that must change is a question for people (an open product decision, a fact
                # outside the repository): the agent cannot answer it, so it is never sent back as a fix. So is a
                # call for changes that names no broken requirement or no ordinary way to reach it: a person
                # decides whether it is needed, rather than the agent building what nobody asked for.
                kind = "unverified"
                unbacked = v["verdict"] == "changes" or any(f["severity"] == "unsupported" for f in v["findings"]) \
                    or (v["requirements_met"] is False and v["verdict"] != "unsure")
                if v["verdict"] == "unsure":
                    reasons.append(v["unsure_because"] or "the reviewer could not tell whether it is right, and did "
                                                           "not say why")
                if unbacked:
                    reasons.append(UNBACKED)
                if v["requirements_met"] is None:
                    reasons.append("the reviewer did not say whether it meets the ticket")
                elif v["requirements_met"] is False and v["verdict"] == "unsure":
                    reasons.append("until that is answered, the reviewer cannot say it meets the ticket")
                if not build_ok:
                    reasons.append(self._not_run("build", checks.ci_build_jobs))
                if not tests_ok:
                    reasons.append(self._not_run("tests", checks.ci_test_jobs))
                if unseen:
                    reasons.append(unseen)
                if fits and not unbacked and build_ok and tests_ok and not v.get("doubts") \
                        and v["requirements_met"] is not None and self._fit_goes_back(task, snap, fits):
                    # The open question stays with people and nothing is approved, but what does not fit the
                    # project is not theirs to decide: it goes to the author now, the question marked as theirs.
                    kind = "changes"
                    reasons = [r for r in reasons if r != v["unsure_because"]]
            elif v.get("doubts"):
                # A verdict the runner could not read whole is not a pass.
                kind = "unverified"
                reasons += v.get("doubts", [])
            elif fits and self._fit_goes_back(task, snap, fits):
                # Nothing is broken, but the change does not fit the project (a rule it breaks, a job the code
                # already does another way, code that does nothing, work nobody asked for): the author brings it in
                # line in a round of its own. Once the rounds are used up it no longer holds up the merge. What
                # people see still waits for someone to look at it.
                kind = "changes"
                if unseen:
                    reasons.append(unseen)
            elif unseen:
                # Tests and a reading of the code do not show a column squeezed off the screen or a label a screen
                # reader no longer matches: a change people see is approved only once someone saw it.
                kind = "unverified"
                reasons.append(unseen)
            else:
                kind = "ready"
            if changed and kind != "closed":
                # Whatever it found answers what people asked before: no approval on it, no fixes passed on that the
                # newer word may rule out; a person asks for the review again.
                kind = "unverified"
                reasons.append(changed)
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
                "stale": bool(changed) and kind != "closed",
                "risky": risky, "ci": ci, "verdict": v, "in_ci": in_ci, "not_needed": not_needed})
        return {"off": not self.enabled, "rules": RULES, "mrs": mrs, "summary": verdict.get("summary") or "",
                "problems": verdict.get("problems") or [], "stale": changed or ""}

    def _fit_goes_back(self, task: Task, snap: dict | None = None, fits: list[dict] | None = None) -> bool:
        """Whether findings of fit alone send the change back: only to an agent waiting in Code Review, while the
        ticket has fix rounds left, and once per set of commits: an author that kept them (a person decided so, or
        they were wrong) is not asked again for the same code. Otherwise they are notes, and the approval goes by
        its usual checks."""
        if task.people_work or not self.rc.fix_rounds or task.state != "code_review" \
                or len(self.store.signals_of_kind(task.task_id, FIX_KIND)) >= self.rc.fix_rounds:
            return False
        if snap is None:
            return True
        if self._ticket_problem(task, snap):
            return False
        sent = self._sent_fits(task, snap)
        return any(_fit_key(mr, f) not in sent for mr, f in fits or [])

    def _fit_holds(self, task: Task, mr: dict, v: dict, f: dict) -> bool:
        """Whether a finding of fit stands on what it names, checked in the code: the place in the change, then by its
        basis the rule quoted from the project's notes or from a file of the target branch (not the change's own),
        the line of existing code, or the entry of `scope` that nobody asked for."""
        if not _in_change(f, mr):
            return False
        path = self.cfg.paths.workspace / mr["repo"]
        target = mr.get("target_sha") or f"origin/{mr['target']}"
        if f["basis"] == "rule":
            # The whole quote, in the project's notes or in a file of rules of the target branch (not the change's).
            quote = _quoted(f["evidence"])
            if not quote:
                return False
            try:
                texts = [self.d.workspace.task_notes(task)]
            except OSError:
                texts = []
            named = [x for x in re.findall(r"[\w./-]+\.md\b", f["evidence"])]
            for name in dict.fromkeys([*self.git.rule_files(path, target), *named]):
                texts.append("\n".join(self.git.text_lines(path, target, name) or []))
            return any(quote in _plain(t) for t in texts)
        if f["basis"] == "existing":
            # A line that exists, in a text file, holding a name the evidence gives for the code doing that job.
            place = _PLACE.search(f["evidence"])
            if not place:
                return False
            where, line = place.group(0).rsplit(":", 1)
            names = {w for w in re.findall(r"[A-Za-z_][\w]{2,}", f["evidence"].replace(place.group(0), " "))}
            for ref in (mr["sha"], target):
                lines = self.git.text_lines(path, ref, where)
                if lines and int(line) <= len(lines):
                    near = " ".join(lines[max(int(line) - 4, 0):int(line) + 3])
                    if any(re.search(rf"\b{re.escape(n)}\b", near) for n in names):
                        return True
            return False
        if f["basis"] == "unasked":
            # The entry of `scope` it is about says nobody asked for it, and no other entry says somebody did.
            marks = [t.lower() for t in re.findall(r"`([^`]+)`", f["evidence"])] or \
                [f["file"].rsplit("/", 1)[-1].lower()]
            about = [x for x in v.get("scope", []) if any(t in x["what"].lower() for t in marks)]
            return bool(about) and all(x["asked_by"].lower().startswith("nobody") for x in about)
        return True

    def _sent_fits(self, task: Task, snap: dict) -> set:
        """The findings of fit earlier reviews of the same code, read against the same ticket and comments, sent to
        the author."""
        sent = set()
        for r in self.store.reviews("done", task_id=task.task_id):
            if self.store.signal(_fix_signal(r)) is None or _judged(json.loads(r.snapshot or "{}")) != _judged(snap):
                continue
            for mr in json.loads(r.result or "{}").get("mrs", []):
                if mr["kind"] == "changes":
                    sent |= {_fit_key(mr, f) for f in (mr.get("verdict") or {}).get("findings", [])
                             if f.get("severity") == "fit"}
        return sent

    def _ci_jobs(self, st: MRStatus, m: dict, patterns: list[str]) -> list[str] | None:
        """The jobs of the reviewed commit's CI run that match `patterns`, when there is one and all of them
        succeeded; [] when they do not count (no run for that commit, no match, one failed or was skipped); None
        while one of them is still running."""
        if st.ci_id is None or st.ci_sha != m["sha"]:
            return []
        jobs = [j for j in self.host.ci_jobs(m["project_id"], st.ci_id)
                if any(fnmatch.fnmatchcase(j.name, p) for p in patterns)]
        if any(j.status in ("created", "waiting_for_resource", "preparing", "pending", "running", "scheduled")
               for j in jobs):
            return None
        return [j.name for j in jobs] if jobs and all(j.status == "success" for j in jobs) else []

    @staticmethod
    def _checks_hint(checks: RepoChecks) -> str:
        """What this repository's rules (review_agent.repos) mean for the reviewer, or "" when they are the default."""
        said = []
        none = [w for w, need in (("no build", checks.build), ("none of its own tests", checks.tests))
                if need == "none"]
        if none:
            said.append(f"For this repository the runner needs {' and '.join(none)}: what of them you do not run is "
                        "\"not_run\" and stops nothing, what you run and see fail still does.")
        jobs = [f"{what} {', '.join(f'`{p}`' for p in patterns)}" for what, patterns in
                (("the build", checks.ci_build_jobs), ("the tests", checks.ci_test_jobs)) if patterns]
        if jobs:
            said.append("For what you could not run, the runner counts these jobs of the reviewed commit's CI run "
                        "instead: " + "; ".join(jobs) + ".")
        return " ".join(said) + " Still report only what you saw yourself." if said else ""

    @staticmethod
    def _not_run(what: str, patterns: list[str]) -> str:
        said = f"the {what} {'was' if what == 'build' else 'were'} not run"
        if not patterns:
            return said
        return f"{said}, and no CI job of the reviewed commit matching {', '.join(f'`{p}`' for p in patterns)} passed"

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

    def approval_outlived(self, task: Task, project_id, iid, st: MRStatus) -> None:
        """The newest finished review approved this MR, and what it approved is no longer what the MR is: new
        commits, another target branch, or a ticket that now asks for something else (its description or files).
        Take the approval back, once, and say so on the MR. GitLab keeps an approval across pushes unless the
        project resets approvals on push, which not every edition offers; the runner's own approval stands for one
        commit, one target and one version of the ticket."""
        if not st.sha or not getattr(self.host, "supports_approval", False):
            return
        done = [r for r in self.store.reviews("done", task_id=task.task_id) if r.result]
        for row in reversed(done):
            mr = next((m for m in json.loads(row.result).get("mrs", [])
                       if str(m["project_id"]) == str(project_id) and str(m["iid"]) == str(iid)), None)
            if mr is None:
                continue
            approval = self._approval(row, task, mr)
            if not (approval and approval["done"]):
                return  # the newest review of this MR gave no approval
            key = f"{self._keys(row, mr)}:outlived"
            act = self.store.action(key)
            if act and act[0] == "done":
                return
            # A revoke begun before a failure or a crash is finished for the reason it began with.
            why = json.loads(act[1])["why"] if act and act[1] else self._outlived_by(row, task, mr, st)
            if not why:
                return
            if not act:
                self.store.action_intent(key, task.task_id, "review-revoke", json.dumps({"why": why}))
            by = approval.get("by") or self.host.reviewer
            # Begun before (act): the approval may be gone already, taken back by that attempt; the note still says so.
            revoked = self.host.revoke(mr["project_id"], mr["iid"]) or (act is not None and by == self.host.reviewer)
            if revoked or by != self.host.reviewer:
                # An approval given by an account the runner no longer approves as (the approver was changed in the
                # config) cannot be taken back by the runner: the note says so and who has to remove it.
                marker = f"<!-- arceus:{row.id}:{mr['label']}:outlived -->"
                signer = {self.host.reviewer, self.cfg.code_host.bot_user}  # the bot posts it when the approver cannot
                head_line = f"It was given for commit `{mr['sha'][:12]}`; {why}."
                text = (f"**Code review: the approval was taken back.** {head_line}" if revoked else
                        f"**Code review: the approval by {by} is out of date.** {head_line} The runner now approves "
                        f"as {self.host.reviewer} and cannot take it back: {by}, remove it on the merge request.")
                if not any(marker in n.body and n.author in signer
                           for n in self.host.notes(mr["project_id"], mr["iid"], mr["url"])):
                    self.host.note_review(mr["project_id"], mr["iid"],
                                          f"{text} Write `{self.rc.command}` on the ticket to review it again.\n\n"
                                          f"{marker}")
                self.events.emit("review_approval_revoked" if revoked else "review_approval_outdated",
                                 task_id=task.task_id, review=row.id, url=mr["url"], reason=why, sha=st.sha, by=by)
            self.store.action_done(key)
            return

    def merged_check(self, task: Task, project_id, iid, st: MRStatus) -> None:
        """A merge request the code review judged is merged: if what was merged is not what the review let through
        (the newest finished review asked for changes, or the merge is at commits the review never saw), say so
        on the ticket and in Slack, once per merge request across failures and restarts. A merge request no review
        judged, or one merged at the commit (or the same source diff) the review found ready, is left alone. What
        the merge did against a target branch that moved on is not compared: the check is of the source diff. While a
        review of the ticket is under way nothing is decided: its own result says it was merged meanwhile."""
        key = f"{task.task_id}:merged-check:{project_id}:{iid}"
        act = self.store.action(key)
        if act and act[0] == "done":
            return
        if act and act[1]:  # begun before a failure or a crash: finished for the reason it began with
            begun = json.loads(act[1])
            row, mr, why = self.store.review(begun["review"]), begun["mr"], begun["why"]
        else:
            if self.store.reviews(*REVIEW_UNDER_WAY, task_id=task.task_id):
                return
            row, mr = self._last_judged(task, project_id, iid)
            why = self._merged_unchecked(row, mr, project_id, iid, st) if row else ""
            if not why:
                self.store.action_intent(key, task.task_id, "merged-check")
                self.store.action_done(key, json.dumps({"why": "", "review": row.id if row else None}))
                return
            self.store.action_intent(key, task.task_id, "merged-check",
                                     json.dumps({"review": row.id, "mr": mr, "why": why, "sha": st.sha}))
        # Only blockers the review stood by: an unconfirmed one (people changed what they asked for while it ran, or
        # its run never finished as asked) found nothing anyone should still act on.
        findings = [f for f in (mr.get("verdict") or {}).get("findings", [])
                    if f.get("severity") == "blocker"] if mr["kind"] == "changes" else []
        lines = [f"**Merged without a passing code review.** {mr['url']} ({mr['repo']}) was merged {why}."]
        if findings:
            lines += ["", "What the review found that blocks the merge:",
                      *(f"- {self._finding(f)}" for f in findings[:10])]
        lines += ["", "Decide whether it needs a fix in a follow-up."]
        self.d.comment_once(task, f"merged-unchecked:{project_id}:{iid}", "\n".join(lines))
        self.events.emit("review_merged_unchecked", task_id=task.task_id, review=row.id if row else None,
                         url=mr["url"], repo=mr["repo"], reason=why, sha=st.sha, reviewed=mr["sha"])
        self.store.action_done(key)

    def watch_merges(self) -> None:
        """The merge requests a review judged, of tickets the runner has let go of (moved out of Code Review, for
        instance back to In Progress for the fixes, or a task that ended): each open one is read on the code
        host's poll until it is merged or closed, so a later merge the review did not let through is still
        reported (merged_check). Tickets in Code Review are watched by their own poll; a task under way is left to
        it as well, since it comes back to Code Review before its MRs can be merged by the runner's loop."""
        for task in self.store.tasks("done", "stopped", "failed"):
            for m in self.store.task_mrs(task.task_id):
                if m.state != "opened" or self._last_judged(task, m.project_id, m.iid)[0] is None:
                    continue
                if (act := self.store.action(f"{task.task_id}:merged-check:{m.project_id}:{m.iid}")) \
                        and act[0] == "done":
                    continue
                try:
                    st = self.host.mr_status(m.project_id, m.iid)
                    if st.state == "merged":
                        self.merged_check(task, m.project_id, m.iid, st)
                    if st.state != "opened" and not self.store.reviews(*REVIEW_UNDER_WAY, task_id=task.task_id):
                        self.store.update_mr(task.task_id, m.repo, state=st.state)
                except ServiceError as e:
                    self.events.emit("review_poll_failed", task_id=task.task_id, reason=str(e))

    def _last_judged(self, task: Task, project_id, iid) -> tuple[ReviewRequest | None, dict | None]:
        """The newest finished review that judged this MR, and its entry for it. A review whose MR was merged or
        closed while it ran judged nothing."""
        for row in reversed([r for r in self.store.reviews("done", task_id=task.task_id) if r.result]):
            mr = next((m for m in json.loads(row.result).get("mrs", [])
                       if str(m["project_id"]) == str(project_id) and str(m["iid"]) == str(iid)), None)
            if mr is not None and mr["kind"] != "closed":
                return row, mr
        return None, None

    def _merged_unchecked(self, row: ReviewRequest, mr: dict, project_id, iid, st: MRStatus) -> str:
        """Why the merge is not what the review let through, in words for the ticket, or "" when it is."""
        then = f"commit `{mr['sha'][:12]}`"
        # Findings of fit alone do not hold up a merge: merged as they stood, the people decided.
        asked = mr["kind"] == "changes" and not _fit_only(mr)
        if not st.sha:  # the host does not say what was merged: only a review that asked for changes is news
            return f"although the code review asked for changes at {then}" if asked else ""
        moved = st.sha != mr["sha"]
        if moved:
            same = getattr(self.host, "same_change", None)
            if same is not None and same(project_id, iid, mr["sha"], st.sha) is True:
                moved = False  # rebased: the source diff the review read is the one that was merged
        if asked:
            if moved:
                return (f"after the code review asked for changes at {then}; the commits pushed since (merged at "
                        f"`{st.sha[:12]}`) were never reviewed")
            return f"although the code review asked for changes at {then}, and the code was not changed after it"
        if moved:
            return (f"at `{st.sha[:12]}`, but the code review read {then}; the changes between them were never "
                    "reviewed")
        return ""

    def _outlived_by(self, row: ReviewRequest, task: Task, mr: dict, st: MRStatus) -> str:
        """Why the approval no longer holds, in words for the MR note, or "" while it does."""
        if st.sha != mr["sha"]:
            return f"the merge request now has new commits (head `{st.sha[:12]}`) that nobody reviewed"
        if st.target and mr.get("target") and st.target != mr["target"]:
            return f"the merge request now merges into {st.target}, not {mr['target']}, which the review checked"
        then = json.loads(row.snapshot or "{}").get("ticket", {}).get("requirements")
        if then and self._requirements(task, self.tracker.issue(task.task_id)) != then:
            return "the ticket's description or attached files changed since the review"
        return ""

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
        if mr.get("stale"):
            return "\n".join([*lines, "Why:", *(f"- {r}" for r in mr["reasons"]), "",
                               "What the review found answers what people asked before that, so none of it is shown "
                               f"here. Not approved. Write `{self.rc.command}` on the ticket to review it again."])
        if v:
            met = {True: "met", False: "not met", None: "not judged"}[v["requirements_met"]]
            in_ci = mr.get("in_ci") or {}
            ran = {w: f"passed in CI ({', '.join(in_ci[w])})" for w in in_ci}
            ran |= {w: "not run, not needed for this repository" for w in mr.get("not_needed") or []}
            lines += [f"- Requirements of the ticket: {met}",
                      f"- Build: {ran.get('build') or v['build'].replace('_', ' ')}",
                      f"- Tests: {ran.get('tests') or TEST_WORDS.get(v['tests'], v['tests'].replace('_', ' '))}"]
            if v.get("ui"):
                lines.append(f"- What people see: {UI_WORDS[v['ui']]}" + (f" ({v['ui_checked']})" if v["ui_checked"]
                                                                           and v["ui"] != "none" else ""))
            if v["checked"]:
                lines.append(f"- What was run: {v['checked']}")
        lines += [f"- CI: {self._ci_words(mr['ci'])}", ""]
        blockers = [f for f in v.get("findings", []) if f["severity"] == "blocker"]
        suggestions = [f for f in v.get("findings", []) if f["severity"] == "suggestion"]
        fits = [f for f in v.get("findings", []) if f["severity"] == "fit"]
        attention = self._attention(mr)
        if outcome == "approved" and fits:
            lines.append("Approved: nothing has to change for it to work. What does not fit the project is below.")
        elif outcome == "approved":
            lines.append("Approved: the review found nothing that has to change before merge.")
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
            failed = v and "failed" in (v["build"], v["tests"])
            if blockers or failed:
                lines += ["What has to change:", *(f"- {self._finding(f)}" for f in blockers)]
            if failed:
                lines.append(f"- The {'build' if v['build'] == 'failed' else 'tests'} failed.")
            if fits and (blockers or failed):
                lines += ["", "Also to bring in line with the project:", *(f"- {self._finding(f)}" for f in fits)]
            elif fits:
                lines += ["To bring in line with the project (nothing is broken; once the fix rounds are used up "
                          "this no longer holds up the merge):", *(f"- {self._finding(f)}" for f in fits)]
            if v and v["verdict"] == "unsure" and v["unsure_because"]:
                lines += ["", f"Left open for a person to decide: {v['unsure_because']}"]
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
        if fits and outcome != "changes":
            lines += ["", "Does not fit the project, not sent to the agent (" + self._fit_kept(task, outcome) + "):",
                      *(f"- {self._finding(f)}" for f in fits)]
        unsupported = [f for f in v.get("findings", []) if f["severity"] == "unsupported"]
        if unsupported:
            lines += ["", "Asked for without the requirement it breaks or an ordinary way to reach it, so not sent to "
                      "the agent (a person decides):", *(f"- {self._finding(f)}" for f in unsupported)]
        if suggestions:
            lines += ["", "Suggestions (not blocking):", *(f"- {self._finding(f)}" for f in suggestions)]
        return "\n".join(lines)[:60000]

    def _fit_kept(self, task: Task, outcome: str) -> str:
        """Why findings of fit stayed on the note instead of going to the agent."""
        if task.people_work:
            return "people wrote this code: its author decides"
        if not self.rc.fix_rounds:
            return "findings go back only when a person passes them on"
        if len(self.store.signals_of_kind(task.task_id, FIX_KIND)) >= self.rc.fix_rounds:
            return f"the {self.rc.fix_rounds} automatic fix rounds of this ticket are used up"
        if outcome == "unverified":
            return "the review did not confirm the merge request, so a person decides first"
        if task.state != "code_review":
            return "the task is not waiting in Code Review"
        return "they already went to the agent once for these commits, and it kept them"

    @staticmethod
    def _finding(f: dict) -> str:
        where = f["file"] + (f":{f['line']}" if f["file"] and f["line"] else "")
        text = (f"`{where}`: " if where else "") + f["text"].replace("\n", " ")
        if f.get("severity") == "blocker" and f.get("breaks") and f.get("reached_by"):
            text += f" (breaks: {f['breaks']}; reached by: {f['reached_by']})".replace("\n", " ")
        if f.get("severity") == "fit" and f.get("evidence"):
            text += f" ({FIT_WORDS[f['basis']]}: {f['evidence']})".replace("\n", " ")
        return text

    def _ticket_text(self, row: ReviewRequest, task: Task, result: dict, overall: str) -> str:
        who = self.tracker.mention(row.author)
        if result["off"]:
            given = [mr["url"] for mr in result["mrs"] if (self._approval(row, task, mr) or {}).get("done")]
            if given:
                return (f"{who}, the code review was switched off before it finished; nothing more was posted on "
                        "the merge requests. Approved before that: " + ", ".join(given) + ".")
            return (f"{who}, the code review was switched off while it ran; nothing was posted on the merge "
                    "requests and nothing was approved.")
        words = {"approved": "ready to merge, approved; the review found nothing that has to change",
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
        skipped = json.loads(row.snapshot or "{}").get("skipped") or []
        if skipped:
            lines += ["", "Not reviewed:", *(f"- {x['url']}: {x['why']}." for x in skipped)]
        if result.get("stale"):
            lines += ["", f"Not confirmed: {result['stale']}. What the review found answers what people asked before "
                      "that, so it is left out."]
        elif result["summary"]:
            lines += ["", result["summary"]]
        if result["problems"] and overall in ("unverified", "nothing"):
            lines += ["", "What got in the way: " + "; ".join(result["problems"][:5]) + "."]
        if overall == "changes":
            lines += ["", self._sent_back_words(row, task)]
        elif overall == "unverified":
            lines += ["", f"Write `{self.rc.command}` again to repeat the review."]
        return "\n".join(lines)

    def _compose(self, row: ReviewRequest, task: Task, issue: IssueDetails, comments: list[Comment],
                 mrs: list[dict], mr_comments: list[tuple[str, Note]]) -> str:
        """The reviewer's brief: the ticket whole, the MRs and how to answer. The same inputs give the same
        text, so a repeat is the same message for the engine and the same text for the screen."""
        bot = self.cfg.tracker.bot_user
        ws = self.cfg.paths.workspace.resolve()
        lines = [f"Code review of the merge requests of ticket {task.task_id}, asked for by {row.author}"
                 + (f" (again, on the fixes the author pushed for {_auto_of(row)})"
                    if row.comment_id.startswith(AUTO_PREFIX) else "") + ".", "",
                 "You are the reviewer. " + ("This is a review-only run for changes submitted separately" if task.people_work
                                              else "Another agent wrote this code")
                 + "; you check the changes and report what you find. You do "
                 "not fix anything: do not edit files, do not commit, do not push. Do not ask anyone a question: "
                 "nobody answers during this run. When something is unclear, decide what you can, say what you "
                 "assumed, and mark that merge request \"unsure\" instead of stopping.", ""]
        if row.request:
            lines += [f"What {row.author} asked of the review: {row.request}", ""]
        lines += ["## The ticket", "", f"{issue.key}: {issue.summary}", "", issue.description.strip() or "(no description)",
                  ""]
        tags = self.d._tags_section(self.d._ticket_tags(task), reviewer=True)
        if tags:
            lines += [tags, ""]
        lines += ["## Comments on the ticket, oldest first", ""]
        shown = [x for x in comments if x.text.strip()]
        for x in shown:
            who = "Arceus (the runner)" if x.author == bot else x.author
            text = x.text.split(PROGRESS_SEPARATOR, 1)[0] if x.author == bot else x.text
            body = _TRACKER_MARK.sub("", text).strip().replace("\n", "\n  ")
            lines.append(f"- {who}, {_when(x.created)}: {body}")
        if not shown:
            lines.append("(none)")
        lines += ["", "## Comments on the merge requests, oldest first", ""]
        for repo, n in mr_comments:
            where = f" ({n.path}:{n.line})" if n.path else ""
            when = n.created_at[:16].replace("T", " ") + " UTC" if n.created_at else "at an unknown time"
            lines.append(f"- {n.author} on {repo}{where}, {when}: {n.body.strip().replace(chr(10), chr(10) + '  ')}")
        if not mr_comments:
            lines.append("(none)")
        lines += ["", "The requirements are the description as the later comments, on the ticket and on the merge "
                  "requests, changed or narrowed it.", "",
                  "## Merge requests to review", ""]
        for m in mrs:
            lines += [f"### {m['label']} · {m['repo']}", m["url"],
                      f"Branch {m['source']} into {m['target']}, at commit {m['sha']}. In the workspace, "
                      f"`{ws / m['repo']}` is checked out at that commit; the change is "
                      f"`git diff origin/{m['target']}...HEAD` there ({len(m['files'])} files).", ""]
            checks = self._checks_hint(self.rc.checks(m["repo"]))
            if checks:
                lines += [checks, ""]
            always = self._always_attention(m["files"])
            hints = [f for f in attention_matches(m["files"], self.rc.review_hint_paths) if f not in always]
            if always:
                lines += ["Listed for a person by the runner in any case (security, secrets), so do not repeat "
                          "them in `attention` unless you have something to add: "
                          + ", ".join(f"`{f}`" for f in always) + ".", ""]
            if hints:
                lines += ["Read these with extra care (build, CI, dependencies, configuration, migrations); they go to `attention` "
                          "or `deploy` only by the rules of point 3: " + ", ".join(f"`{f}`" for f in hints) + ".", ""]
        if not task.people_work:
            lines += [f"Why the author chose what it did, if it wrote it down: {decisions_file(ws, task.task_id)}.", ""]
        lines += ["## The project's own rules", "",
                  f"Before the code, read the rules the project wrote down: its notes in `{ws / 'AGENTS.md'}` (under "
                  "\"Project notes\"), and in each repository under review its CLAUDE.md and AGENTS.md, at the root "
                  "and in the folders of the changed files, with the files they point to. They say how this project "
                  "wants its code, its tests and its merge requests; you judge by them, not by your own taste. Where "
                  "a merge request edits one of them, judge by the version on its target branch "
                  "(`git show origin/<target>:<path>`), and an edit of a rule nobody asked for is itself a finding "
                  "of fit. Where a rule and the ticket or a person's decision disagree, the ticket and the decision "
                  "win.", "",
                  "## What to check in each merge request", "",
                  "1. Requirements. Does the change do what the ticket asks, all of it? The requirements are the "
                  "ticket's description, acceptance criteria and design notes, as people's comments on the ticket or "
                  "the merge requests later changed or narrowed them. Read each criterion as it is written. A "
                  "decision a person wrote down is a requirement even where you would have chosen otherwise: never "
                  "ask to undo it, and do not raise it again as a suggestion from another angle. Where a criterion reads two ways and the answer changes the code, that is a "
                  "question for people (\"unsure\"), not a blocker; where the change took the wider reading and "
                  "the narrower one would do, it is a finding of fit by point 5. Missing or wrong behaviour is a "
                  "blocker.",
                  "   Do not add requirements. Validation, hardening, retries, fallbacks, ordering or concurrency "
                  "guarantees, logging, tooling and refactors that neither the ticket nor a person asked for are not "
                  "requirements, however sensible: never a blocker. What the change adds that the ticket did not ask "
                  "for is a finding of fit by point 5.",
                  "2. Build and tests. Build the project and run its tests the way the repository does it (README, "
                  "Makefile, verify.sh, CI config). Say what you ran. A repository without tests is \"none\", not "
                  "\"passed\". If something cannot run here (a service, a secret, the network), say so and use "
                  "\"not_run\". When tests fail, run the same ones on the target branch without the change "
                  "(`git stash`, then `git checkout origin/<target>`; back to the reviewed commit after). If exactly "
                  "those fail there too and the change adds no failure of its own, that is \"failed_before\": not a "
                  "blocker, say in `checked` which ones and that they fail on the target branch as well. A test the "
                  "change breaks, or one it was meant to fix and did not, is \"failed\". Never report a result you "
                  "did not see. A build or tests you could not run are \"not_run\" and by themselves no reason for "
                  "\"unsure\": judge the code as you would otherwise, and the runner decides what an unrun build or "
                  "unrun tests mean for the merge.",
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
                  "   `deploy` is for what whoever deploys it should know or do, when the change itself is right "
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
                  f"problem. A preference neither the project's rules nor the code around it share is not a finding. "
                  f"At most {self.rc.max_suggestions} suggestions per merge request.",
                  "5. Fit with the project. Fit with the code around it is not style: it is what the people who keep "
                  "this code send back most. Before any finding of fit, take stock of the change and write it into "
                  "`scope` (see the verdict):",
                  "   - what the change does, group by group: a mechanism or behaviour, a new file, endpoint, "
                  "parameter, setting, dependency, script or build target, a change to shared code or defaults, a "
                  "test or fixture set. For each, `asked_by` is the line of the ticket or of a person's comment that "
                  "asks for it, quoted, or the asked-for change that cannot work without it (\"needed for AC2: ...\"), "
                  "or \"nobody\". A change that settles a question people left open, reads a criterion more widely "
                  "than its words, or guards against what the ticket does not mention is \"nobody\" too: if the "
                  "quote needs \"read as\" or \"assumed\" beside it, it is \"nobody\";",
                  "   - each new definition (function, class, type, enum, constant, validator, error format, "
                  "component, hook, endpoint): `existing` is how the code around it already does that kind of thing, "
                  "found by opening the module's files of that kind (its validators, helpers, constants, errors, "
                  "components): the closest definition doing the same job, or else the established shape for that "
                  "kind (validators that raise the framework's validation error with a code, one error format, one "
                  "way to build a query), as `file:line`, or \"none\".",
                  "   The findings of fit follow from that list, the largest first: a mechanism nobody asked for "
                  "outweighs any comment or constant out of place, so never report the small ones and leave the large "
                  "one out. A finding of fit (`severity` \"fit\") is one of these, and `basis` says which, with "
                  "`evidence` to back it:",
                  "   - \"rule\": it breaks a rule the project wrote down (above); `evidence` quotes the rule and "
                  "names the file it is in;",
                  "   - \"existing\": it solves a job its module already solves another way, or in a shape of its "
                  "own where the module has an established shape for that kind of thing (a validator, helper, type, "
                  "enum or error format of its own beside the existing ones), changes one of two places that do "
                  "the same job (two forms, two endpoints) and leaves the other behind, or changes what is shown or "
                  "returned while the tests' mocks, fixtures or screenshot baselines still hold the old version; "
                  "`evidence` is the existing code to follow, `file:line`. Name the existing code to follow;",
                  "   - \"needless\": code in the change that does nothing (a guard nothing reaches, a check made "
                  "twice, a default that is never used) or takes the long way round where the plain form does the "
                  "same (a generic helper, comprehension or layer for two fixed cases); `evidence` is the plain form;",
                  "   - \"unasked\": a group your `scope` marks \"nobody\" that is more than a line or two (a "
                  "mechanism, tooling, a refactor, a setting, a change to shared code or defaults wider than the "
                  "ticket needs); `evidence` is what it changes beyond the ticket and what the smaller change that "
                  "meets the ticket leaves out. Where the change took the wider of two readings nobody settled, the "
                  "extra is this finding, not a question: the smaller change is the default, and people widen it if "
                  "they want. \"unsure\" is for what even the smaller change leaves open.",
                  "   A finding of fit asks to remove the change or bring it in line with what is there, never to add "
                  "to it. Only what a careful maintainer of this project would send back, one finding per thing. "
                  "What the rules leave open and the code around it does both ways is taste, not a finding. A "
                  "person's decision on the ticket or the merge requests settles a thing: never a finding against "
                  "it. " + ("Findings of fit are notes for the people who wrote the code" if task.people_work else
                            "A reason in the author's decisions file is not a person's decision, but say it in the "
                            "finding. Findings of fit go back to the author before approval")
                  + "; they are not blockers unless they also break something by the rules below.",
                  "6. What people see. When the change alters anything a person sees or operates (a page, a "
                  "component, a dialog, a message, copy, layout, keyboard or screen-reader use, an email), look at it, "
                  "not only at its code. Run the repository's own suites that drive it (end-to-end, visual, "
                  "accessibility) for what changed, and open the changed screens yourself in a browser (the "
                  "repository's Playwright or a headless Chromium in a throwaway container) in the states the ticket "
                  "names: the empty, the error and the filled state, at a phone and a desktop width or at the widths "
                  "the project's rules or test config use. Where the look is not meant to change, compare with the "
                  "target branch. Screenshot baselines that pass only within a tolerance while showing the old text "
                  "are a finding. What is wrong on screen is a blocker when it breaks a requirement or what worked on "
                  "the target branch (a column pushed off the screen, a control a screen reader no longer finds by "
                  "its visible label, an error the form no longer shows). Say in `ui_checked` what you ran and looked "
                  "at, or why you could not. An API, a job or a script that no person operates directly is not this "
                  "point.", "",
                  "A blocker is something that must change before merge, and you can say both of these about it:",
                  "- what it breaks: a requirement by point 1 (quote the ticket or the comment) or behaviour that works "
                  "on the target branch;",
                  "- how ordinary use reaches it: the steps a real user, a producer in these repositories or a "
                  "service the system works with takes in the setup the ticket describes, ending in the wrong result; "
                  "for a test the ticket requires that is missing entirely, where the ticket requires it.",
                  "What only made-up input reaches (a database row nothing writes, a payload no producer sends, a "
                  "value the code's own checks already refuse), or a race that needs several unlikely things to "
                  "happen at once, is a suggestion: say what would make it reachable. Tests that exist and pass but "
                  "could be stronger are a suggestion. Everything else is a suggestion and does not stop the merge. "
                  "A blocker without both is not counted as one: the runner leaves that merge request to a person "
                  "instead of sending it back to the author.", "",
                  "## Your verdict", "",
                  f"Write it to {self._verdict_file(row)} as JSON, one entry per merge request:", "", "```json",
                  SCHEMA_EXAMPLE, "```", "",
                  "- `verdict`: \"ready\" (meets the ticket, no blocker, and the build and tests you could run "
                  "passed or fail only as they did before; findings of fit and suggestions leave it ready), \"changes\" "
                  "(at least one blocker), or \"unsure\" (reading the code, you could not tell whether it is right). A question "
                  "only people can answer, such as a product decision the ticket leaves open, makes it \"unsure\", "
                  "not a blocker: the runner then asks people instead of sending it to the agent.",
                  "- `unsure_because`: with \"unsure\", what you could not tell and why, in a sentence a person can "
                  "check (\"whether the retry is safe: it depends on the payment provider's idempotency, which is "
                  "not in the repository\"); it is the reason the note gives.",
                  "- `requirements_met`: true or false. `build` (passed, failed, not_run) and `tests` (passed, failed, "
                  "failed_before, not_run, none): what you saw yourself.",
                  "- `checked`: the commands you ran and what they showed, in a line or two.",
                  "- `ui`: by point 6, \"checked\" (the change alters what people see and you looked at it), "
                  "\"not_run\" (it does and you could not; the runner then does not approve until a person looks, "
                  "so judge the rest as usual) or \"none\" (nothing a person sees changed). `ui_checked`: what you "
                  "ran and looked at, or why you could not.",
                  "- `scope`: by point 5, one entry per group of the change (`what`, `asked_by`) and per new "
                  "definition (`what`, `existing`).",
                  "- `findings`: `severity` is \"blocker\", \"fit\" or \"suggestion\"; `file` and `line` when it is "
                  "about a place in the code. A blocker also has `breaks` (what it breaks) and `reached_by` (how "
                  "ordinary use reaches it); a finding of fit has `basis` (rule, existing, needless or unasked) and "
                  "`evidence` by point 5, and without them counts as a suggestion.",
                  "- `attention`: what a person has to decide before merge, by point 3, each `why` the question they "
                  "must answer; an empty list when there is nothing.",
                  "- `deploy`: what whoever deploys it should know or do, by point 3; an empty list when there is "
                  "nothing.",
                  "- `summary`: a few sentences for the ticket.",
                  "",
                  "Your closing report (the outcome's summary) is checked by Codex against the code and your logs, "
                  "and a report it does not accept is never approved. Say only what you verified, each claim with "
                  "its evidence: the command and what it showed, or the file and line. Leave out what does not bear "
                  "on the verdict. Anything you ran that changed data or state (a database you seeded, a container "
                  "you started, a file you wrote) runs somewhere throwaway; say where. Codex judges the report by "
                  "this:",
                  "", REPORT_CRITERIA, "",
                  f"Write in English. The runner reads the file and posts the result; you cannot post on "
                  f"{self.tracker.label} or {self.host.label} yourself. Then finish with the outcome "
                  "succeeded_research and the same summary. Never end with needs_input or blocked."]
        return "\n".join(lines)
