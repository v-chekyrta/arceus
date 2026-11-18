import io
import json
import threading
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from assistant import server, tools_mcp
from assistant.brain import BrainConfig, Event, command
from assistant.store import Store
from assistant.youtrack import Client, YouTrackConfig, YouTrackError
from tests.test_chat import H, cfg, wait_idle


def yt_config(tmp: Path, token: str | None = "perm:secret-token", tag: str = "", board: str = "") -> YouTrackConfig:
    tf = tmp / "secrets" / "assistant.env"
    tf.parent.mkdir(parents=True, exist_ok=True)
    if token:
        tf.write_text(f"YOUTRACK_TOKEN={token}\n")
        tf.chmod(0o600)
    return YouTrackConfig(url="https://yt.example.com", project="NWND", secrets=tf, queue_tag=tag,
                          board=board, recheck_s=(0.0, 0.0))


class FakeYouTrack:
    """A YouTrack that answers the calls the assistant makes, and remembers them."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.created = 0
        self.fail_create: int | None = None
        self.lose_answer = False          # make the ticket, then the answer never arrives
        self.issues: list[dict] = []      # what was made: id, idReadable, description
        self.on_board: list[str] = []     # issue ids put on the board
        self.ticket7 = {"summary": "Кнопка", "description": "опис\n\n---\nСтворено асистентом фабрики з розмови з "
                                                             "m@x (Northwind). Мітка чернетки: fa-aaaaaaaaaaaa-1."}
        self.updates = 0
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        params = dict(request.url.params)
        with self.lock:
            self.calls.append((request.method, request.url.path, params, body))
        assert request.headers["authorization"] == "Bearer perm:secret-token"
        p = request.url.path
        made = next((i for i in self.issues if p in (f"/api/issues/{i['idReadable']}", f"/api/issues/{i['id']}")), None)
        if made and request.method == "GET" and "tags(id,name)" in params.get("fields", ""):
            return httpx.Response(200, json={"id": made["id"], "idReadable": made["idReadable"],
                                             "tags": [{"id": "6-1", "name": t} for t in made["tags"]],
                                             "customFields": [
                                                 {"name": "State", "value": {"name": made["state"]} if made["state"] else None},
                                                 {"name": "Assignee", "value": {"login": made["assignee"]}
                                                  if made.get("assignee") else None}]})
        if p.endswith("/comments") and request.method == "GET":
            i = next(i for i in self.issues if p == f"/api/issues/{i['id']}/comments")
            skip, top = int(params.get("$skip", 0)), int(params["$top"])
            return httpx.Response(200, json=i["comments"][skip:skip + top])
        if p.endswith("/activitiesPage"):
            i = next(i for i in self.issues if p == f"/api/issues/{i['id']}/activitiesPage")
            return httpx.Response(200, json={"activities": i["history"], "hasAfter": False})
        if p == "/api/issues" and request.method == "GET" and '"fa-' in params.get("query", ""):
            mark = params["query"].split('"')[1]
            return httpx.Response(200, json=[i for i in self.issues if mark in i["description"]])
        if p == "/api/issues" and request.method == "GET":
            return httpx.Response(200, json=[{"idReadable": "NWND-7", "summary": "Кнопка «Вперед» сіра",
                                              "resolved": None, "tags": [{"name": "agent"}],
                                              "customFields": [{"name": "State", "value": {"name": "In Progress"}},
                                                               {"name": "Assignee", "value": {"login": "Agent01"}}]}])
        if p == "/api/issues/NWND-7" and request.method == "GET":
            t = self.ticket7
            return httpx.Response(200, json={"id": "2-7", "idReadable": "NWND-7", "summary": t["summary"],
                                             "description": t["description"],
                                             "comments": [{"text": "Беру в роботу", "author": {"login": "Agent01"}}],
                                             "customFields": [], "tags": []})
        if p == "/api/issues/2-7" and request.method == "POST":
            with self.lock:
                self.ticket7.update(summary=body["summary"], description=body["description"])
                self.updates += 1
            return httpx.Response(200, json={"id": "2-7"})
        if p == "/api/issues/OTHER-1":
            return httpx.Response(200, json={"idReadable": "OTHER-1", "summary": "чуже"})
        if p == "/api/admin/projects":
            return httpx.Response(200, json=[{"id": "0-12", "shortName": "NWND"}, {"id": "0-1", "shortName": "X"}])
        if p == "/api/issues" and request.method == "POST":
            if self.fail_create:
                return httpx.Response(self.fail_create, json={"error_description": "no rights"})
            with self.lock:
                self.created += 1
                n = 100 + self.created
                self.issues.append({"id": f"2-{n}", "idReadable": f"NWND-{n}", "description": body["description"],
                                    "state": "Backlog", "tags": [], "history": [], "comments": []})
            if self.lose_answer:
                raise httpx.ReadTimeout("the answer was lost")
            return httpx.Response(200, json={"id": f"2-{n}", "idReadable": f"NWND-{n}"})
        if p == "/api/tags":
            return httpx.Response(200, json=[{"id": "6-1", "name": "agent"}, {"id": "6-2", "name": "agent-failed"}])
        if p.endswith("/tags") and request.method == "POST":
            return httpx.Response(200, json={"id": "6-1"})
        if p == "/api/agiles":                  # two pages of boards: the one wanted is on the second
            skip, top = int(params.get("$skip", 0)), int(params["$top"])
            boards = [{"id": f"104-{i}", "name": f"Board {i}", "currentSprint": {"id": f"105-{i}"}}
                      for i in range(top + 3)]
            boards.append({"id": "104-17", "name": "Northwind Course development",
                           "currentSprint": {"id": "105-4143"}})
            return httpx.Response(200, json=boards[skip:skip + top])
        if p == "/api/agiles/104-17":
            return httpx.Response(200, json={"id": "104-17", "name": "Northwind Course development",
                                             "currentSprint": {"id": "105-4143"}})
        if p == "/api/agiles/104-17/sprints/105-4143/issues" and request.method == "POST":
            with self.lock:
                self.on_board.append(body["id"])
            return httpx.Response(200, json={"id": body["id"]})
        return httpx.Response(404, json={})


# ----------------------------------------------------------------------------------------------------- client
def test_client_reads_and_creates(tmp_path):
    fake = FakeYouTrack()
    c = Client(yt_config(tmp_path, tag="agent"), transport=httpx.MockTransport(fake))
    found = c.search("кнопка #Unresolved")
    assert found[0]["key"] == "NWND-7" and found[0]["url"] == "https://yt.example.com/issue/NWND-7"
    assert found[0]["state"] == "In Progress" and found[0]["assignee"] == "Agent01" and found[0]["tags"] == ["agent"]
    assert fake.calls[0][2]["query"] == "project: {NWND} кнопка #Unresolved"
    assert c.issue("NWND-7")["comments"][0]["text"] == "Беру в роботу"
    with pytest.raises(YouTrackError):
        c.current("OTHER-1")                     # only this project's tickets, by the key's own shape
    with pytest.raises(YouTrackError):
        c.issue("OTHER-1")                       # only this project's tickets
    made = c.create("Назва", "Опис", "bug")
    assert made == {"id": "2-101", "key": "NWND-101", "url": "https://yt.example.com/issue/NWND-101",
                    "tagged": "agent", "board": None}
    post = next(x for x in fake.calls if x[0] == "POST" and x[1] == "/api/issues")
    assert post[3]["project"] == {"id": "0-12"} and post[3]["summary"] == "Назва"
    assert post[3]["customFields"][0]["value"] == {"name": "Bug"}
    assert ("POST", "/api/issues/2-101/tags", {"fields": "id"}, {"id": "6-1"}) in fake.calls


def test_client_says_why_in_words(tmp_path):
    fake = FakeYouTrack()
    fake.fail_create = 403
    c = Client(yt_config(tmp_path), transport=httpx.MockTransport(fake))
    with pytest.raises(YouTrackError, match="не пускає бота"):
        c.create("a", "b", "bug")

    def down(request):
        raise httpx.ConnectError("refused")
    c2 = Client(yt_config(tmp_path), transport=httpx.MockTransport(down))
    with pytest.raises(YouTrackError, match="недоступний"):
        c2.search("x")
    with pytest.raises(YouTrackError, match="не підключено"):
        Client(yt_config(tmp_path / "none", token=None))


# ----------------------------------------------------------------------------------------------------- tools server
def rpc(tools: tools_mcp.Tools, *msgs) -> list[dict]:
    out = io.StringIO()
    tools_mcp.serve(tools, io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_tools_server_speaks_mcp_and_only_drafts(tmp_path, monkeypatch):
    store = Store(tmp_path / "a.db")
    conv = store.create_conversation("m@x")
    fake = FakeYouTrack()
    real = Client.__init__
    monkeypatch.setattr(Client, "__init__", lambda self, c, transport=None:
                        real(self, c, httpx.MockTransport(fake)))
    tools = tools_mcp.Tools(yt_config(tmp_path), store, conv.id)
    res = rpc(tools,
              {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
              {"jsonrpc": "2.0", "method": "notifications/initialized"},
              {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
              {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "youtrack_search", "arguments": {"query": "кнопка"}}},
              {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "propose_ticket", "arguments": {
                  "kind": "bug", "summary": "Сіра кнопка «Вперед»", "description": "## Що болить\nКнопка сіра",
                  "open_questions": []}}},
              {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
               "params": {"name": "create_ticket", "arguments": {}}})
    assert [r["id"] for r in res] == [1, 2, 3, 4, 5]          # the notification got no answer
    names = [t["name"] for t in res[1]["result"]["tools"]]
    assert names == ["youtrack_search", "youtrack_issue", "propose_ticket", "propose_ticket_update",
                     "memory_search", "remember", "forget"]   # nothing creates or changes a ticket
    assert json.loads(res[2]["result"]["content"][0]["text"])["issues"][0]["key"] == "NWND-7"
    assert not res[3]["result"]["isError"]
    d = store._drafts("conversation_id=?", (conv.id,))
    assert len(d) == 1 and d[0].state == "draft" and d[0].message_id is None
    assert res[4]["result"]["isError"]
    assert not any(c[0] == "POST" for c in fake.calls)       # the tools never wrote to YouTrack
    assert "perm:secret-token" not in json.dumps(res)


def test_tools_without_token_offer_only_drafts(tmp_path):
    store = Store(tmp_path / "a.db")
    conv = store.create_conversation("m@x")
    tools = tools_mcp.Tools(yt_config(tmp_path, token=None), store, conv.id)
    res = rpc(tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
              {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "propose_ticket", "arguments": {
                  "kind": "nonsense", "summary": "x", "description": "y"}}})
    assert [t["name"] for t in res[0]["result"]["tools"]] == ["propose_ticket", "memory_search", "remember", "forget"]
    assert res[1]["result"]["isError"]


def test_turn_gets_its_tools_server(tmp_path):
    spec = {"command": "/py", "args": ["-m", "assistant.tools_mcp"], "env": {"ASSISTANT_CONVERSATION": "c1"}}
    c = command(BrainConfig("claude", Path("/m"), Path("/p"), Path("/s")), None, "sid", (), spec)
    assert json.loads(c[c.index("--mcp-config") + 1]) == {"mcpServers": {"assistant": spec}}
    assert "--strict-mcp-config" in c and c[c.index("--allowedTools") + 1] == "mcp__assistant"
    assert c[c.index("--tools") + 1] == "Read,Grep,Glob"


# ----------------------------------------------------------------------------------------------------- the chat
def chat(tmp_path, monkeypatch, token="perm:secret-token", tag="", board=""):
    s = cfg(tmp_path)
    s.youtrack = yt_config(tmp_path, token=token, tag=tag, board=board)
    fake = FakeYouTrack()
    real = Client.__init__
    monkeypatch.setattr(Client, "__init__", lambda self, c, transport=None:
                        real(self, c, httpx.MockTransport(fake)))
    prompts = []

    async def run(cfg_, prompt, session, new_session=None, env=None, **kw):
        prompts.append(prompt)
        app = client_holder[0].app.state.app
        cid = next(c.id for c in app.store.conversations())
        if "тікет" in prompt and "Події" not in prompt:
            app.store.add_draft(cid, "bug", "Сіра кнопка «Вперед»", "## Що болить\nКнопка сіра після уроку 3")
        yield Event("done", "Ось чернетка.")
    monkeypatch.setattr(server, "run_turn", run)
    client_holder = []
    client = TestClient(server.build(s))
    client_holder.append(client)
    return client, fake, prompts


def ask_for_draft(client):
    cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
    client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "оформи тікет"})
    conv = wait_idle(client, cid)
    return cid, conv["messages"][-1]["drafts"][0]


def test_draft_is_shown_edited_created_once_and_told(tmp_path, monkeypatch):
    client, fake, prompts = chat(tmp_path, monkeypatch, tag="agent")
    with client:
        assert client.get("/assistant/api/me", headers=H).json()["youtrack"] == {
            "ready": True, "project": "NWND", "url": "https://yt.example.com", "queue_tag": "agent",
            "queued_state": "To Do"}
        cid, d = ask_for_draft(client)
        assert d["state"] == "draft" and d["summary"] == "Сіра кнопка «Вперед»"
        r = client.patch(f"/assistant/api/drafts/{d['id']}", headers=H,
                         json={"kind": "feature", "summary": "Кнопка «Вперед» після уроку 3", "description": "Новий опис"})
        assert r.status_code == 200 and r.json()["edited_by"] == "manager@example.com"
        r = client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H)
        assert r.status_code == 201
        made = r.json()
        assert made["state"] == "created" and made["issue_key"] == "NWND-101" and made["tagged"] == "agent"
        assert made["acted_by"] == "manager@example.com"
        again = client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H)
        assert again.json()["issue_key"] == "NWND-101" and fake.created == 1
        post = next(x for x in fake.calls if x[0] == "POST" and x[1] == "/api/issues")
        assert post[3]["summary"] == "Кнопка «Вперед» після уроку 3" and post[3]["description"].startswith("Новий опис")
        assert post[3]["customFields"][0]["value"] == {"name": "Feature"}
        assert client.patch(f"/assistant/api/drafts/{d['id']}", headers=H,
                            json={"summary": "x", "description": "y"}).status_code == 409
        # the reply shows it as created after a reload, and the assistant hears about it once
        conv = client.get(f"/assistant/api/conversations/{cid}", headers=H).json()
        assert conv["messages"][-1]["drafts"][0]["state"] == "created"
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "що далі?"})
        wait_idle(client, cid)
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "і ще?"})
        wait_idle(client, cid)
    assert "створив тікет NWND-101" in prompts[1] and "https://yt.example.com/issue/NWND-101" in prompts[1]
    assert "NWND-101" not in prompts[2]


def test_two_presses_at_once_make_one_ticket(tmp_path, monkeypatch):
    client, fake, _ = chat(tmp_path, monkeypatch)
    with client:
        _, d = ask_for_draft(client)
        results = []

        def press():
            results.append(client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H).status_code)
        ts = [threading.Thread(target=press) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert fake.created == 1 and results.count(201) == 1


def test_failure_keeps_the_draft_and_allows_retry(tmp_path, monkeypatch):
    client, fake, _ = chat(tmp_path, monkeypatch)
    with client:
        _, d = ask_for_draft(client)
        fake.fail_create = 403
        r = client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H)
        assert r.status_code == 502 and r.json()["state"] == "draft" and "не пускає бота" in r.json()["error"]
        fake.fail_create = 504                          # a gateway's: maybe made, maybe not; not found, so say so
        r = client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H)
        assert r.status_code == 502 and "поки не видно" in r.json()["error"] and fake.created == 0
        fake.fail_create = None
        r = client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H)
        assert r.status_code == 201 and r.json()["error"] is None


def test_without_token_the_draft_says_so(tmp_path, monkeypatch):
    client, fake, _ = chat(tmp_path, monkeypatch, token=None)
    with client:
        assert client.get("/assistant/api/me", headers=H).json()["youtrack"]["ready"] is False
        _, d = ask_for_draft(client)
        r = client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H)
        assert r.status_code == 503 and "не підключено" in r.json()["error"] and r.json()["state"] == "draft"
    assert fake.calls == []


def test_dismiss_and_restart_mid_create(tmp_path, monkeypatch):
    client, _, prompts = chat(tmp_path, monkeypatch)
    with client:
        cid, d = ask_for_draft(client)
        r = client.post(f"/assistant/api/drafts/{d['id']}/dismiss", headers=H)
        assert r.json()["state"] == "dismissed"
        assert client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H).status_code == 409
        app = client.app.state.app
        d2 = app.store.add_draft(cid, "bug", "друга", "опис")
        app.store.claim_draft(d2.id)                    # the server dies while creating it
    s = cfg(tmp_path)
    s.youtrack = yt_config(tmp_path)
    with TestClient(server.build(s)) as client2:
        d2 = client2.app.state.app.store.draft(d2.id)
        assert d2.state == "draft" and "другого не створить" in d2.error


def test_lost_answer_does_not_make_a_second_ticket(tmp_path, monkeypatch):
    """YouTrack made the ticket but its answer was lost: the press finds it, and so does every later press."""
    client, fake, _ = chat(tmp_path, monkeypatch)
    with client:
        _, d = ask_for_draft(client)
        fake.lose_answer = True
        r = client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H)
        assert r.status_code == 201 and r.json()["issue_key"] == "NWND-101" and fake.created == 1
        assert f"Мітка чернетки: fa-" in fake.issues[0]["description"]


def test_retry_after_restart_finds_the_ticket_already_made(tmp_path, monkeypatch):
    client, fake, _ = chat(tmp_path, monkeypatch)
    with client:
        cid, d = ask_for_draft(client)
        app = client.app.state.app
        draft = app.store.draft(d["id"])
        # the ticket was made, then the server died before it could write that down
        fake.issues.append({"id": "2-55", "idReadable": "NWND-55",
                            "description": f"опис\n\nМітка чернетки: {server.draft_mark(draft)}."})
        r = client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H)
        assert r.status_code == 201 and r.json()["issue_key"] == "NWND-55" and fake.created == 0


def test_mark_of_another_draft_is_not_taken_for_this_one(tmp_path):
    fake = FakeYouTrack()
    fake.issues.append({"id": "2-9", "idReadable": "NWND-9", "description": "Мітка чернетки: fa-0123456789ab-12."})
    c = Client(yt_config(tmp_path), transport=httpx.MockTransport(fake))
    assert c.find_marked("fa-0123456789ab-1") is None
    assert c.find_marked("fa-0123456789ab-12")["key"] == "NWND-9"


# ----------------------------------------------------------------------------------------------------- board and work
@pytest.mark.parametrize("board", ["Northwind Course development", "104-17"])
def test_created_ticket_goes_on_the_board(tmp_path, board):
    """By name (found on the second page of the bot's boards) or by id."""
    fake = FakeYouTrack()
    c = Client(yt_config(tmp_path, board=board), transport=httpx.MockTransport(fake))
    made = c.create("Назва", "Опис", "bug")
    assert made["board"] == "на борді «Northwind Course development»" and fake.on_board == ["2-101"]


def test_a_board_that_fails_does_not_lose_the_ticket(tmp_path):
    fake = FakeYouTrack()
    c = Client(yt_config(tmp_path, board="Немає такої"), transport=httpx.MockTransport(fake))
    made = c.create("Назва", "Опис", "bug")
    assert made["key"] == "NWND-101" and made["board"] == "борду «Немає такої» не знайдено"


def test_found_again_after_a_lost_answer_is_still_put_on_the_board(tmp_path):
    fake = FakeYouTrack()
    fake.lose_answer = True
    c = Client(yt_config(tmp_path, board="104-17"), transport=httpx.MockTransport(fake))
    made = c.create("Назва", "Опис\n\nМітка чернетки: fa-abc-1.", "bug", marker="fa-abc-1")
    assert made["key"] == "NWND-101" and fake.on_board == ["2-101"]


def test_ticket_status_only_in_this_project(tmp_path):
    c = Client(yt_config(tmp_path), transport=httpx.MockTransport(FakeYouTrack()))
    with pytest.raises(YouTrackError):
        c.ticket("OTHER-1")


def factory_config(tmp_path, users=("lead.dev",)) -> Path:
    f = tmp_path / "runner-config.yaml"
    f.write_text("tracker:\n  bot_user: Agent01\n  allowed_users: [" + ", ".join(users) + "]\n"
                 "  tags: {queue: agent}\n  states: {queued: To Do}\n")
    return f


def launch_chat(tmp_path, monkeypatch, users=("lead.dev",)):
    client, fake, prompts = chat(tmp_path, monkeypatch, board="104-17")
    client.app.state.app.s.factory_config = factory_config(tmp_path, users)
    return client, fake, prompts


def test_the_card_tells_the_manager_what_is_left_until_the_factory_takes_it(tmp_path, monkeypatch):
    """The assistant never queues a ticket: the manager moves it to To Do and tags it. The card reads the ticket and
    says which step is missing and whose tag the factory accepts. «Taken» is only the factory's own claim comment,
    written after the tag; a state alone never says so."""
    client, fake, prompts = launch_chat(tmp_path, monkeypatch)
    with client:
        cid, d = ask_for_draft(client)
        assert client.get(f"/assistant/api/drafts/{d['id']}/ticket", headers=H).status_code == 409   # not made yet
        made = client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H).json()
        assert made["board"] == "на борді «Northwind Course development»" and made["tagged"] is None
        status = lambda: client.get(f"/assistant/api/drafts/{d['id']}/ticket", headers=H).json()
        st = status()
        assert (st["state"], st["tag_by"], st["tag_ok"], st["taken"], st["rejected"]) == ("Backlog", None, False,
                                                                                           False, False)
        assert st["queued_state"] == "To Do" and st["tag"] == "agent" and st["factory_users"] == ["lead.dev"]
        assert st["factory_bot"] == "Agent01"
        i = fake.issues[0]
        # none of these is the factory taking it: cancelled, no state, a person working on it
        for state, assignee in (("Canceled", ""), ("", ""), ("In Progress", "kyrylo.kholodenko")):
            i["state"], i["assignee"] = state, assignee
            st = status()
            assert st["taken"] is False and st["state"] == state and st["assignee"] == assignee
        # tagged by someone the factory does not take work from: the card says so
        i["state"], i["assignee"] = "To Do", ""
        i["tags"].append("agent")
        i["history"].append({"timestamp": 1000, "author": {"login": "someone"}, "added": [{"name": "agent"}],
                             "removed": []})
        st = status()
        assert st["tag_by"] == "someone" and st["tag_ok"] is False and st["taken"] is False
        # a claim comment from before the latest tag belongs to an earlier run: not this one
        i["comments"].append({"text": "The agent has taken this ticket.\n\n[arceus:claim]", "created": 1500,
                              "author": {"login": "Agent01"}})
        i["history"].append({"timestamp": 2000, "author": {"login": "lead.dev"}, "added": [{"name": "agent"}],
                             "removed": []})
        st = status()
        assert st["tag_ok"] is True and st["taken"] is False
        # the same words from someone else are not the factory's
        i["comments"].append({"text": "[arceus:claim]", "created": 2500, "author": {"login": "lead.dev"}})
        assert status()["taken"] is False
        # the factory claims it
        i["state"], i["assignee"] = "In Progress", "Agent01"
        i["comments"].append({"text": "The agent has taken this ticket.\n\n[arceus:claim]", "created": 3000,
                              "author": {"login": "Agent01"}})
        st = status()
        assert st["taken"] is True and (st["state"], st["assignee"]) == ("In Progress", "Agent01")
        # the assistant itself never changed the ticket after making it
        assert [c for c in fake.calls if c[0] in ("POST", "DELETE") and c[1].startswith("/api/issues/2-")] == []
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "що далі?"})
        wait_idle(client, cid)
    assert "перевести його в To Do й поставити тег agent. Нагадай про це." in prompts[1]


