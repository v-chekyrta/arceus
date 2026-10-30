"""GitLab as Arceus's code host: the provider that ships with it (integrations.CodeHost).

Three layers, top to bottom: the `code_host: {type: gitlab}` config section (GitLabConfig), the
adapter the runner's flow calls (GitLabHost), and the REST client with only the calls it needs
(GitLab, GitLabWriter, GitLabReview). A new host can follow the same shape.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from urllib.parse import quote

from pydantic import Field, field_validator

from arceus_runner.checks import Check, Status
from arceus_runner.config import CodeHostConfig, HttpConfig, _url
from arceus_runner.gitops import GitAuth
from arceus_runner.http import AuthError, BadRequest, NotFound, ServiceClient, ServiceError
from arceus_runner.integrations import (
    Approval,
    CIStatus,
    CodeHostType,
    FailedJob,
    Id,
    MergeRequest,
    MRStatus,
    Note,
    Repo,
)

PAGE = 100
UPLOAD_TIMEOUT = 120  # seconds: a recording is megabytes, more than the usual read timeout allows


class GitLab:
    def __init__(self, client: ServiceClient):
        self.c = client

    def paged(self, path: str, params: dict | None = None) -> Iterator[dict]:
        page = 1
        while True:
            resp = self.c.request("GET", path, params={**(params or {}), "per_page": PAGE, "page": page})
            batch = resp.json()
            yield from batch
            nxt = resp.headers.get("x-next-page")
            if not nxt or not batch:
                return
            page = int(nxt)

    def me(self) -> dict:
        return self.c.get_json("/api/v4/user")

    def token_self(self) -> dict:
        return self.c.get_json("/api/v4/personal_access_tokens/self")

    def group(self, full_path: str) -> dict:
        return self.c.get_json(f"/api/v4/groups/{quote(full_path, safe='')}", {"with_projects": "false"})

    def group_repos(self, group_path: str, include_subgroups: bool) -> list[Repo]:
        group = self.group(group_path)
        prefix = group["full_path"].rstrip("/") + "/"
        params = {
            "include_subgroups": "true" if include_subgroups else "false",
            "with_shared": "false",
            "archived": "false",
            "simple": "true",
        }
        out = []
        for p in self.paged(f"/api/v4/groups/{group['id']}/projects", params):
            full = p["path_with_namespace"]
            out.append(
                Repo(
                    id=p["id"],
                    path=p["path"],
                    relative_path=full[len(prefix):] if full.startswith(prefix) else full,
                    default_branch=p.get("default_branch"),
                    archived=bool(p.get("archived")),
                    http_url=p.get("http_url_to_repo") or "",
                    description=(p.get("description") or "").strip(),
                )
            )
        return out


def _state(value: str | None) -> str:
    # `locked` is an open MR that GitLab is merging this very moment: still open until it says merged.
    return "opened" if value == "locked" else value or ""


def _mr(d: dict) -> MergeRequest:
    closed = d.get("closed_at")
    return MergeRequest(iid=d["iid"], url=d.get("web_url") or "", state=_state(d.get("state")),
                        closed_at_ms=int(datetime.fromisoformat(closed.replace("Z", "+00:00")).timestamp() * 1000)
                        if closed else None, target=d.get("target_branch") or "")


class GitLabWriter(GitLab):
    def open_mr(self, project_id: int, source_branch: str) -> MergeRequest | None:
        items = self.c.get_json(f"/api/v4/projects/{project_id}/merge_requests",
                                {"source_branch": source_branch, "state": "opened"})
        return _mr(items[0]) if items else None

    def branch_mr(self, project_id: int, source_branch: str) -> MergeRequest | None:
        """The newest MR from this branch in any state: opened, merged or closed by a person."""
        items = self.c.get_json(f"/api/v4/projects/{project_id}/merge_requests",
                                {"source_branch": source_branch, "state": "all", "order_by": "created_at",
                                 "sort": "desc"})
        return _mr(items[0]) if items else None

    def create_mr(self, project_id: int, *, source_branch: str, target_branch: str, title: str, description: str,
                  label: str) -> MergeRequest:
        resp = self.c.request("POST", f"/api/v4/projects/{project_id}/merge_requests", json={
            "source_branch": source_branch, "target_branch": target_branch, "title": title,
            "description": description, "labels": label, "remove_source_branch": False,
        })
        return _mr(resp.json())


# GitLab works mergeability out asynchronously: until it has, it cannot say whether there is a conflict.
_MERGEABILITY_PENDING = {"checking", "unchecked", "preparing", "approvals_syncing"}
_CI_RUNNING = {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}


def _ci_status(status: str | None) -> CIStatus | None:
    if not status:
        return None
    if status in ("success", "failed"):
        return status
    return "running" if status in _CI_RUNNING else "other"


def _conflict(detailed: str) -> bool | None:
    if detailed == "conflict":
        return True
    return None if not detailed or detailed in _MERGEABILITY_PENDING else False


class GitLabReview(GitLabWriter):
    def mr_status(self, project_id: int, iid: int) -> MRStatus:
        d = self.c.get_json(f"/api/v4/projects/{project_id}/merge_requests/{iid}")
        p = d.get("head_pipeline") or {}
        return MRStatus(
            state=_state(d.get("state")), sha=d.get("sha") or "",
            conflict=_conflict(d.get("detailed_merge_status") or d.get("merge_status") or ""),
            ci_id=p.get("id"), ci_status=_ci_status(p.get("status")), ci_sha=p.get("sha"), ci_url=p.get("web_url"),
            closed_by=(d.get("closed_by") or {}).get("username") or "", target=d.get("target_branch") or "")

    def notes(self, project_id: int, iid: int, mr_url: str) -> list[Note]:
        """Every note of every discussion, so a late reply in an old thread is seen too."""
        out = []
        for disc in self.paged(f"/api/v4/projects/{project_id}/merge_requests/{iid}/discussions"):
            for n in disc.get("notes") or []:
                pos = n.get("position") or {}
                out.append(Note(
                    id=n["id"], discussion_id=disc["id"], author=(n.get("author") or {}).get("username") or "",
                    body=n.get("body") or "", system=bool(n.get("system")), created_at=n.get("created_at") or "",
                    path=pos.get("new_path") or pos.get("old_path"), line=pos.get("new_line") or pos.get("old_line"),
                    url=f"{mr_url}#note_{n['id']}", commit=pos.get("head_sha")))
        return out

    def failed_jobs(self, project_id: int, pipeline_id: int) -> list[FailedJob]:
        # Retried attempts are left out of this listing by GitLab: only each job's latest run is here.
        return [FailedJob(j["id"], j.get("name") or "", j.get("stage") or "", j.get("web_url") or "")
                for j in self.paged(f"/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs",
                                    {"scope[]": "failed"})]

    def job_trace_tail(self, project_id: int, job_id: int, limit: int = 3000) -> str:
        text = self.c.request("GET", f"/api/v4/projects/{project_id}/jobs/{job_id}/trace").text
        return text[-limit:]

    def reply(self, project_id: int, iid: int, discussion_id: str, body: str) -> int:
        resp = self.c.request("POST", f"/api/v4/projects/{project_id}/merge_requests/{iid}/discussions/"
                                      f"{discussion_id}/notes", json={"body": body})
        return resp.json()["id"]

    def description(self, project_id: int, iid: int) -> str:
        return self.c.get_json(f"/api/v4/projects/{project_id}/merge_requests/{iid}").get("description") or ""

    def upload(self, project_id: int, name: str, data: bytes, mime: str) -> str:
        """A file in the project's uploads; returns its URL, relative to the project, for Markdown."""
        resp = self.c.request("POST", f"/api/v4/projects/{project_id}/uploads", files={"file": (name, data, mime)},
                              timeout=UPLOAD_TIMEOUT)
        return resp.json()["url"]

    def set_description(self, project_id: int, iid: int, description: str) -> None:
        self.c.request("PUT", f"/api/v4/projects/{project_id}/merge_requests/{iid}", json={"description": description})

    def note_mr(self, project_id: int, iid: int, body: str) -> None:
        self.c.request("POST", f"/api/v4/projects/{project_id}/merge_requests/{iid}/notes", json={"body": body})

    def close_mr(self, project_id: int, iid: int) -> None:
        self.c.request("PUT", f"/api/v4/projects/{project_id}/merge_requests/{iid}", json={"state_event": "close"})


