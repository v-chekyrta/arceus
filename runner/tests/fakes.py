"""In-memory YouTrack and GitLab that speak just enough of the REST API."""

from __future__ import annotations

import json

import httpx

from .conftest import json_response


class FakeYouTrack:
    def __init__(self, login: str = "agent-runner"):
        self.login = login
        self.issues: list[dict] = []
        self.activities: dict[str, list[dict]] = {}
        self.fail: dict[str, int] = {}  # path -> status to return
        self.requests: list[httpx.Request] = []
        self.users = {"j.doe", "a.smith", "mallory"}
        self.tags = {"agent", "agent-needs-input", "agent-failed"}
        self.states = ["To Do", "In Progress", "Code Review", "Done"]
        self.issue_tags: dict[str, set[str]] = {}
        self.issue_fields: dict[str, dict[str, object]] = {}
        self.comments: dict[str, list[dict]] = {}
        self.clock = 1_792_479_600_000  # 2026-10-20 07:00 UTC in ms, the test runner's 'now'
        self.fail_once: dict[tuple[str, str], int] = {}  # (method, path) -> status, once
        self.attachments: dict[str, list[tuple[str, bytes]]] = {}  # comment id -> (file name, content)

    def comment(self, db_id: str, author: str, text: str) -> None:
        self.clock += 1
        self.comments.setdefault(db_id, []).append({"id": f"c-{self.clock}", "text": text, "created": self.clock,
                                                    "author": {"login": author}})

    def _write(self, request: httpx.Request) -> httpx.Response | None:
        path, method = request.url.path, request.method
        parts = path.split("/")
        if len(parts) >= 4 and parts[1:3] == ["api", "issues"]:
            db_id = self._db_id(parts[3])
            if db_id is None:
                return json_response({"error": "no issue"}, 404)
            rest = parts[4:]
            if method == "GET" and not rest:
                issue = next(i for i in self.issues if i["id"] == db_id)
                fields = self.issue_fields.setdefault(db_id, {"State": {"name": "To Do"}})
                return json_response({**issue, "description": issue.get("description", "Task description"),
                                      "customFields": [{"name": k, "value": v} for k, v in fields.items()]})
            if method == "POST" and not rest:
                for f in json.loads(request.content)["customFields"]:
                    self.issue_fields.setdefault(db_id, {})[f["name"]] = f["value"]
                return json_response({"id": db_id})
            if rest == ["comments"] and method == "GET":
                return json_response(self._page(self.comments.get(db_id, []), request))
            if rest == ["comments"] and method == "POST":
                self.comment(db_id, self.login, json.loads(request.content)["text"])
                c = self.comments[db_id][-1]
                return json_response({"id": c["id"], "created": c["created"]})
            if len(rest) == 3 and rest[0] == "comments" and rest[2] == "attachments" and method == "POST":
                name, data = _multipart_file(request)
                self.attachments.setdefault(rest[1], []).append((name, data))
                return json_response([{"id": f"a-{len(self.attachments[rest[1]])}", "name": name}])
            if rest == ["tags"] and method == "GET":
                return json_response(self._page([{"id": f"t-{n}", "name": n}
                                                 for n in sorted(self.issue_tags.get(db_id, set()))], request))
            if rest == ["tags"] and method == "POST":
                tag_id = json.loads(request.content)["id"]
                self.issue_tags.setdefault(db_id, set()).add(tag_id.removeprefix("t-"))
                return json_response({"id": tag_id})
            if len(rest) == 2 and rest[0] == "tags" and method == "DELETE":
                self.issue_tags.setdefault(db_id, set()).discard(rest[1].removeprefix("t-"))
                return json_response({})
        return None

    def _db_id(self, ref: str) -> str | None:
        for i in self.issues:
            if ref in (i["id"], i["idReadable"]):
                return i["id"]
        return None

    def add_issue(self, n: int, *tag_events: tuple[str, str, str]) -> str:
        """tag_events: (activity_id, author, '+agent' or '-agent')."""
        db_id = f"2-{n}"
        self.issue_tags[db_id] = {"agent"} if not tag_events or tag_events[-1][2] == "+agent" else set()
        self.issues.append({"id": db_id, "idReadable": f"ACME-{n}", "summary": f"Issue {n}", "created": n,
                            "updated": n})
        self.activities[db_id] = [
            {"id": aid, "timestamp": i, "author": {"login": who},
             "added": [{"name": tag[1:]}] if tag[0] == "+" else [],
             "removed": [{"name": tag[1:]}] if tag[0] == "-" else []}
            for i, (aid, who, tag) in enumerate(tag_events)
        ]
        return db_id

    @staticmethod
    def _page(items: list, request: httpx.Request) -> list:
        top = int(request.url.params.get("$top", 42))
        skip = int(request.url.params.get("$skip", 0))
        return items[skip:skip + top]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        once = self.fail_once.pop((request.method, path), None)
        if once:
            return json_response({"error": "temporarily"}, once)
        if path in self.fail:
            return json_response({"error": "nope", "echo": request.headers.get("authorization")}, self.fail[path])
        if path == "/api/users/me":
            return json_response({"id": "1-1", "login": self.login})
        if path == "/api/admin/projects":
            return json_response(self._page([{"id": "0-1", "shortName": "ACME", "name": "Acme"}], request))
        if path == "/api/admin/projects/0-1/customFields":
            fields = [
                {"field": {"name": "State"}, "bundle": {"values": [{"name": s} for s in self.states]}},
            ]
            return json_response(self._page(fields, request))
        if path == "/api/tags":
            q = request.url.params.get("query")
            return json_response(self._page([{"id": f"t-{t}", "name": t} for t in sorted(self.tags)
                                             if not q or t == q], request))
        if path == "/api/users":
            q = request.url.params.get("query")
            return json_response(self._page([{"login": u} for u in sorted(self.users) if u == q], request))
        if path == "/api/issues":
            queued = [i for i in self.issues if "agent" in self.issue_tags.get(i["id"], set())
                      and (self.issue_fields.get(i["id"], {}).get("State") or {"name": "To Do"})["name"] == "To Do"]
            return json_response(self._page(queued, request))
        written = None if path.endswith("/activitiesPage") else self._write(request)
        if written is not None:
            return written
        if path.startswith("/api/issues/") and path.endswith("/activitiesPage"):
            db_id = path.split("/")[3]
            acts = self.activities.get(db_id)
            if acts is None:
                return json_response({"error": "not found"}, 404)
            top = int(request.url.params.get("$top", 42))
            start = int(request.url.params.get("cursor", 0))
            chunk = acts[start:start + top]
            more = start + top < len(acts)
            return json_response({"activities": chunk, "hasAfter": more, "afterCursor": str(start + top) if more else None})
        return json_response({"error": f"unexpected {path}"}, 400)