def test_the_factory_refusing_the_tag_is_shown(tmp_path, monkeypatch):
    client, fake, _ = launch_chat(tmp_path, monkeypatch, users=("lead.dev", "maintainer"))
    with client:
        _, d = ask_for_draft(client)
        client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H)
        i = fake.issues[0]
        i["state"], i["tags"] = "To Do", ["agent"]
        i["history"].append({"timestamp": 10, "author": {"login": "someone"}, "added": [{"name": "agent"}], "removed": []})
        i["comments"].append({"text": "someone may not start the agent.\n\n[arceus:rejected:a-1]", "created": 20,
                              "author": {"login": "Agent01"}})
        st = client.get(f"/assistant/api/drafts/{d['id']}/ticket", headers=H).json()
        assert st["rejected"] is True and st["taken"] is False


def test_a_bots_tag_does_not_count(tmp_path, monkeypatch):
    client, fake, _ = launch_chat(tmp_path, monkeypatch, users=("lead.dev", "Agent01"))
    with client:
        _, d = ask_for_draft(client)
        client.post(f"/assistant/api/drafts/{d['id']}/create", headers=H)
        i = fake.issues[0]
        i["state"], i["tags"] = "To Do", ["agent"]
        i["history"].append({"timestamp": 1, "author": {"login": "Agent01"}, "added": [{"name": "agent"}], "removed": []})
        assert client.get(f"/assistant/api/drafts/{d['id']}/ticket", headers=H).json()["tag_ok"] is False