class GitLabApprover(GitLab):
    """The approver's own client: a refusal (401/403) pauses this token's client, never the bot's."""

    def approved_by(self, project_id: int, iid: int) -> set[str]:
        d = self.c.get_json(f"/api/v4/projects/{project_id}/merge_requests/{iid}/approvals")
        return {(a.get("user") or {}).get("username") or "" for a in d.get("approved_by") or []}

    def approve(self, project_id: int, iid: int, sha: str) -> None:
        # `sha`: GitLab answers 409 when the source branch is no longer at it, so a newer push is never approved.
        self.c.request("POST", f"/api/v4/projects/{project_id}/merge_requests/{iid}/approve", json={"sha": sha})

    def unapprove(self, project_id: int, iid: int) -> None:
        self.c.request("POST", f"/api/v4/projects/{project_id}/merge_requests/{iid}/unapprove")

    def note_mr(self, project_id: int, iid: int, body: str) -> None:
        """The code review's note, signed by the approver rather than by the bot that wrote the code."""
        self.c.request("POST", f"/api/v4/projects/{project_id}/merge_requests/{iid}/notes", json={"body": body})


# -- the code host ----------------------------------------------------------------------------------


class GitLabConfig(CodeHostConfig):
    url: str
    group: str = Field(min_length=1)  # the group that holds all the project's repositories
    token_env: str = "GITLAB_TOKEN"
    include_subgroups: bool = True

    _check_url = field_validator("url")(_url)

    @field_validator("group")
    @classmethod
    def _group(cls, v: str) -> str:
        return v.strip("/")


