"""An issue tracker and a code host that live in memory: a provider module from outside the runner.

config.yaml names it by module path, `type: tests.memory_providers`, the way a Jira or GitHub module
of someone's own would be named. It imports nothing of YouTrack or GitLab, and it differs from them
where a provider may: string ids everywhere, some of them shaped like numbers ("00042", "1e3"), other URLs, `[~login]` mentions, no CI job logs, no
conflict detection, no uploads, no attachments. `lose_response` makes the next write of that name
take effect and then fail, as a connection that breaks after the server has done the work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import httpx

from arceus_runner.checks import Check
from arceus_runner.config import CodeHostConfig, HttpConfig, IssueTrackerConfig
from arceus_runner.gitops import GitAuth
from arceus_runner.http import ServiceClient, TransientError
from arceus_runner.integrations import (
    CodeHostType,
    Comment,
    FailedJob,
    Issue,
    IssueDetails,
    MergeRequest,
    MRStatus,
    Note,
    Repo,
    TrackerType,
    Trigger,
)


def _client(name: str, url: str, http: HttpConfig) -> ServiceClient:
    # Nothing goes over HTTP; the client is here for its pause and backoff, which the runner reads.
    return ServiceClient(name, url, {}, http, transport=httpx.MockTransport(lambda r: httpx.Response(599)))


# -- the tracker ------------------------------------------------------------------------------------


class MemoryTrackerConfig(IssueTrackerConfig):
    token_env: str = "MEMORY_TRACKER_TOKEN"
    base_url: str = "https://tracker.test"


@dataclass
class Ticket:
    db_id: str
    key: str
    summary: str
    description: str
    state: str
    created: int
    tags: set[str] = field(default_factory=set)
    history: list[tuple[str, str, str, int]] = field(default_factory=list)  # (event id, author, +tag/-tag, ms)
    comments: list[Comment] = field(default_factory=list)
    assignee: str | None = None


class MemoryTracker:
    name = "memtracker"
    label = "Memory Tracker"
    engine_source = "tracker"
    supports_attachments = False

    def __init__(self, cfg: MemoryTrackerConfig, http: HttpConfig):
        self.cfg = cfg
        self.client = _client(self.name, cfg.base_url, http)
        self.tickets: dict[str, Ticket] = {}
        self.users = {"j.doe", "a.smith", "mallory", cfg.bot_user}
        self.clock = 1_792_479_600_000
        self.lose_response: set[str] = set()

    # what a test does on the tracker's side
    def file(self, n: int, summary: str, queued_by: str | None, state: str | None = None) -> Ticket:
        self.clock += 1
        t = Ticket(f"tkt-{n:04d}", f"ACME-{n}", summary, "Task description", state or self.cfg.states.queued,
                   self.clock)
        if queued_by is not None:
            t.tags.add(self.cfg.tags.queue)
            t.history.append((f"evt-{n}-{self.clock}", queued_by, "+" + self.cfg.tags.queue, self.clock))
        self.tickets[t.db_id] = t
        return t

    def retag(self, key: str, author: str | None) -> None:
        """The queue tag taken off and put on again; by nobody the history names when `author` is None."""
        t = self._ticket(key)
        self.clock += 1
        t.history.append((f"evt-off-{self.clock}", author or "", "-" + self.cfg.tags.queue, self.clock))
        if author is not None:
            self.clock += 1
            t.history.append((f"evt-on-{self.clock}", author, "+" + self.cfg.tags.queue, self.clock))

    def say(self, key: str, author: str, text: str) -> None:
        self.clock += 1
        t = self._ticket(key)
        t.comments.append(Comment(f"cmt-{self.clock}", text, author, self.clock))

    def _ticket(self, ref: str) -> Ticket:
        for t in self.tickets.values():
            if ref in (t.db_id, t.key):
                return t
        raise KeyError(ref)

    def _lost(self, write: str) -> None:
        if write in self.lose_response:
            self.lose_response.discard(write)
            raise TransientError(self.name, f"connection reset after {write}")

    # integrations.IssueTracker
    def identity(self) -> str:
        return f"memtracker {self.cfg.base_url}"

    def describe(self) -> str:
        return f"Memory Tracker at {self.cfg.base_url}"

    def queued(self) -> list[Issue]:
        c = self.cfg
        return [Issue(t.db_id, t.key, t.summary, t.created, t.created)
                for t in sorted(self.tickets.values(), key=lambda t: t.created)
                if t.state == c.states.queued and c.tags.queue in t.tags]

    def trigger(self, issue: Issue) -> Trigger | None:
        last = None
        for event, author, change, ms in self._ticket(issue.db_id).history:
            if change == "+" + self.cfg.tags.queue:
                last = Trigger(event, author, ms)
            elif change == "-" + self.cfg.tags.queue:
                last = None
        return last

    def issue(self, key: str) -> IssueDetails:
        t = self._ticket(key)
        return IssueDetails(t.db_id, t.key, t.summary, t.description, t.state)

    def tags(self, db_id: str) -> set[str]:
        return set(self._ticket(db_id).tags)

    def add_tag(self, db_id: str, name: str) -> None:
        self._ticket(db_id).tags.add(name)

    def remove_tag(self, db_id: str, name: str) -> None:
        self._ticket(db_id).tags.discard(name)

    def set_state(self, db_id: str, state: str) -> None:
        self._ticket(db_id).state = state

    def assign_to_bot(self, db_id: str) -> None:
        self._ticket(db_id).assignee = self.cfg.bot_user

    def comments(self, db_id: str) -> list[Comment]:
        return list(self._ticket(db_id).comments)

    def add_comment(self, db_id: str, text: str) -> tuple[str, int]:
        self.clock += 1
        c = Comment(f"cmt-{self.clock}", text, self.cfg.bot_user, self.clock)
        self._ticket(db_id).comments.append(c)
        self._lost("add_comment")
        return c.id, c.created

    def attach(self, db_id: str, comment_id: str, name: str, data: bytes, mime: str) -> None:
        raise AssertionError("supports_attachments is False: the runner must not attach")

    def attachment_markdown(self, name: str, caption: str) -> str:
        raise AssertionError("supports_attachments is False")

    def issue_url(self, key: str) -> str:
        return f"{self.cfg.base_url}/browse/{key}"

    def mention(self, user: str) -> str:
        return f"[~{user}]"

    def user_exists(self, login: str) -> bool:
        return login in self.users

    def check(self) -> list[Check]:
        return [Check("memtracker.auth", "ok", self.cfg.bot_user)]


# -- the code host ----------------------------------------------------------------------------------


class MemoryHostConfig(CodeHostConfig):
    token_env: str = "MEMORY_HOST_TOKEN"
    base_url: str = "https://host.test"
    org: str = "acme"


@dataclass
class PullRequest:
    repo_id: str
    iid: str
    source: str
    target: str
    title: str
    description: str
    label: str
    state: str = "opened"
    closed_at_ms: int | None = None
    ci: dict | None = None  # {"id", "sha", "status", "url"}
    threads: dict[str, list[Note]] = field(default_factory=dict)


# Strings a careless store would read as the numbers 42, 1000 and 7.
REPO_IDS = {"acme-platform": "00042", "acme-theme": "1e3", "acme-infra": "007"}


class MemoryHost:
    name = "memhost"
    label = "Memory Host"
    scope_kind = "organization"
    supports_uploads = False

    def __init__(self, cfg: MemoryHostConfig, http: HttpConfig, token: str):
        self.cfg = cfg
        self.scope = cfg.org
        self.client = _client(self.name, cfg.base_url, http)
        self._token = token
        self.urls: dict[str, str] = {}  # repository name -> what git clones
        self.repo_ids = dict(REPO_IDS)
        self.prs: list[PullRequest] = []
        self.seq = 0
        self.lose_response: set[str] = set()

    # what a test does on the host's side
    def pr(self, n: int = 0) -> PullRequest:
        return self.prs[n]

    def comment(self, pr: PullRequest, author: str, body: str, thread: str | None = None,
                note_id: str | None = None) -> str:
        self.seq += 1
        thread = thread or f"thr-{self.seq}"
        note_id = note_id or f"nt-{self.seq}"
        note = Note(note_id, thread, author, body, False, "2026-10-20T07:00:00Z", None, None,
                    f"{self.cfg.base_url}/{pr.repo_id}/pull/{pr.iid}#{note_id}")
        pr.threads.setdefault(thread, []).append(note)
        return thread

    def _pr(self, repo_id, iid) -> PullRequest:
        return next(p for p in self.prs if p.repo_id == repo_id and p.iid == iid)

    def _url(self, pr: PullRequest) -> str:
        return f"{self.cfg.base_url}/{pr.repo_id}/pull/{pr.iid}"

    def _mr(self, pr: PullRequest) -> MergeRequest:
        return MergeRequest(pr.iid, self._url(pr), pr.state, pr.closed_at_ms, pr.target)

    # integrations.CodeHost
    def identity(self) -> str:
        return f"memhost {self.cfg.base_url} org {self.cfg.org}"

    def describe(self) -> str:
        return f"Memory Host at {self.cfg.base_url}, organization {self.cfg.org}"

    def repos(self) -> list[Repo]:
        return [Repo(self.repo_ids[name], name, name, "main", False, url) for name, url in sorted(self.urls.items())]

    def branch_mr(self, repo_id, branch: str) -> MergeRequest | None:
        mine = [p for p in self.prs if p.repo_id == repo_id and p.source == branch]
        return self._mr(mine[-1]) if mine else None

    def create_mr(self, repo_id, *, source_branch: str, target_branch: str, title: str, description: str,
                  label: str) -> MergeRequest:
        self.seq += 1
        pr = PullRequest(repo_id, f"{self.seq:03d}", source_branch, target_branch, title, description, label)
        self.prs.append(pr)
        return self._mr(pr)

    def mr_status(self, repo_id, iid) -> MRStatus:
        pr = self._pr(repo_id, iid)
        ci = pr.ci or {}
        # This host cannot tell conflicts: None, never a guess.
        return MRStatus(pr.state, ci.get("sha") or "", None, ci_id=ci.get("id"), ci_status=ci.get("status"),
                        ci_sha=ci.get("sha"), ci_url=ci.get("url"))

    def notes(self, repo_id, iid, mr_url: str) -> list[Note]:
        return [n for thread in self._pr(repo_id, iid).threads.values() for n in thread]

    def mentions_bot(self, text: str) -> bool:
        return f"[~{self.cfg.bot_user}]" in text

    def failed_jobs(self, repo_id, ci_id) -> list[FailedJob]:
        return []  # no job logs on this host: the agent gets the run's link

    def job_log_tail(self, repo_id, job_id, limit: int) -> str:
        raise AssertionError("no jobs were listed")

    def reply(self, repo_id, iid, discussion_id: str, body: str) -> None:
        self.comment(self._pr(repo_id, iid), self.cfg.bot_user, body, thread=discussion_id)
        self._lost("reply")

    def description(self, repo_id, iid) -> str:
        return self._pr(repo_id, iid).description

    def set_description(self, repo_id, iid, description: str) -> None:
        self._pr(repo_id, iid).description = description

    def note_mr(self, repo_id, iid, body: str) -> None:
        self.comment(self._pr(repo_id, iid), self.cfg.bot_user, body)

    def close_mr(self, repo_id, iid) -> None:
        self._pr(repo_id, iid).state = "closed"

    def upload(self, repo_id, name: str, data: bytes, mime: str) -> str:
        raise AssertionError("supports_uploads is False: the runner must not upload")

    def git_auth(self) -> GitAuth:
        return GitAuth(self.cfg.base_url, self.cfg.bot_user, self._token)

    def ping(self) -> None:
        pass

    def token_check(self, warn_days: int, today: date | None = None) -> Check | None:
        return None  # this host cannot tell when a token expires

    def check(self, warn_days: int) -> list[Check]:
        return [Check("memhost.auth", "ok", self.cfg.bot_user)]

    def _lost(self, write: str) -> None:
        if write in self.lose_response:
            self.lose_response.discard(write)
            raise TransientError(self.name, f"connection reset after {write}")


TRACKER = TrackerType(config=MemoryTrackerConfig, build=lambda cfg, token, http, transport=None: MemoryTracker(cfg, http))
CODE_HOST = CodeHostType(config=MemoryHostConfig,
                         build=lambda cfg, token, http, transport=None: MemoryHost(cfg, http, token))