def test_a_draft_with_open_questions_is_refused(tmp_path):
    store = Store(tmp_path / "a.db")
    conv = store.create_conversation("m@x")
    tools = tools_mcp.Tools(yt_config(tmp_path), store, conv.id)
    args = {"kind": "feature", "summary": "Favicon без білого фону", "description": "## Що болить\nБілий фон"}
    res = rpc(tools,
              {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "propose_ticket", "arguments": {
                  **args, "open_questions": ["Чи є логотип у SVG?", " "]}}},
              {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "propose_ticket",
                                                                             "arguments": args}},
              {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "propose_ticket", "arguments": {
                  **args, "open_questions": []}}})
    assert res[0]["result"]["isError"] and "Чи є логотип у SVG?" in res[0]["result"]["content"][0]["text"]
    assert res[1]["result"]["isError"]                        # saying nothing about open questions is not «none»
    assert not res[2]["result"]["isError"]
    assert len(store._drafts("conversation_id=?", (conv.id,))) == 1


def test_the_prompt_asks_for_requirements_before_a_ticket():
    prompt = (Path(__file__).parent.parent / "assistant" / "prompt.md").read_text()
    for must in ("з'ясуй усе, що знає лише людина", "Матеріали, без яких не обійтися", "open_questions",
                 "(перевести в To Do і поставити тег agent), підкаже картка", "Тікет описує, що ми очікуємо, а не як це зробити"):
        assert must in prompt