def mentions(bot: str) -> re.Pattern:
    """`@bot` as GitLab writes a mention: not part of an e-mail address or of a longer username."""
    return re.compile(rf"(?<![\w.@-])@{re.escape(bot)}(?![\w-])", re.IGNORECASE)


class GitLabHost:
    """integrations.CodeHost on GitLab: the repositories of one group, its MRs, discussions and pipelines."""

    name = "gitlab"
    label = "GitLab"
    scope_kind = "group"
    supports_uploads = True

    def __init__(self, cfg: GitLabConfig, client: ServiceClient, token: str, approver: ServiceClient | None = None):
        self.cfg = cfg
        self.client = client
        self.api = GitLabReview(client)
        self.scope = cfg.group
        self._token = token
        self._mention = mentions(cfg.bot_user)
        self.approver_client = approver
        self.approver_api = GitLabApprover(approver) if approver else None
        self.approver = cfg.approver.user if cfg.approver and approver else ""

    @property
    def supports_approval(self) -> bool:
        return self.approver_api is not None

    @property
    def reviewer(self) -> str:
        return self.approver or self.cfg.bot_user

    def note_review(self, repo_id: Id, iid: Id, body: str) -> None:
        # The bot posts it when the approver cannot: its token was refused (that pauses its client) or lost its
        # rights. A review that cannot be signed by the reviewer is still a review people must see.
        if self.approver_api and self.approver_client.available():
            try:
                self.approver_api.note_mr(repo_id, iid, body)
                return
            except AuthError:
                pass
        self.api.note_mr(repo_id, iid, body)

    def identity(self) -> str:
        return f"gitlab {self.cfg.url} group {self.cfg.group}"

    def describe(self) -> str:
        return f"GitLab at {self.cfg.url}, the repositories of group {self.cfg.group}"

    def repos(self) -> list[Repo]:
        return self.api.group_repos(self.cfg.group, self.cfg.include_subgroups)

    def branch_mr(self, repo_id: Id, branch: str) -> MergeRequest | None:
        return self.api.branch_mr(repo_id, branch)

    def create_mr(self, repo_id: Id, *, source_branch: str, target_branch: str, title: str, description: str,
                  label: str) -> MergeRequest:
        return self.api.create_mr(repo_id, source_branch=source_branch, target_branch=target_branch, title=title,
                                  description=description, label=label)

    def mr_status(self, repo_id: Id, iid: Id) -> MRStatus:
        return self.api.mr_status(repo_id, iid)

    def notes(self, repo_id: Id, iid: Id, mr_url: str) -> list[Note]:
        return self.api.notes(repo_id, iid, mr_url)

    def mentions_bot(self, text: str) -> bool:
        return bool(self._mention.search(text))

    def failed_jobs(self, repo_id: Id, ci_id: Id) -> list[FailedJob]:
        return self.api.failed_jobs(repo_id, ci_id)

    def job_log_tail(self, repo_id: Id, job_id: Id, limit: int) -> str:
        return self.api.job_trace_tail(repo_id, job_id, limit)

    def reply(self, repo_id: Id, iid: Id, discussion_id: str, body: str) -> None:
        self.api.reply(repo_id, iid, discussion_id, body)

    def description(self, repo_id: Id, iid: Id) -> str:
        return self.api.description(repo_id, iid)

    def set_description(self, repo_id: Id, iid: Id, description: str) -> None:
        self.api.set_description(repo_id, iid, description)

    def note_mr(self, repo_id: Id, iid: Id, body: str) -> None:
        self.api.note_mr(repo_id, iid, body)

    def close_mr(self, repo_id: Id, iid: Id) -> None:
        self.api.close_mr(repo_id, iid)

    def upload(self, repo_id: Id, name: str, data: bytes, mime: str) -> str:
        return self.api.upload(repo_id, name, data, mime)

    def approve(self, repo_id: Id, iid: Id, sha: str) -> Approval:
        a = self.approver_api
        if a is None:
            return Approval(False, "no approver account is configured")
        try:
            if self.approver in a.approved_by(repo_id, iid):
                # An approval GitLab kept across a push is not one for `sha`: take it back rather than claim it.
                if (a.c.get_json(f"/api/v4/projects/{repo_id}/merge_requests/{iid}").get("sha") or sha) != sha:
                    a.unapprove(repo_id, iid)
                    return Approval(False, "the merge request has commits newer than the reviewed one", stale=True)
                return Approval(True, "already approved")
            a.approve(repo_id, iid, sha)
        except AuthError as e:  # the account may not approve here: the author rule, its role, a lost token
            return Approval(False, f"GitLab refused the approval by {self.approver}: {e}")
        except NotFound:
            return Approval(False, f"GitLab did not find the merge request for {self.approver}")
        except BadRequest as e:
            if e.status == 409:
                return Approval(False, "the merge request has commits newer than the reviewed one", stale=True)
            return Approval(False, f"GitLab refused the approval: {e}")
        return Approval(True)

    def revoke(self, repo_id: Id, iid: Id) -> bool:
        a = self.approver_api
        if a is None:
            return False
        try:
            if self.approver not in a.approved_by(repo_id, iid):
                return False
            a.unapprove(repo_id, iid)
        except (AuthError, NotFound, BadRequest):
            return False
        return True

    def git_auth(self) -> GitAuth:
        return GitAuth(self.cfg.url, self.cfg.bot_user, self._token)

    def ping(self) -> None:
        self.api.me()

    def token_check(self, warn_days: int, today: date | None = None) -> Check | None:
        try:
            return _expiry_check(warn_days, self.api.token_self(), today or datetime.now(UTC).date())
        except ServiceError as e:
            return Check("gitlab.token_expiry", "warn", f"cannot read token info: {e}")

    def check(self, warn_days: int, today: date | None = None) -> list[Check]:
        out = check_gitlab(self.cfg, self.api, warn_days, today)
        if self.approver_api:
            out.append(_check_approver(self.cfg, self.approver_api))
        return out


