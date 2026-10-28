"""YouTrack as Arceus's issue tracker: the provider that ships with it (integrations.IssueTracker).

Three layers, top to bottom: the `tracker: {type: youtrack}` config section (YouTrackConfig), the
adapter the runner's flow calls (YouTrackTracker), and the REST client with only the calls it needs
(YouTrack, YouTrackWriter). A new tracker can follow the same shape.

Collections are paged with $top/$skip (YouTrack returns 42 items by default
without $top); the activity stream is paged with its cursor.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import quote

from pydantic import Field, field_validator

from arceus_runner.checks import Check
from arceus_runner.config import HttpConfig, IssueTrackerConfig, _url
from arceus_runner.http import AuthError, ServiceClient, ServiceError
from arceus_runner.integrations import Comment, Issue, IssueDetails, Trigger, TrackerType

PAGE = 100
UPLOAD_TIMEOUT = 120  # seconds: a recording is megabytes, more than the usual read timeout allows


@dataclass(frozen=True)
class TagEvent:
    activity_id: str
    author: str
    timestamp: int  # ms since epoch, as YouTrack reports it


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
class YouTrackIssue:
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

    def issue(self, issue_id: str) -> YouTrackIssue:
        data = self.c.get_json(
            f"/api/issues/{quote(issue_id)}",
            {"fields": "id,idReadable,summary,description,reporter(login),updater(login),"
                       "customFields(name,value(name,login,text))"},
        )
        fields = {f["name"]: _value_names(f.get("value")) for f in data.get("customFields") or [] if f.get("name")}
        writers = tuple(dict.fromkeys(u["login"] for u in (data.get("reporter"), data.get("updater"))
                                      if isinstance(u, dict) and u.get("login")))
        return YouTrackIssue(data["id"], data["idReadable"], data.get("summary") or "", data.get("description") or "",
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


# -- the issue tracker ------------------------------------------------------------------------------


class YouTrackConfig(IssueTrackerConfig):
    url: str
    project: str = Field(min_length=1)  # short name (ID) of the YouTrack project
    token_env: str = "YOUTRACK_TOKEN"
    state_field: str = "State"  # the project's state field; `states` are its values
    assignee_field: str | None = "Assignee"  # set to the bot when a task is taken; null to leave it

    _check_url = field_validator("url")(_url)


class YouTrackTracker:
    """integrations.IssueTracker on YouTrack: the queue tag and state from a search, the tag's author from
    the issue's activity stream, states in a custom field."""

    name = "youtrack"
    label = "YouTrack"
    engine_source = "youtrack"
    supports_attachments = True

    def __init__(self, cfg: YouTrackConfig, client: ServiceClient):
        self.cfg = cfg
        self.client = client
        self.api = YouTrackWriter(client)

    def identity(self) -> str:
        return f"youtrack {self.cfg.url} project {self.cfg.project}"

    def describe(self) -> str:
        return f"YouTrack at {self.cfg.url}, project {self.cfg.project}"

    def queued(self) -> list[Issue]:
        c = self.cfg
        return self.api.queued_issues(c.project, c.tags.queue, c.state_field, c.states.queued)

    def trigger(self, issue: Issue) -> Trigger | None:
        event = self.api.tag_setter(issue.db_id, self.cfg.tags.queue)
        return Trigger(event.activity_id, event.author, event.timestamp) if event else None

    def issue(self, key: str) -> IssueDetails:
        i = self.api.issue(key)
        return IssueDetails(i.db_id, i.key, i.summary, i.description, (i.fields.get(self.cfg.state_field) or [""])[0],
                            i.writers)

    def tags(self, db_id: str) -> set[str]:
        return self.api.issue_tags(db_id)

    def add_tag(self, db_id: str, name: str) -> None:
        self.api.add_tag(db_id, name)

    def remove_tag(self, db_id: str, name: str) -> None:
        self.api.remove_tag(db_id, name)

    def set_state(self, db_id: str, state: str) -> None:
        self.api.set_state(db_id, self.cfg.state_field, state)

    def assign_to_bot(self, db_id: str) -> None:
        if self.cfg.assignee_field:
            self.api.set_assignee(db_id, self.cfg.assignee_field, self.cfg.bot_user)

    def comments(self, db_id: str) -> list[Comment]:
        return self.api.comments(db_id)

    def add_comment(self, db_id: str, text: str) -> tuple[str, int]:
        return self.api.add_comment(db_id, text)

    def attach(self, db_id: str, comment_id: str, name: str, data: bytes, mime: str) -> None:
        self.api.attach_to_comment(db_id, comment_id, name, data, mime)

    def attachment_markdown(self, name: str, caption: str) -> str:
        # YouTrack shows a comment's own attachment in place when the text names it: images and a video player.
        return f"![{caption}]({name})"

    def issue_url(self, key: str) -> str:
        return f"{self.cfg.url}/issue/{key}"

    def mention(self, user: str) -> str:
        return f"@{user}"

    def user_exists(self, login: str) -> bool:
        return self.api.user_exists(login)

    def check(self) -> list[Check]:
        return check_youtrack(self.cfg, self.api)