def test_factory_policy_follows_the_runner(tmp_path):
    from assistant.factory import FactoryPolicy
    p = FactoryPolicy.load(factory_config(tmp_path, ("lead.dev", "Agent01")))
    assert p.accepts("lead.dev") and not p.accepts("Agent01") and not p.accepts("someone") and not p.accepts("")
    assert FactoryPolicy.load(None, ["a"], "Open").queued_state == "Open"


def test_old_database_gets_the_new_columns(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript('''CREATE TABLE drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
        message_id INTEGER, kind TEXT NOT NULL, summary TEXT NOT NULL, description TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'draft', error TEXT, issue_key TEXT, issue_url TEXT, tagged TEXT, acted_by TEXT,
        acted_at REAL, edited_by TEXT, announced INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
        INSERT INTO drafts (conversation_id, kind, summary, description, state, issue_key, created_at)
        VALUES ('c', 'bug', 's', 'd', 'created', 'NWND-1', 0);''')
    con.commit(); con.close()
    store = Store(db)
    d = store.draft(1)
    assert d.board is None and d.issue_key == "NWND-1"


def test_columns_of_the_old_into_work_flow_are_dropped(tmp_path):
    import sqlite3
    db = tmp_path / "mid.db"
    Store(db)
    con = sqlite3.connect(db)
    for c in ("work", "work_by", "queued_as"):
        con.execute(f"ALTER TABLE drafts ADD COLUMN {c} TEXT")
    con.commit(); con.close()
    Store(db)
    con = sqlite3.connect(db)
    assert not {"work", "work_by", "queued_as"} & {r[1] for r in con.execute("PRAGMA table_info(drafts)")}


# ----------------------------------------------------------------------------------------------------- edits
def test_strip_footer_and_marks():
    from assistant.youtrack import marks, strip_footer
    text = "текст\n\n---\nОновлено асистентом фабрики з розмови з a (Northwind). Мітка правки: fa-x-2. Мітка чернетки: fa-x-1."
    assert strip_footer(text) == "текст" and marks(text) == ["fa-x-2", "fa-x-1"]
    assert strip_footer("без підвалу") == "без підвалу" and marks("ні") == []


ADDED = ("## Що болить\nСіра кнопка.\n\n---\nСтворено асистентом фабрики з розмови з m@x (Northwind). Мітка чернетки: "
         "fa-aaaaaaaaaaaa-1.\n\n## Доповнення Дани\nКнопка має бути зеленою.")


def test_a_persons_addition_after_the_footer_is_kept():
    """A person may write under the assistant's footer. Only the footer's own lines are the assistant's."""
    from assistant.youtrack import marks, strip_footer
    assert strip_footer(ADDED) == "## Що болить\nСіра кнопка.\n\n## Доповнення Дани\nКнопка має бути зеленою."
    assert marks(ADDED) == ["fa-aaaaaaaaaaaa-1"]


def test_the_page_shows_the_addition_in_the_comparison_too():
    """The card strips the footer with its own expression: run it on the same text."""
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    js = (Path(__file__).parent.parent / "assistant" / "static" / "app.js").read_text()
    expr = re.search(r'const before = \(d\.target_before \|\| ""\)(.*?);\n', js, re.S).group(1)
    out = subprocess.run([node, "-e", f"process.stdout.write(({json.dumps(ADDED)}){expr})"],
                         capture_output=True, text=True, check=True).stdout
    assert "Кнопка має бути зеленою." in out and "Створено асистентом" not in out and "Сіра кнопка." in out


def propose_edit(tmp_path, monkeypatch, fake, **kw):
    store = Store(tmp_path / "data" / "assistant.db")
    real = Client.__init__
    monkeypatch.setattr(Client, "__init__", lambda self, c, transport=None: real(self, c, httpx.MockTransport(fake)))
    conv = store.create_conversation("manager@example.com")
    args = {"key": "nwnd-7", "summary": "Кнопка «Вперед» після уроку 3", "description": "## Що болить\nНовий текст",
            "what_changed": "Менеджер дописав, що кнопка має бути зеленою.", "open_questions": [], **kw}
    res = rpc(tools_mcp.Tools(yt_config(tmp_path), store, conv.id),
              {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "propose_ticket_update", "arguments": args}})
    return store, conv, res[0]["result"]


