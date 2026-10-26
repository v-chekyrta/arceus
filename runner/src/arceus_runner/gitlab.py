"""GitLab REST client: only the calls the runner needs."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from arceus_runner.http import ServiceClient

PAGE = 100
UPLOAD_TIMEOUT = 120  # seconds: a recording is megabytes, more than the usual read timeout allows


@dataclass(frozen=True)
class Repo:
    id: int
    path: str  # last path segment
    relative_path: str  # path below the configured group, e.g. "sub/repo"
    default_branch: str | None
    archived: bool
    http_url: str
    description: str = ""


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


def resolve_repo(repos: list[Repo], name: str) -> tuple[Repo | None, str | None]:
    """Find a repository by its path below the group, or by its short path if that is unique."""
    exact = [r for r in repos if r.relative_path == name]
    if exact:
        return exact[0], None
    short = [r for r in repos if r.path == name]
    if len(short) == 1:
        return short[0], None
    if len(short) > 1:
        options = ", ".join(sorted(r.relative_path for r in short))
        return None, f"{name!r} is ambiguous, write one of: {options}"
    return None, f"{name!r} is not in the group"


@dataclass(frozen=True)
class MergeRequest:
    iid: int
    url: str
    state: str
    closed_at_ms: int | None = None


def _mr(d: dict) -> MergeRequest:
    closed = d.get("closed_at")
    return MergeRequest(iid=d["iid"], url=d.get("web_url") or "", state=d.get("state") or "",
                        closed_at_ms=int(datetime.fromisoformat(closed.replace("Z", "+00:00")).timestamp() * 1000)
                        if closed else None)


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
                  label: str, after_closed: bool = False) -> MergeRequest:
        """Create the MR unless this branch already has one (a retry after a crash, or a person closed it).

        `after_closed`: a person asked for more work after closing the branch's MR, so a closed one
        is not the answer and a new MR is opened next to it.
        """
        existing = self.branch_mr(project_id, source_branch)
        if existing and not (after_closed and existing.state == "closed"):
            return existing
        resp = self.c.request("POST", f"/api/v4/projects/{project_id}/merge_requests", json={
            "source_branch": source_branch, "target_branch": target_branch, "title": title,
            "description": description, "labels": label, "remove_source_branch": False,
        })
        return _mr(resp.json())


@dataclass(frozen=True)
class MRStatus:
    """What the review loop needs to know about one MR right now."""
    state: str                 # opened | merged | closed | locked
    merge_status: str          # detailed_merge_status: mergeable, conflict, checking, ci_must_pass, ...
    has_conflicts: bool
    sha: str                   # head of the source branch as GitLab sees it
    pipeline_id: int | None
    pipeline_status: str | None  # success, failed, running, pending, canceled, manual, skipped, ...
    pipeline_sha: str | None
    pipeline_url: str | None
    closed_by: str = ""        # who closed it, when it is closed


@dataclass(frozen=True)
class Note:
    id: int
    discussion_id: str
    author: str
    body: str
    system: bool
    created_at: str
    path: str | None           # file and line for a comment on the diff
    line: int | None
    url: str
    commit: str | None = None  # the MR version a diff comment was written on (position.head_sha)


@dataclass(frozen=True)
class FailedJob:
    id: int
    name: str
    stage: str
    url: str


class GitLabReview(GitLabWriter):
    def mr_status(self, project_id: int, iid: int) -> MRStatus:
        d = self.c.get_json(f"/api/v4/projects/{project_id}/merge_requests/{iid}")
        p = d.get("head_pipeline") or {}
        return MRStatus(
            state=d.get("state") or "", merge_status=d.get("detailed_merge_status") or d.get("merge_status") or "",
            has_conflicts=bool(d.get("has_conflicts")), sha=d.get("sha") or "",
            pipeline_id=p.get("id"), pipeline_status=p.get("status"), pipeline_sha=p.get("sha"),
            pipeline_url=p.get("web_url"), closed_by=(d.get("closed_by") or {}).get("username") or "")

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