class FakeGitLab:
    def __init__(self, username: str = "acme-agent-bot"):
        self.username = username
        self.status: int | None = None
        self.retry_after: str | None = None
        self.requests: list[httpx.Request] = []
        self.repos = ["acme-platform", "acme-theme", "acme-infra"]
        self.token = {"scopes": ["api"], "expires_at": "2027-06-01", "active": True}
        self.urls: dict[str, str] = {}
        self.descriptions: dict[str, str] = {}
        self.mrs: list[dict] = []
        self.discussions: dict[int, list[dict]] = {}   # MR iid -> discussions
        self.jobs: dict[int, list[dict]] = {}          # pipeline id -> jobs
        self.traces: dict[int, str] = {}               # job id -> log
        self.uploads: list[tuple[int, str, bytes]] = []  # (project id, file name, content)
        self.refuse_uploads = False
        self._note = 1000

    def mr(self, iid: int) -> dict:
        return next(m for m in self.mrs if m["iid"] == iid)

    def note(self, iid: int, author: str, body: str, discussion: str | None = None, path=None, line=None,
             system: bool = False, commit: str | None = None) -> str:
        """A note on MR iid, in a new discussion or as a reply in an existing one."""
        self._note += 1
        n = {"id": self._note, "body": body, "system": system, "author": {"username": author},
             "created_at": "2026-10-20T07:00:00Z",
             "position": {"new_path": path, "new_line": line, "head_sha": commit} if path else None}
        threads = self.discussions.setdefault(iid, [])
        if discussion:
            next(d for d in threads if d["id"] == discussion)["notes"].append(n)
            return discussion
        threads.append({"id": f"d{self._note}", "notes": [n]})
        return f"d{self._note}"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status:
            return json_response({}, self.status, {"Retry-After": self.retry_after} if self.retry_after else None)
        path = request.url.path
        if path == "/api/v4/user":
            return json_response({"id": 7, "username": self.username})
        if path == "/api/v4/personal_access_tokens/self":
            return json_response(self.token)
        if path == "/api/v4/groups/clients%2Facme" or request.url.raw_path.startswith(b"/api/v4/groups/clients%2Facme"):
            return json_response({"id": 42, "full_path": "clients/acme"})
        if path == "/api/v4/groups/42/projects":
            page = int(request.url.params.get("page", 1))
            items = [{"id": i, "path": r, "path_with_namespace": f"clients/acme/{r}", "default_branch": "main",
                      "archived": False, "http_url_to_repo": self.urls.get(r, f"https://gl/clients/acme/{r}.git"),
                      "description": self.descriptions.get(r)}
                     for i, r in enumerate(self.repos)]
            headers = {"x-next-page": ""}
            return json_response(items if page == 1 else [], headers=headers)
        parts = path.split("/")
        if "merge_requests" in parts and parts[-1] != "merge_requests":
            iid = int(parts[parts.index("merge_requests") + 1])
            mr = self.mr(iid)
            tail = parts[parts.index("merge_requests") + 2:]
            if not tail and request.method == "GET":
                return json_response({**mr, "detailed_merge_status": mr.get("detailed_merge_status", "mergeable"),
                                      "sha": mr.get("sha", ""), "head_pipeline": mr.get("head_pipeline")})
            if not tail and request.method == "PUT":
                body = json.loads(request.content)
                if body.pop("state_event", None) == "close":
                    mr.update(state="closed", closed_by={"username": self.username})
                mr.update(body)
                return json_response(mr)
            if tail == ["notes"] and request.method == "POST":
                self.note(iid, self.username, json.loads(request.content)["body"])
                return json_response({"id": self._note}, 201)
            if tail == ["discussions"]:
                page = int(request.url.params.get("page", 1))
                return json_response(self.discussions.get(iid, []) if page == 1 else [], headers={"x-next-page": ""})
            if len(tail) == 3 and tail[0] == "discussions" and tail[2] == "notes":
                body = json.loads(request.content)["body"]
                self.note(iid, self.username, body, discussion=tail[1])
                return json_response({"id": self._note}, 201)
        if "pipelines" in parts and parts[-1] == "jobs":
            page = int(request.url.params.get("page", 1))
            return json_response(self.jobs.get(int(parts[-2]), []) if page == 1 else [], headers={"x-next-page": ""})
        if "jobs" in parts and parts[-1] == "trace":
            return httpx.Response(200, text=self.traces.get(int(parts[-2]), ""))
        if path.endswith("/uploads") and request.method == "POST":
            if self.refuse_uploads:
                return json_response({"message": "413 Request Entity Too Large"}, 413)
            name, data = _multipart_file(request)
            self.uploads.append((int(parts[4]), name, data))
            return json_response({"url": f"/uploads/{len(self.uploads):032x}/{name}",
                                  "markdown": f"![{name}](/uploads/{len(self.uploads):032x}/{name})"}, 201)
        if path.endswith("/merge_requests"):
            pid = int(path.split("/")[4])
            if request.method == "GET":
                sb = request.url.params.get("source_branch")
                want = request.url.params.get("state", "all")
                found = [m for m in self.mrs if m["project_id"] == pid and m["source_branch"] == sb
                         and want in ("all", m["state"])]
                return json_response(sorted(found, key=lambda m: -m["iid"]))
            body = json.loads(request.content)
            mr = {**body, "project_id": pid, "iid": len(self.mrs) + 1, "state": "opened",
                  "web_url": f"https://gl/clients/acme/mr/{len(self.mrs) + 1}"}
            self.mrs.append(mr)
            return json_response(mr, 201)
        return json_response({"message": f"unexpected {path}"}, 400)


def _multipart_file(request: httpx.Request) -> tuple[str, bytes]:
    """The one file of a multipart/form-data upload: (file name, content)."""
    import email.parser
    import email.policy
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + request.read())
    part = next(p for p in msg.iter_parts() if p.get_filename())
    return part.get_filename(), part.get_payload(decode=True)


class FakeScreen:
    """The security classifier: `allow`, unless a rule matches the payload. Records every message it read."""

    def __init__(self):
        self.calls: list[dict] = []
        self.rules: list[tuple[str, object]] = []  # (substring of the payload, Verdict or an exception to raise)

    def __call__(self, message: dict):
        from arceus_runner.screening import Verdict
        self.calls.append(message)
        for needle, outcome in self.rules:
            if needle in message["payload"]:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return Verdict("allow", (), "ordinary work", "fake-model")

    def payloads(self) -> list[str]:
        return [m["payload"] for m in self.calls]