def test_an_edit_is_proposed_from_the_tickets_current_text(tmp_path, monkeypatch):
    fake = FakeYouTrack()
    store, conv, r = propose_edit(tmp_path, monkeypatch, fake)
    assert not r["isError"] and json.loads(r["content"][0]["text"])["update"] == "NWND-7"
    d = store._drafts("conversation_id=?", (conv.id,))[0]
    assert d.target_key == "NWND-7" and d.target_summary == "Кнопка" and d.target_before == fake.ticket7["description"]
    assert fake.updates == 0                       # proposing never changes the ticket
    # the same text as now is not an edit; a ticket of another project is not ours; open questions wait
    _, _, same = propose_edit(tmp_path, monkeypatch, fake, summary="Кнопка", description="опис")
    assert same["isError"] and "уже саме такий" in same["content"][0]["text"]
    _, _, other = propose_edit(tmp_path, monkeypatch, fake, key="OTHER-1")
    assert other["isError"]
    _, _, asks = propose_edit(tmp_path, monkeypatch, fake, open_questions=["Який відтінок зеленого?"])
    assert asks["isError"] and "Який відтінок зеленого?" in asks["content"][0]["text"]


def edit_chat(tmp_path, monkeypatch):
    client, fake, prompts = chat(tmp_path, monkeypatch)
    app = client.app.state.app
    return client, fake, app