def gitlab_client(cfg: GitLabConfig, token: str, http: HttpConfig, transport=None) -> ServiceClient:
    return ServiceClient("gitlab", cfg.url, {"PRIVATE-TOKEN": token}, http, transport=transport)


def check_gitlab(g: GitLabConfig, gl: GitLab, warn_days: int, today: date | None = None) -> list[Check]:
    """Every mistake in the section on its own line: the token and its rights, the bot, the group."""
    out: list[Check] = []
    today = today or datetime.now(UTC).date()
    try:
        me = gl.me()
    except AuthError as e:
        return [Check("gitlab.auth", "fail", str(e))]
    except ServiceError as e:
        return [Check("gitlab.reachable", "fail", str(e))]
    if me.get("username") != g.bot_user:
        out.append(Check("gitlab.bot_user", "fail",
                         f"token belongs to {me.get('username')!r}, config says {g.bot_user!r}"))
    else:
        out.append(Check("gitlab.bot_user", "ok", g.bot_user))

    try:
        tok = gl.token_self()
        scopes = tok.get("scopes") or []
        if "api" not in scopes:
            out.append(Check("gitlab.token_scopes", "fail", f"token needs scope 'api', has {scopes}"))
        else:
            out.append(Check("gitlab.token_scopes", "ok", ", ".join(scopes)))
        out.append(_expiry_check(warn_days, tok, today))
    except ServiceError as e:
        out.append(Check("gitlab.token_expiry", "warn", f"cannot read token info: {e}"))

    try:
        repos = gl.group_repos(g.group, g.include_subgroups)
    except ServiceError as e:
        out.append(Check("gitlab.group", "fail", f"group {g.group!r}: {e}"))
        return out
    usable = [r for r in repos if r.relative_path not in g.exclude_repos and r.path not in g.exclude_repos]
    out.append(Check("gitlab.group", "ok", f"{g.group}: {len(usable)} repositories ({len(repos) - len(usable)} excluded)"))

    if not usable:
        out.append(Check("gitlab.repositories", "fail", f"no repositories left in {g.group} after exclude_repos"))
    for name in g.exclude_repos:
        if not any(name in (r.relative_path, r.path) for r in repos):
            out.append(Check(f"gitlab.exclude_repos.{name}", "warn", f"{name!r} is not in the group"))
    return out


