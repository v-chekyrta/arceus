"""The two outside systems Arceus works with, as contracts: an issue tracker and a code host.

The runner's flow (loop, delivery, review, screening, cleanup) talks only to these two contracts.
YouTrack (youtrack.py) and GitLab (gitlab.py) are the implementations that ship with Arceus; another
tracker or host is one more module with the same shape, named in config.yaml:

    tracker:   { type: youtrack, ... }   # or a module of your own: { type: acme_arceus.jira, ... }
    code_host: { type: gitlab, ... }

A provider module defines three things:

- its config section: a subclass of config.IssueTrackerConfig or config.CodeHostConfig with the
  provider's own keys next to the shared ones (strict: an unknown key is an error at start);
- the adapter: a class with the methods of IssueTracker or CodeHost below;
- `TRACKER = TrackerType(config=..., build=...)` or `CODE_HOST = CodeHostType(...)`, which this
  module looks up by `type:`. A built-in name is one line in BUILTIN_TRACKERS / BUILTIN_CODE_HOSTS;
  any other `type:` is imported as a Python module path.

What every adapter has to keep, because the flow relies on it:

- Requests go through http.ServiceClient (`client`): it gives the service its own pause and backoff,
  and raises the errors the flow handles (AuthError, NotFound, BadRequest, TransientError). An
  adapter that raises anything else stops the tick for that service.
- Writes are safe to repeat. The flow journals each one and repeats it after a crash; comments
  carry a marker the flow looks for before posting again.
- Ids are opaque to the flow: it stores them as given (an integer as an integer, a string as a
  string), puts them into its own keys through key_part(), and hands them back.

Git itself is not abstracted: the workspace is git repositories, cloned over HTTPS with the
credentials CodeHost.git_auth() gives.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import quote

if TYPE_CHECKING:
    from arceus_runner.checks import Check
    from arceus_runner.config import CodeHostConfig, Config, HttpConfig, IssueTrackerConfig, Secrets
    from arceus_runner.gitops import GitAuth
    from arceus_runner.http import ServiceClient

Id = int | str  # an id as the provider gives it


def key_part(value: Id, keep_dash: bool = False) -> str:
    """An id as one part of the runner's own keys (`note-<repo>-<note>`, `<task>:close-mr:<repo>:<mr>`).

    A number is written as it is, so the keys a GitLab install already stored stay the same. In a string
    everything but letters, digits and `_.~` is percent-encoded, `-` and `:` included: the parts never
    contain a separator, and ("a-b", "c") and ("a", "b-c") cannot make one key. `keep_dash` is for keys
    separated by `:` only, where a tracker's ids (YouTrack's `4-123`) then stay as they were stored.
    """
    if isinstance(value, int):
        return str(value)
    part = quote(str(value), safe="")
    return part if keep_dash else part.replace("-", "%2D")

# -- issue tracker ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    db_id: str     # the tracker's internal id, used in its API
    key: str       # what people call it: PRJ-123. Arceus names the task, its branch and its MRs after it
    summary: str
    created: int   # ms since epoch
    updated: int


@dataclass(frozen=True)
class Trigger:
    """Who put an issue into the queue, as the tracker's own history records it."""
    id: str          # stable id of that event: a later re-queue is a new decision, the same one never is
    author: str      # the tracker login of the person who did it
    timestamp: int   # ms since epoch


@dataclass(frozen=True)
class IssueDetails:
    db_id: str
    key: str
    summary: str
    description: str
    state: str                      # the tracker's name of the issue's state now ("" when it has none)
    writers: tuple[str, ...] = ()   # who reported it and who changed it last: the text may be theirs


@dataclass(frozen=True)
class Comment:
    id: str
    text: str
    author: str
    created: int  # ms since epoch