def make_edit(app, fake):
    cid = app.store.create_conversation("manager@example.com").id
    cur = {"key": "NWND-7", "summary": fake.ticket7["summary"], "description": fake.ticket7["description"]}
    d = app.store.add_draft(cid, "feature", "Кнопка «Вперед» зелена", "## Що болить\nНовий текст", target=cur)
    app._with_drafts(app.store.add_message(cid, "assistant", "Ось правка.", meta={}))
    return cid, d


def test_applying_an_edit_keeps_the_marks_and_is_done_once(tmp_path, monkeypatch):
    client, fake, app = edit_chat(tmp_path, monkeypatch)
    with client:
        cid, d = make_edit(app, fake)
        got = client.get(f"/assistant/api/drafts/{d.id}", headers=H).json()
        assert got["target_key"] == "NWND-7" and got["target_before"].startswith("опис")
        r = client.post(f"/assistant/api/drafts/{d.id}/create", headers=H)
        assert r.status_code == 201 and r.json()["state"] == "created" and r.json()["issue_key"] == "NWND-7"
        t = fake.ticket7
        assert t["summary"] == "Кнопка «Вперед» зелена" and t["description"].startswith("## Що болить\nНовий текст")
        assert "Оновлено асистентом фабрики з розмови з manager@example.com" in t["description"]
        assert f"Мітка правки: fa-{cid[:12]}-{d.id}." in t["description"]
        assert "Мітка чернетки: fa-aaaaaaaaaaaa-1." in t["description"]       # the original mark is kept
        assert fake.created == 0 and fake.updates == 1
        # the answer was lost and it is pressed again: the ticket already holds it, nothing is written twice
        app.store._db.execute("UPDATE drafts SET state='draft' WHERE id=?", (d.id,))
        assert client.post(f"/assistant/api/drafts/{d.id}/create", headers=H).status_code == 201
        assert fake.updates == 1
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "далі"})
        wait_idle(client, cid)