def youtrack_client(cfg: YouTrackConfig, token: str, http: HttpConfig, transport=None) -> ServiceClient:
    return ServiceClient("youtrack", cfg.url, {"Authorization": f"Bearer {token}", "Accept": "application/json"},
                         http, transport=transport)


def check_youtrack(c: YouTrackConfig, yt: YouTrack) -> list[Check]:
    """Every mistake in the section on its own line: the token, the bot, the project, its states and tags."""
    out: list[Check] = []
    try:
        me = yt.me()
    except AuthError as e:
        return [Check("youtrack.auth", "fail", str(e))]
    except ServiceError as e:
        return [Check("youtrack.reachable", "fail", str(e))]
    if me.get("login") != c.bot_user:
        out.append(Check("youtrack.bot_user", "fail",
                         f"token belongs to {me.get('login')!r}, config says {c.bot_user!r}"))
    else:
        out.append(Check("youtrack.bot_user", "ok", c.bot_user))

    project = yt.find_project(c.project)
    if not project:
        out.append(Check("youtrack.project", "fail", f"project {c.project!r} not found or not visible to the bot"))
        return out
    if project.get("archived"):
        out.append(Check("youtrack.project", "fail", f"project {c.project!r} is archived"))
        return out
    out.append(Check("youtrack.project", "ok", f"{c.project} ({project.get('name')})"))

    fields = {f["field"]["name"]: f for f in yt.project_fields(project["id"]) if f.get("field")}

    def bundle_values(field: dict) -> list[str]:
        return [v["name"] for v in ((field.get("bundle") or {}).get("values") or []) if not v.get("archived")]

    state = fields.get(c.state_field)
    if not state:
        out.append(Check("youtrack.state_field", "fail",
                         f"field {c.state_field!r} not in project; fields: {', '.join(sorted(fields))}"))
    else:
        values = bundle_values(state)
        for key in ("queued", "in_progress", "code_review"):
            want = getattr(c.states, key)
            if want in values:
                out.append(Check(f"youtrack.states.{key}", "ok", want))
            else:
                out.append(Check(f"youtrack.states.{key}", "fail",
                                 f"{want!r} is not a {c.state_field} value; available: {', '.join(values)}"))

    tags = yt.tag_names()
    for key in ("queue", "needs_input", "failed"):
        want = getattr(c.tags, key)
        if want in tags:
            out.append(Check(f"youtrack.tags.{key}", "ok", want))
        else:
            out.append(Check(f"youtrack.tags.{key}", "fail",
                             f"tag {want!r} does not exist or is not shared with {c.bot_user}"))
    return out


def _build(cfg: YouTrackConfig, token: str, http: HttpConfig, transport=None) -> YouTrackTracker:
    return YouTrackTracker(cfg, youtrack_client(cfg, token, http, transport))


TRACKER = TrackerType(config=YouTrackConfig, build=_build)