class IssueTracker(Protocol):
    """Where tasks come from and where people talk to the agent about them.

    Required: everything below. A tracker that cannot say who queued an issue (trigger) cannot be
    used: that answer is the authorization of the whole run.
    """

    name: str            # the service in health checks, metrics and events: "youtrack"
    label: str           # how texts for people name it: "YouTrack"
    engine_source: str   # the engine's --source for text from the ticket: "tracker" (or "youtrack")
    client: ServiceClient
    supports_attachments: bool  # False: screenshots go to the merge requests only

    def identity(self) -> str:
        """Which tracker and project this is (type, address, project). runner.db is bound to it."""

    def describe(self) -> str:
        """One line for the security screen: what the client's own tracker is, by address."""

    def queued(self) -> list[Issue]:
        """Issues waiting for the agent: in the `queued` state with the `queue` tag, oldest first."""

    def trigger(self, issue: Issue) -> Trigger | None:
        """Who put the `queue` tag on this issue most recently, read from the tracker's own history.

        Security boundary: Arceus starts work only for an author on `allowed_users`. Return the
        person who actually applied the tag as the tracker recorded it, never the reporter, the
        assignee or the last editor. None when the history does not say: the issue is not taken.
        """

    def issue(self, key: str) -> IssueDetails: ...

    def tags(self, db_id: str) -> set[str]: ...

    def add_tag(self, db_id: str, name: str) -> None:
        """Idempotent: a tag already there is left as it is."""

    def remove_tag(self, db_id: str, name: str) -> None:
        """Idempotent: a tag already gone is not an error."""

    def set_state(self, db_id: str, state: str) -> None:
        """Move the issue to the state with this name (one of the configured `states`)."""

    def assign_to_bot(self, db_id: str) -> None:
        """Make the bot the issue's assignee when the tracker is set up for that; otherwise nothing.
        BadRequest is reported and the work goes on."""

    def comments(self, db_id: str) -> list[Comment]:
        """Every comment, oldest first, deleted ones left out."""

    def add_comment(self, db_id: str, text: str) -> tuple[str, int]:
        """Post a Markdown comment as the bot; returns its (id, created ms)."""

    def attach(self, db_id: str, comment_id: str, name: str, data: bytes, mime: str) -> None:
        """Attach a file to the bot's comment. Only called when supports_attachments."""

    def attachment_markdown(self, name: str, caption: str) -> str:
        """Markdown that shows an attachment of the comment in place."""

    def issue_url(self, key: str) -> str: ...

    def mention(self, user: str) -> str:
        """How a comment addresses a person so the tracker notifies them: "@j.doe"."""

    def user_exists(self, login: str) -> bool:
        """An active user with this login: `arceus-runner check` verifies the allowlists with it."""

    def check(self) -> list[Check]:
        """`arceus-runner check`: the token, the bot's identity, the project, states and tags."""


# -- code host -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Repo:
    id: Id
    path: str            # last path segment
    relative_path: str   # path inside the configured scope, e.g. "sub/repo": the folder in the workspace
    default_branch: str | None
    archived: bool
    http_url: str        # what git clones over HTTPS
    description: str = ""


MRState = Literal["opened", "merged", "closed"]
CIStatus = Literal["success", "failed", "running", "other"]


@dataclass(frozen=True)
class MergeRequest:
    """A merge request (a pull request, on hosts that call it that)."""
    iid: Id              # its number in the repository
    url: str
    state: MRState
    closed_at_ms: int | None = None


@dataclass(frozen=True)
class MRStatus:
    """What the review loop needs to know about one merge request right now."""
    state: MRState
    sha: str                    # head of the source branch as the host sees it
    # True only when the host has confirmed a conflict with the target branch; None while it is still
    # working mergeability out or cannot say. Unknown is never a conflict.
    conflict: bool | None
    ci_id: Id | None = None     # the newest CI run of the merge request, if the host has CI
    ci_status: CIStatus | None = None  # running: not finished yet; other: cancelled, skipped, manual
    ci_sha: str | None = None   # the commit that run is for: a run for an older commit is not a signal
    ci_url: str | None = None
    closed_by: str = ""         # who closed it, when it is closed


@dataclass(frozen=True)
class Note:
    """One comment on a merge request, in its thread."""
    id: Id
    discussion_id: str         # the thread; a host without threads gives every note its own
    author: str
    body: str
    system: bool               # written by the host itself (a push, a label): never a request
    created_at: str            # ISO 8601
    path: str | None           # file and line for a comment on the diff
    line: int | None
    url: str
    commit: str | None = None  # the version of the merge request a diff comment was written on


@dataclass(frozen=True)
class FailedJob:
    id: Id
    name: str
    stage: str
    url: str