def test_an_edit_of_a_ticket_with_an_addition_keeps_it_and_its_marks(tmp_path, monkeypatch):
    client, fake, app = edit_chat(tmp_path, monkeypatch)
    with client:
        fake.ticket7["description"] = ADDED
        cid, d = make_edit(app, fake)
        from assistant.youtrack import strip_footer
        # what the writer starts from (Night Shift's brief, the card's «now») still has the addition
        assert "Кнопка має бути зеленою." in strip_footer(app.store.draft(d.id).target_before)
        app.store.edit_draft(d.id, "feature", "Кнопка зелена", "## Що болить\nСіра кнопка.\n\n## Доповнення Дани\n"
                             "Кнопка має бути зеленою.\n\n## Рішення\n- Зелена.", "manager@example.com")
        assert client.post(f"/assistant/api/drafts/{d.id}/create", headers=H).status_code == 201
        t = fake.ticket7["description"]
        assert "Кнопка має бути зеленою." in t and "- Зелена." in t
        assert t.count("Мітка чернетки: fa-aaaaaaaaaaaa-1.") == 1 and f"Мітка правки: fa-{cid[:12]}-{d.id}." in t


def test_an_edit_never_overwrites_a_change_made_since(tmp_path, monkeypatch):
    client, fake, app = edit_chat(tmp_path, monkeypatch)
    with client:
        _, d = make_edit(app, fake)
        fake.ticket7["description"] = "Хтось уже дописав сюди своє."
        r = client.post(f"/assistant/api/drafts/{d.id}/create", headers=H)
        assert r.status_code == 502 and "змінили після того" in r.json()["error"] and r.json()["state"] == "draft"
        assert fake.updates == 0 and fake.ticket7["description"] == "Хтось уже дописав сюди своє."
