"""YouTrack REST client: only the calls the runner needs.

Collections are paged with $top/$skip (YouTrack returns 42 items by default
without $top); the activity stream is paged with its cursor.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import quote

from arceus_runner.http import ServiceClient

PAGE = 100
UPLOAD_TIMEOUT = 120  # seconds: a recording is megabytes, more than the usual read timeout allows


@dataclass(frozen=True)
class TagEvent:
    activity_id: str
    author: str
    timestamp: int  # ms since epoch, as YouTrack reports it


@dataclass(frozen=True)
class Issue:
    db_id: str
    key: str  # PRJ-123
    summary: str
    created: int
    updated: int


def query_value(v: str) -> str:
    """Quote a value for YouTrack search syntax: {To Do} is safe for spaces and dashes."""
    if "}" in v:
        raise ValueError(f"cannot use {v!r} in a YouTrack query")
    return "{" + v + "}"


class YouTrack:
    def __init__(self, client: ServiceClient):
        self.c = client

    def paged(self, path: str, params: dict | None = None, page: int = PAGE) -> Iterator[dict]:
        skip = 0
        while True:
            batch = self.c.get_json(path, {**(params or {}), "$top": page, "$skip": skip})
            if not isinstance(batch, list):
                raise ValueError(f"YouTrack {path}: expected a list, got {type(batch).__name__}")
            yield from batch
            if len(batch) < page:
                return
            skip += page

    def me(self) -> dict:
        return self.c.get_json("/api/users/me", {"fields": "id,login,name,banned"})

    def find_project(self, short_name: str) -> dict | None:
        for p in self.paged("/api/admin/projects", {"fields": "id,shortName,name,archived"}):
            if p.get("shortName") == short_name:
                return p
        return None

    def project_fields(self, project_id: str) -> list[dict]:
        fields = "id,$type,field(name),bundle(values(name,archived))"
        return list(self.paged(f"/api/admin/projects/{quote(project_id)}/customFields", {"fields": fields}))

    def tag_names(self) -> set[str]:
        return {t["name"] for t in self.paged("/api/tags", {"fields": "id,name"}) if t.get("name")}

    def user_exists(self, login: str) -> bool:
        params = {"fields": "login,banned", "query": login}
        return any(u.get("login") == login and not u.get("banned") for u in self.paged("/api/users", params))

    def search(self, query: str) -> Iterator[Issue]:
        params = {"fields": "id,idReadable,summary,created,updated", "query": query}
        for i in self.paged("/api/issues", params):
            yield Issue(
                db_id=i["id"],
                key=i["idReadable"],
                summary=i.get("summary") or "",
                created=int(i.get("created") or 0),
                updated=int(i.get("updated") or 0),
            )

    def queued_issues(self, project: str, tag: str, state_field: str, state: str) -> list[Issue]:
        q = (
            f"project: {query_value(project)} tag: {query_value(tag)} "
            f"{state_field}: {query_value(state)} sort by: created asc"
        )
        return list(self.search(q))

    def tag_activities(self, issue_id: str) -> Iterator[dict]:
        """All tag changes of an issue, oldest first, following the cursor to the end."""
        fields = "afterCursor,hasAfter,activities(id,timestamp,author(login),added(name),removed(name))"
        cursor: str | None = None
        while True:
            params = {"categories": "TagsCategory", "fields": fields, "$top": PAGE}
            if cursor:
                params["cursor"] = cursor
            page = self.c.get_json(f"/api/issues/{quote(issue_id)}/activitiesPage", params)
            yield from page.get("activities") or []
            if not page.get("hasAfter") or not page.get("afterCursor"):
                return
            cursor = page["afterCursor"]

    def tag_setter(self, issue_id: str, tag: str) -> TagEvent | None:
        """Who put the tag on most recently. None if the tag is not on the issue per history."""
        last: TagEvent | None = None
        for a in self.tag_activities(issue_id):
            added = {t.get("name") for t in (a.get("added") or []) if isinstance(t, dict)}
            removed = {t.get("name") for t in (a.get("removed") or []) if isinstance(t, dict)}
            if tag in removed:
                last = None
            if tag in added:
                last = TagEvent(
                    activity_id=a["id"],
                    author=(a.get("author") or {}).get("login") or "",
                    timestamp=int(a.get("timestamp") or 0),
                )
        return last


@dataclass(frozen=True)
class Comment:
    id: str
    text: str
    author: str
    created: int


@dataclass(frozen=True)
class IssueDetails:
    db_id: str
    key: str
    summary: str
    description: str
    fields: dict[str, list[str]]  # custom field name -> value names (logins for users)
    writers: tuple[str, ...] = ()  # who reported it and who changed it last: the text may be theirs


def _value_names(value: object) -> list[str]:
    items = value if isinstance(value, list) else [value]
    out = []
    for v in items:
        if isinstance(v, dict):
            name = v.get("name") or v.get("login") or v.get("text")
            if name:
                out.append(str(name))
        elif v is not None:
            out.append(str(v))
    return out


class YouTrackWriter(YouTrack):
    """The calls that change an issue. Each one is safe to repeat."""

    def __init__(self, client: ServiceClient):
        super().__init__(client)
        self._tag_ids: dict[str, str] = {}

    def issue(self, issue_id: str) -> IssueDetails:
        data = self.c.get_json(
            f"/api/issues/{quote(issue_id)}",
            {"fields": "id,idReadable,summary,description,reporter(login),updater(login),"
                       "customFields(name,value(name,login,text))"},
        )
        fields = {f["name"]: _value_names(f.get("value")) for f in data.get("customFields") or [] if f.get("name")}
        writers = tuple(dict.fromkeys(u["login"] for u in (data.get("reporter"), data.get("updater"))
                                      if isinstance(u, dict) and u.get("login")))
        return IssueDetails(data["id"], data["idReadable"], data.get("summary") or "", data.get("description") or "",
                            fields, writers)

    def comments(self, issue_id: str) -> list[Comment]:
        fields = "id,text,created,author(login),deleted"
        return [
            Comment(c["id"], c.get("text") or "", (c.get("author") or {}).get("login") or "", int(c.get("created") or 0))
            for c in self.paged(f"/api/issues/{quote(issue_id)}/comments", {"fields": fields})
            if not c.get("deleted")
        ]

    def add_comment(self, issue_id: str, text: str) -> tuple[str, int]:
        resp = self.c.request("POST", f"/api/issues/{quote(issue_id)}/comments", params={"fields": "id,created"},
                              json={"text": text})
        data = resp.json()
        return data["id"], int(data.get("created") or 0)

    def attach_to_comment(self, issue_id: str, comment_id: str, name: str, data: bytes, mime: str) -> None:
        self.c.request("POST", f"/api/issues/{quote(issue_id)}/comments/{quote(comment_id)}/attachments",
                       params={"fields": "id,name"}, files={"file": (name, data, mime)}, timeout=UPLOAD_TIMEOUT)

    def set_state(self, issue_id: str, field: str, state: str) -> None:
        self.c.request("POST", f"/api/issues/{quote(issue_id)}", params={"fields": "id"}, json={
            "customFields": [{"name": field, "$type": "StateIssueCustomField", "value": {"name": state}}]})

    def set_assignee(self, issue_id: str, field: str, login: str) -> None:
        self.c.request("POST", f"/api/issues/{quote(issue_id)}", params={"fields": "id"}, json={
            "customFields": [{"name": field, "$type": "SingleUserIssueCustomField", "value": {"login": login}}]})

    def tag_id(self, name: str) -> str:
        if name not in self._tag_ids:
            for t in self.paged("/api/tags", {"fields": "id,name", "query": name}):
                if t.get("name") == name:
                    self._tag_ids[name] = t["id"]
                    break
            else:
                raise LookupError(f"YouTrack tag {name!r} not found")
        return self._tag_ids[name]

    def issue_tags(self, issue_id: str) -> set[str]:
        return {t["name"] for t in self.paged(f"/api/issues/{quote(issue_id)}/tags", {"fields": "id,name"})}

    def add_tag(self, issue_id: str, name: str) -> None:
        if name not in self.issue_tags(issue_id):
            self.c.request("POST", f"/api/issues/{quote(issue_id)}/tags", params={"fields": "id"},
                           json={"id": self.tag_id(name)})

    def remove_tag(self, issue_id: str, name: str) -> None:
        if name in self.issue_tags(issue_id):
            self.c.request("DELETE", f"/api/issues/{quote(issue_id)}/tags/{quote(self.tag_id(name))}")