class CodeHost(Protocol):
    """Where the repositories live: merge requests, their discussions and CI.

    Required: repos, the merge request calls, notes, reply, git_auth, ping. Optional, with what happens
    without them:
    - CI: mr_status without ci_* fields; failed CI is then never a signal to the agent.
    - Logs of failed jobs: failed_jobs() returning [] (the agent gets the run's link only).
    - Conflict detection: conflict=None; the agent learns of a conflict from people instead.
    - Threads: reply() posting a plain comment on the merge request; it carries the same marker.
    - Uploads: supports_uploads False; screenshots go to the ticket only.
    """

    name: str          # the service in health checks, metrics and events: "gitlab"
    label: str         # how texts for people and the agent name it: "GitLab"
    scope_kind: str    # what holds the project's repositories: "group", "organization"
    scope: str         # its path: "clients/acme"
    client: ServiceClient
    supports_uploads: bool

    def identity(self) -> str:
        """Which host and scope this is (type, address, scope). runner.db is bound to it."""

    def describe(self) -> str:
        """One line for the security screen: what the client's own code host is, by address."""

    def repos(self) -> list[Repo]:
        """Every repository of the scope that is not archived. `exclude_repos` is applied by the caller."""

    def branch_mr(self, repo_id: Id, branch: str) -> MergeRequest | None:
        """The newest merge request from this branch in any state: opened, merged or closed by a person."""

    def create_mr(self, repo_id: Id, *, source_branch: str, target_branch: str, title: str, description: str,
                  label: str) -> MergeRequest:
        """Open a new merge request. The caller has looked for an existing one first."""

    def mr_status(self, repo_id: Id, iid: Id) -> MRStatus: ...

    def notes(self, repo_id: Id, iid: Id, mr_url: str) -> list[Note]:
        """Every note of every thread, so a late reply in an old thread is seen too."""

    def mentions_bot(self, text: str) -> bool:
        """The text addresses the bot the way the host writes a mention: only such a note asks the agent."""

    def failed_jobs(self, repo_id: Id, ci_id: Id) -> list[FailedJob]: ...

    def job_log_tail(self, repo_id: Id, job_id: Id, limit: int) -> str: ...

    def reply(self, repo_id: Id, iid: Id, discussion_id: str, body: str) -> None:
        """Answer in the thread as the bot."""

    def description(self, repo_id: Id, iid: Id) -> str: ...

    def set_description(self, repo_id: Id, iid: Id, description: str) -> None: ...

    def note_mr(self, repo_id: Id, iid: Id, body: str) -> None:
        """A plain comment on the merge request."""

    def close_mr(self, repo_id: Id, iid: Id) -> None: ...

    def upload(self, repo_id: Id, name: str, data: bytes, mime: str) -> str:
        """Store a file for the merge request's description; returns the URL Markdown links to."""

    def git_auth(self) -> GitAuth:
        """The bot's credentials for git over HTTPS on this host."""

    def ping(self) -> None:
        """A cheap authenticated request: raises a ServiceError when the host or the token is not fine."""

    def token_check(self, warn_days: int, today: date | None = None) -> Check | None:
        """The token's expiry while the runner runs (hourly); None when the host cannot tell."""

    def check(self, warn_days: int) -> list[Check]:
        """`arceus-runner check`: the token (and its expiry, `warn_days` ahead), the bot's identity, its
        rights, the scope and its repositories."""


# -- registry --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackerType:
    config: type[IssueTrackerConfig]
    build: Callable[..., IssueTracker]  # (section, token, http config, transport=None)


@dataclass(frozen=True)
class CodeHostType:
    config: type[CodeHostConfig]
    build: Callable[..., CodeHost]


BUILTIN_TRACKERS = {"youtrack": "arceus_runner.youtrack"}
BUILTIN_CODE_HOSTS = {"gitlab": "arceus_runner.gitlab"}
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _provider(kind: str, name: object, builtin: dict[str, str], attr: str):
    """The provider `type:` names: a built-in one, or an importable module that defines `attr`."""
    if not isinstance(name, str) or not _MODULE.match(name):
        raise ValueError(f"type: {name!r} is not the name of {kind}")
    module_name = builtin.get(name, name)
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        if name in builtin:
            raise  # Arceus's own provider is broken: the import error says why
        raise ValueError(f"type: unknown {kind} {name!r} ({e}); built in: {', '.join(sorted(builtin))}, "
                         f"or the path of an importable Python module that defines {attr}") from None
    provider = getattr(module, attr, None)
    if provider is None:
        raise ValueError(f"type: module {module_name!r} does not define {attr}, so it cannot be used as {kind}")
    return provider


def tracker_type(name: object) -> TrackerType:
    return _provider("an issue tracker", name, BUILTIN_TRACKERS, "TRACKER")


def code_host_type(name: object) -> CodeHostType:
    return _provider("a code host", name, BUILTIN_CODE_HOSTS, "CODE_HOST")


def build_tracker(cfg: Config, secrets: Secrets, transport=None) -> IssueTracker:
    return tracker_type(cfg.tracker.type).build(cfg.tracker, secrets.tracker, cfg.http, transport)


def build_code_host(cfg: Config, secrets: Secrets, transport=None) -> CodeHost:
    return code_host_type(cfg.code_host.type).build(cfg.code_host, secrets.code_host, cfg.http, transport)


def usable_repos(cfg: Config, repos: list[Repo]) -> list[Repo]:
    """The repositories the agent gets: the scope's, less `code_host.exclude_repos`."""
    excluded = cfg.code_host.exclude_repos
    return [r for r in repos if r.relative_path not in excluded and r.path not in excluded]