def _check_approver(g: GitLabConfig, gl: GitLab) -> Check:
    """The approver's token is the approver's and may use the API; whether it may approve in each repository
    (its role, the project's approval settings) only an approval shows, and the review says so when refused."""
    want = g.approver.user
    try:
        me = gl.me()
        scopes = gl.token_self().get("scopes") or []
    except ServiceError as e:
        return Check("gitlab.approver", "fail", f"{want}: {e}")
    if me.get("username") != want:
        return Check("gitlab.approver", "fail", f"the approver's token belongs to {me.get('username')!r}, "
                                                f"config says {want!r}")
    if "api" not in scopes:
        return Check("gitlab.approver", "fail", f"the approver's token needs scope 'api', has {scopes}")
    return Check("gitlab.approver", "ok", f"{want}; it needs at least the Developer role in every repository "
                                          "of the group to approve")


def _expiry_check(warn_days: int, tok: dict, today: date) -> Check:
    exp = tok.get("expires_at")
    if not exp:
        return Check("gitlab.token_expiry", "warn", "token has no expiry date; set one")
    left = (date.fromisoformat(exp) - today).days
    status: Status = "fail" if left < 0 else "warn" if left < warn_days else "ok"
    return Check("gitlab.token_expiry", status, f"expires {exp} ({left} days)")


def _build(cfg: GitLabConfig, token: str, http: HttpConfig, transport=None, approver_token: str | None = None) \
        -> GitLabHost:
    approver = None
    if cfg.approver and approver_token:
        approver = ServiceClient("gitlab-approver", cfg.url, {"PRIVATE-TOKEN": approver_token}, http,
                                 transport=transport)
    return GitLabHost(cfg, gitlab_client(cfg, token, http, transport), token, approver)


CODE_HOST = CodeHostType(config=GitLabConfig, build=_build)
