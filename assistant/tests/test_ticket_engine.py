"""Tickets written by the assistant's Night Shift engine, with the bridge (`fa-ticket-engine`) faked."""

import json
import subprocess
import time
from pathlib import Path

from starlette.testclient import TestClient

from assistant import server, tools_mcp
from assistant.brain import Event
from assistant.store import Store
from assistant.store import Attachment
from assistant.ticket_engine import (EngineError, Said, TicketEngine, TicketEngineConfig, review_notes, split_ticket,
                                     task_text)
from tests.test_chat import H, cfg
from tests.test_tickets import FakeYouTrack, rpc, yt_config

TICKET = """Банер вебінару угорі дашборда учасника

## Що болить
Клієнтка хоче анонсувати вебінар.

## Рішення
- Текст: «Вебінар».

## Не вирішено (агент питає або робить найпростіше)
- Часовий пояс: Київ."""

REVIEW_FAIL = "STATE: COMPLETE\nVERDICT: FAIL\n\n1. «Зараз банера немає» — прод не перевірено."


class FakeBridge:
    """The engine as `fa-ticket-engine` shows it: status, sync, dispatch, show, artifact."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.signed_in = True
        self.busy = 0                       # how many dispatches answer busy first
        self.tasks: dict[str, dict] = {}    # task id -> task.json
        self.artifacts: dict[str, str] = {}
        self.texts: dict[str, str] = {}
        self.briefs: dict[str, str] = {}
        self.files: dict[str, dict[str, bytes]] = {}

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        args = cmd[1:]
        self.calls.append(args)
        out = lambda rc, so="", se="": subprocess.CompletedProcess(cmd, rc, so, se)  # noqa: E731
        if args[0] == "status":
            st = "ok" if self.signed_in else "fail"
            return out(1, json.dumps({"ok": self.signed_in, "checks": [{"name": "auth.claude", "status": st},
                                                                        {"name": "auth.codex", "status": "ok"}]}))
        if args[0] == "sync":
            return out(0)
        if args[0] == "dispatch":
            tid, path, pack = args[1], args[2], args[3]
            self.texts[tid] = open(path).read()
            self.briefs[tid] = open(f"{pack}/brief.md").read()
            self.files[tid] = {f.name: f.read_bytes() for f in Path(pack, "files").iterdir()}
            if self.busy:
                self.busy -= 1
                return out(6, "TIER=busy\n")
            if tid in self.tasks:
                return out(0, "TIER=duplicate\n{}")
            self.tasks[tid] = {"task_id": tid, "state": "running", "attempt_live": True}
            return out(0, 'TIER=dispatched\n{"task_id": "%s"}' % tid)
        if args[0] == "show":
            t = self.tasks.get(args[1])
            return out(0, json.dumps(t)) if t else out(1, "", "no task")
        if args[0] == "artifact":
            a = self.artifacts.get(args[1])
            return out(0, a) if a else out(3)
        return out(2)

    def finish(self, tid, review="passed", why="", state=None, write=True):
        if write:
            self.artifacts[tid] = TICKET
        self.tasks[tid] = {"task_id": tid, "state": state or ("done" if review == "passed" else "failed"),
                           "state_reason": None if review == "passed" else "needs-user",
                           "receipt": {"review": review, "review_why": why}}


def engine_cfg():
    return TicketEngineConfig(bridge=["fa-ticket-engine"], timeout_s=1500, poll_s=0.05)


# ----------------------------------------------------------------------------------------------- the engine client
def test_parsing_the_ticket_and_the_review():
    assert split_ticket("# Назва\n\n## Що болить\nтекст") == ("Назва", "## Що болить\nтекст")
    assert review_notes(REVIEW_FAIL) == "1. «Зараз банера немає» — прод не перевірено."


def att(tmp, name, kind, **info):
    return Attachment(id=name, conversation_id="c", message_id=1, name=name, mime="x", size=10, kind=kind,
                      path=str(info.pop("path", tmp / name)), status=info.pop("status", "ready"), info=info,
                      created_by="m", created_at=0)


def test_the_task_fits_the_reviews_limit_and_keeps_the_managers_words():
    """The engine's report review reads the first 6000 bytes of the task. In the first live run her long questions
    pushed the manager's answers past it, and Codex said the manager had never said what he had."""
    long_question = "Мені треба знати: " + "картинка, текст, посилання, дати. " * 120
    convo = [Said("manager@x", "Клієнтка просить банер.", True), Said("Олександра", long_question, False),
             Said("manager@x", "Картинки немає. Показувати до 20 листопада, потім зникає сам. Погоджувати не треба.", True)]
    task, brief = task_text("fa-abc-1", "feature", "Банер", "## Що болить\nx", convo)
    assert len(task.encode()) <= 6000
    assert "Клієнтка просить банер." in task and "потім зникає сам. Погоджувати не треба." in task
    assert "картинка, текст" not in task and "briefs/fa-abc-1/brief.md" in task and "artifacts/fa-abc-1.md" in task
    assert long_question.strip() in brief.text and "Назва: Банер" in brief.text and "## Що болить\nx" in brief.text
    many = [Said("manager@x", f"слово {i} " + "дуже " * 200, True) for i in range(10)]
    task, brief = task_text("fa-abc-1", "bug", "x", "y", many)
    assert len(task.encode()) <= 6000 and "слово 9" in task and "слово 0" not in task and "у brief.md" in task
    assert "слово 0" in brief.text


def test_voice_documents_and_pictures_reach_the_ticket(tmp_path):
    """What the chat had from the files reaches Night Shift: a voice note's words are the manager's words (in the task
    the review checks «Рішення» against), a document's text and the pictures go into the brief."""
    doc = tmp_path / "brief.txt"; doc.write_text("Колір банера: помаранчевий")
    big = tmp_path / "big.txt"; big.write_text("x" * 50000)
    pic = tmp_path / "shot.png"; pic.write_bytes(b"png")
    convo = [Said("manager@x", "", True, [att(tmp_path, "voice.ogg", "audio", transcript="Показувати до двадцятого листопада")]),
             Said("manager@x", "ось матеріали", True, [att(tmp_path, "brief.docx", "document", text_path=str(doc)),
                                                         att(tmp_path, "big.xlsx", "sheet", text_path=str(big)),
                                                         att(tmp_path, "shot.png", "image", view_path=str(pic)),
                                                         att(tmp_path, "bad.zip", "archive", status="failed",
                                                             note="Архів пошкоджено.")])]
    task, brief = task_text("fa-abc-2", "feature", "Банер", "x", convo)
    assert "(голосом) Показувати до двадцятого листопада" in task
    assert "Показувати до двадцятого листопада" in brief.text and "Колір банера: помаранчевий" in brief.text
    assert "Архів пошкоджено." in brief.text
    names = dict(brief.files)
    assert sorted(Path(p).name for p in names.values()) == ["big.txt", "shot.png"]
    assert all(f"briefs/fa-abc-2/files/{n}" in brief.text for n in names)


def test_an_engine_without_a_sign_in_is_refused_before_any_task():
    fake = FakeBridge()
    fake.signed_in = False
    e = TicketEngine(engine_cfg(), run=fake)
    try:
        e.start("fa-abc-1", "x", task_text("fa-abc-1", "bug", "x", "y", [])[1])
        raise AssertionError("must refuse")
    except EngineError as err:
        assert str(err) == "перевірку ще не підключено"
    assert [c[0] for c in fake.calls] == ["status"]


# ----------------------------------------------------------------------------------------------- through the chat
def chat(tmp_path, monkeypatch, bridge, engine=True):
    s = cfg(tmp_path)
    s.name = "Олександра"
    s.youtrack = yt_config(tmp_path)
    if engine:
        s.ticket_engine = engine_cfg()
    fake_yt = FakeYouTrack()
    real = server.Client.__init__
    import httpx
    monkeypatch.setattr(server.Client, "__init__", lambda self, c, transport=None:
                        real(self, c, httpx.MockTransport(fake_yt)))
    monkeypatch.setattr(server, "TicketEngine", lambda c: TicketEngine(c, run=bridge))
    client = TestClient(server.build(s))
    return client, s


def proposed(client, s, preparing=True):
    """A draft the way the tools process makes it, under a stored reply, with a conversation before it."""
    app = client.app.state.app
    other = app.store.create_conversation("someone@example.com").id
    app.store.add_user_message(other, "Чужа розмова", "someone@example.com", [])
    cid = app.store.create_conversation("manager@example.com").id
    app.store.add_user_message(cid, "Клієнтка просить банер.", "manager@example.com", [])
    d = app.store.add_draft(cid, "feature", "Банер (бриф)", "## Що болить\nбриф", preparing=preparing)
    app._with_drafts(app.store.add_message(cid, "assistant", "Готую тікет, хвилини дві.", meta={}))
    return app, cid, app.store.draft(d.id)


def test_a_ticket_codex_accepted_replaces_her_brief(tmp_path, monkeypatch):
    bridge = FakeBridge()
    client, s = chat(tmp_path, monkeypatch, bridge)
    with client:
        app, cid, d = proposed(client, s)
        app._ticket_task.cancel()                       # steps are driven by hand here
        r = client.get(f"/assistant/api/drafts/{d.id}", headers=H).json()
        assert r["state"] == "preparing" and r["checked"] is None
        # one cannot create, edit or press anything on a ticket still being written, except put it aside
        assert client.post(f"/assistant/api/drafts/{d.id}/create", headers=H).status_code == 409
        assert client.patch(f"/assistant/api/drafts/{d.id}", headers=H,
                            json={"kind": "bug", "summary": "x", "description": "y"}).status_code == 409
        app.ticket_step()
        tid = f"fa-{cid[:12]}-{d.id}"
        assert [c[0] for c in bridge.calls] == ["status", "sync", "dispatch"] and bridge.calls[2][1] == tid
        assert "Клієнтка просить банер." in bridge.texts[tid] and "Готую тікет" not in bridge.texts[tid]
        assert "Готую тікет" in bridge.briefs[tid] and "Банер (бриф)" in bridge.briefs[tid]
        assert "Чужа розмова" not in bridge.briefs[tid]
        assert app.store.draft(d.id).prep_task == tid
        app.ticket_step()                                # still running: nothing changes, nothing new dispatched
        assert app.store.draft(d.id).state == "preparing" and [c[0] for c in bridge.calls].count("dispatch") == 1
        bridge.finish(tid, review="passed")
        app.ticket_step()
        got = client.get(f"/assistant/api/drafts/{d.id}", headers=H).json()
        assert got["state"] == "draft" and got["checked"] == "passed"
        assert got["summary"] == "Банер вебінару угорі дашборда учасника" and "Не вирішено" in got["description"]
        # changing only the type keeps the check; changing the text makes it stale
        same = {"kind": "bug", "summary": got["summary"], "description": got["description"]}
        assert client.patch(f"/assistant/api/drafts/{d.id}", headers=H, json=same).json()["checked"] == "passed"
        edited = {**same, "description": "зовсім інший текст"}
        assert client.patch(f"/assistant/api/drafts/{d.id}", headers=H, json=edited).json()["checked"] == "stale"
        assert client.post(f"/assistant/api/drafts/{d.id}/create", headers=H).status_code == 201


def test_codex_objections_are_shown_under_the_ticket(tmp_path, monkeypatch):
    bridge = FakeBridge()
    client, s = chat(tmp_path, monkeypatch, bridge)
    with client:
        app, cid, d = proposed(client, s)
        app._ticket_task.cancel()
        app.ticket_step()
        bridge.finish(f"fa-{cid[:12]}-{d.id}", review="failed", why=REVIEW_FAIL)
        app.ticket_step()
        got = app.store.draft(d.id)
        assert got.state == "draft" and got.checked == "notes"
        assert got.review_notes == "1. «Зараз банера немає» — прод не перевірено."
        assert got.summary == "Банер вебінару угорі дашборда учасника"


def test_when_night_shift_cannot_her_brief_stays_marked_unchecked(tmp_path, monkeypatch):
    bridge = FakeBridge()
    bridge.signed_in = False
    client, s = chat(tmp_path, monkeypatch, bridge)
    with client:
        app, cid, d = proposed(client, s)
        app._ticket_task.cancel()
        app.ticket_step()
        got = app.store.draft(d.id)
        assert got.state == "draft" and got.checked == "unchecked" and "перевірку ще не підключено" in got.review_notes
        assert got.summary == "Банер (бриф)"            # her own words, not lost


def test_nothing_written_or_a_dead_session_or_too_long_fall_back(tmp_path, monkeypatch):
    bridge = FakeBridge()
    client, s = chat(tmp_path, monkeypatch, bridge)
    with client:
        app, cid, d1 = proposed(client, s)
        app._ticket_task.cancel()
        app.ticket_step()
        t1 = f"fa-{cid[:12]}-{d1.id}"
        bridge.finish(t1, review="failed", why="", state="failed", write=False)
        app.ticket_step()
        notes = app.store.draft(d1.id).review_notes
        assert app.store.draft(d1.id).checked == "unchecked" and notes == "тікет не встигли дописати: перевірка не дійшла до згоди"
        # the next one: its session dies under it
        _, cid2, d2 = proposed(client, s)
        app.ticket_step()
        t2 = f"fa-{cid2[:12]}-{d2.id}"
        bridge.tasks[t2] = {"task_id": t2, "state": "running", "attempt_live": False}
        app.ticket_step()
        assert app.store.draft(d2.id).checked == "unchecked" and "обірвалася" in app.store.draft(d2.id).review_notes
        # and one that takes longer than allowed
        _, cid3, d3 = proposed(client, s)
        app.ticket_step()
        app.store._db.execute("UPDATE drafts SET prep_started=? WHERE id=?", (time.time() - 3600, d3.id))
        app.ticket_step()
        assert app.store.draft(d3.id).checked == "unchecked" and "не вклалася" in app.store.draft(d3.id).review_notes


def test_one_ticket_at_a_time_and_busy_waits(tmp_path, monkeypatch):
    bridge = FakeBridge()
    bridge.busy = 1
    client, s = chat(tmp_path, monkeypatch, bridge)
    with client:
        app, cid, d1 = proposed(client, s)
        _, cid2, d2 = proposed(client, s)
        app._ticket_task.cancel()
        app.ticket_step()                                # busy: tried later, not given up
        assert app.store.draft(d1.id).prep_task is None and app.store.draft(d1.id).state == "preparing"
        app.ticket_step()
        assert app.store.draft(d1.id).prep_task and app.store.draft(d2.id).prep_task is None   # the second waits
        bridge.finish(app.store.draft(d1.id).prep_task)
        app.ticket_step()
        assert app.store.draft(d1.id).state == "draft" and app.store.draft(d2.id).prep_task


def test_a_restart_mid_ticket_carries_on_with_the_same_task(tmp_path, monkeypatch):
    bridge = FakeBridge()
    client, s = chat(tmp_path, monkeypatch, bridge)
    with client:
        app, cid, d = proposed(client, s)
        app._ticket_task.cancel()
        app.ticket_step()
    tid = f"fa-{cid[:12]}-{d.id}"
    bridge.finish(tid)
    client2 = TestClient(server.build(s))              # the server comes back; the draft is still preparing
    with client2:
        app2 = client2.app.state.app
        app2._ticket_task.cancel()
        app2.ticket_step()
        assert app2.store.draft(d.id).checked == "passed"
    assert [c[0] for c in bridge.calls].count("dispatch") == 1


def test_a_draft_put_aside_while_it_is_written_stays_aside(tmp_path, monkeypatch):
    bridge = FakeBridge()
    client, s = chat(tmp_path, monkeypatch, bridge)
    with client:
        app, cid, d = proposed(client, s)
        app._ticket_task.cancel()
        app.ticket_step()
        assert client.post(f"/assistant/api/drafts/{d.id}/dismiss", headers=H).json()["state"] == "dismissed"
        bridge.finish(f"fa-{cid[:12]}-{d.id}")
        app.ticket_step()
        assert app.store.draft(d.id).state == "dismissed"


def test_without_the_engine_drafts_are_made_as_before(tmp_path):
    store = Store(tmp_path / "a.db")
    conv = store.create_conversation("m@x")
    args = {"kind": "feature", "summary": "Банер", "description": "## Що болить\nx", "open_questions": []}
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "propose_ticket", "arguments": args}}
    off = rpc(tools_mcp.Tools(yt_config(tmp_path), store, conv.id, engine_on=False), call)
    on = rpc(tools_mcp.Tools(yt_config(tmp_path), store, conv.id, engine_on=True), call)
    assert json.loads(off[0]["result"]["content"][0]["text"])["shown"] is True
    assert json.loads(on[0]["result"]["content"][0]["text"])["preparing"] is True
    assert [d.state for d in store._drafts("conversation_id=?", (conv.id,))] == ["draft", "preparing"]


def test_an_edit_gives_night_shift_the_tickets_current_text(tmp_path):
    convo = [Said("manager@x", "Кнопка має бути зеленою.", True)]
    cur = {"key": "NWND-7", "summary": "Кнопка", "description": "## Що болить\nСіра кнопка"}
    task, brief = task_text("fa-abc-3", "feature", "Кнопка зелена", "## Що болить\nНовий", convo, current=cur,
                            name="Олександра")
    assert task.startswith("Онови наявний тікет NWND-7") and "Кнопка має бути зеленою." in task
    assert len(task.encode()) <= 6000
    assert "# Поточний текст тікета NWND-7" in brief.text and "Сіра кнопка" in brief.text
    assert "# Правка, яку пропонує асистент (Олександра" in brief.text


def test_night_shift_gets_a_persons_addition_under_the_footer():
    from assistant.youtrack import strip_footer
    from tests.test_tickets import ADDED
    cur = {"key": "NWND-7", "summary": "Кнопка", "description": strip_footer(ADDED)}
    _, brief = task_text("fa-abc-4", "feature", "x", "y", [Said("m@x", "онови", True)], current=cur)
    assert "Кнопка має бути зеленою." in brief.text and "Створено асистентом" not in brief.text


def test_nothing_a_manager_reads_names_the_inside_workings():
    """Managers do not know Night Shift or Codex: the card, the reasons and what the tools tell her to say never
    name them. (Comments in the code may.)"""
    import re
    from assistant import ticket_engine
    root = Path(__file__).parent.parent
    js = [ln for ln in (root / "assistant/static/app.js").read_text().splitlines() if not ln.strip().startswith("//")]
    shown = "\n".join(js) + json.dumps(tools_mcp.TOOLS, ensure_ascii=False)
    shown += " ".join(ticket_engine.REASONS.values())
    for path in ("assistant/ticket_engine.py", "assistant/server.py", "assistant/tools_mcp.py"):
        src = (root / path).read_text()
        shown += " ".join(re.findall(r'EngineError\((?:f)?"([^"]*)"', src))
        shown += " ".join(re.findall(r'prep_finished\([^)]*?"unchecked", (?:f)?"([^"]*)"', src))
        shown += " ".join(re.findall(r'"note": \(\s*(?:f)?"([^"]*)"', src))
    tools_src = (root / "assistant/tools_mcp.py").read_text()
    shown += " ".join(re.findall(r'note = \((?:f)?"([^"]*)"', tools_src))
    for word in ("Night Shift", "Codex"):
        assert word not in shown, word


INSIDE = ("night shift", "codex", "claude", "dispatch", "tier=", "fa-ticket-engine", "exit", "auth")


def test_every_failure_a_manager_may_read_is_plain(caplog):
    """The messages actually formed on each failure, not the source: no engine, model or command names in them,
    while the server's log keeps what the administrator needs."""
    import logging
    caplog.set_level(logging.WARNING, logger="assistant.ticket_engine")
    seen = []

    def fails(make):
        try:
            make()
        except EngineError as e:
            seen.append(str(e))
            return
        raise AssertionError("must fail")

    for claude, codex in ((False, True), (True, False), (False, False)):
        b = FakeBridge()
        b.signed_in = claude
        orig = b.__call__

        def call(cmd, *a, _orig=orig, _codex=codex, **k):
            r = _orig(cmd, *a, **k)
            if cmd[1] == "status" and not _codex:
                d = json.loads(r.stdout)
                d["checks"] = [c if c["name"] != "auth.codex" else {**c, "status": "fail"} for c in d["checks"]]
                r = subprocess.CompletedProcess(cmd, 1, json.dumps(d), "")
            return r
        fails(lambda: TicketEngine(engine_cfg(), run=call).start("fa-x-1", "t", task_text("fa-x-1", "bug", "x", "y", [])[1]))
    assert "not signed in to: Codex" in caplog.text and "Claude, Codex" in caplog.text

    def refusing(cmd, *a, **k):
        if cmd[1] == "status":
            return FakeBridge()(cmd)
        if cmd[1] == "sync":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 77, "", "❌ Night Shift: dispatch.sh refused, codex quota, exit 77")
    fails(lambda: TicketEngine(engine_cfg(), run=refusing).start("fa-x-2", "t", task_text("fa-x-2", "bug", "x", "y", [])[1]))
    assert "codex quota" in caplog.text

    def broken(cmd, *a, **k):
        raise OSError("fa-ticket-engine: no such file")
    fails(lambda: TicketEngine(engine_cfg(), run=broken).ready())

    def garbled(cmd, *a, **k):
        return subprocess.CompletedProcess(cmd, 1, "not json", "Night Shift doctor crashed")
    fails(lambda: TicketEngine(engine_cfg(), run=garbled).ready())

    def nosync(cmd, *a, **k):
        if cmd[1] == "status":
            return FakeBridge()(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "rsync: codex dir")
    fails(lambda: TicketEngine(engine_cfg(), run=nosync).start("fa-x-3", "t", task_text("fa-x-3", "bug", "x", "y", [])[1]))

    b = FakeBridge()
    e = TicketEngine(engine_cfg(), run=b)
    for task in ({"state": "failed", "state_reason": "codex_unavailable", "receipt": {}},
                 {"state": "failed", "state_reason": "stalled", "receipt": {}}):
        fails(lambda t=task: e.result("fa-none", t))
    b.artifacts["fa-some"] = "Назва\n\nтекст"
    fails(lambda: e.result("fa-some", {"state": "failed", "state_reason": "needs-user",
                                       "receipt": {"review": "failed", "review_why": "STATE: COMPLETE\nVERDICT: FAIL"}}))
    assert len(seen) == 10
    for msg in seen:
        assert not [w for w in INSIDE if w in msg.lower()], msg


# ----------------------------------------------------------------------------------------------- «розберусь»
REPORT = "Кнопка не видна через перевизначений шаблон картки курсу.\n\n## Що з'ясувала\n- ...\n\n## Що пропоную\nТікет."


def investigation_chat(tmp_path, monkeypatch, bridge, answer="Знайшла: кнопку ховає оформлення картки. Оформити тікет?"):
    client, s = chat(tmp_path, monkeypatch, bridge)
    prompts = []

    async def run(cfg_, prompt, session, new_session=None, env=None, **kw):
        prompts.append(prompt)
        yield Event("session", data={"session_id": new_session or session})
        yield Event("done", answer)
    monkeypatch.setattr(server, "run_turn", run)
    return client, s, prompts


def asked(app, question="Чому учні не бачать кнопку сертифіката на сторінці курсу?"):
    cid = app.store.create_conversation("manager@example.com").id
    app.store.add_user_message(cid, "Учні не бачать кнопку сертифіката.", "manager@example.com", [])
    inv = app.store.add_investigation(cid, question)
    app._with_drafts(app.store.add_message(cid, "assistant", "Розберусь, напишу тут, щойно знайду.", meta={}))
    return cid, app.store.investigation(inv.id)


def drive(client, app, until, timeout=5.0):
    """Run the loop's steps until `until()` holds (the findings turn runs on the server's own event loop)."""
    import anyio
    end = time.time() + timeout
    while time.time() < end:
        app.ticket_step()
        client.portal.call(lambda: anyio.sleep(0))
        client.portal.call(app._tell_all_now)
        client.portal.call(lambda: anyio.sleep(0.05))
        if until():
            return
    raise AssertionError("did not happen")


def test_investigate_is_offered_only_with_the_engine(tmp_path):
    store = Store(tmp_path / "a.db")
    conv = store.create_conversation("m@x")
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "investigate", "arguments": {"question": "Чому учні не бачать кнопку сертифіката?"}}}
    lst = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    off = rpc(tools_mcp.Tools(yt_config(tmp_path), store, conv.id, engine_on=False), call, lst)
    on = rpc(tools_mcp.Tools(yt_config(tmp_path), store, conv.id, engine_on=True), call, lst)
    assert off[0]["result"]["isError"] and "investigate" not in [t["name"] for t in off[1]["result"]["tools"]]
    assert not on[0]["result"]["isError"] and "investigate" in [t["name"] for t in on[1]["result"]["tools"]]
    assert "Розбираєшся" in on[0]["result"]["content"][0]["text"] or "розбираєшся" in on[0]["result"]["content"][0]["text"]
    assert [i.question for i in store._investigations("conversation_id=?", (conv.id,))] == [
        "Чому учні не бачать кнопку сертифіката?"]


def test_a_finding_comes_back_as_her_message(tmp_path, monkeypatch):
    bridge = FakeBridge()
    client, s, prompts = investigation_chat(tmp_path, monkeypatch, bridge)
    with client:
        app = client.app.state.app
        app._ticket_task.cancel()
        cid, inv = asked(app)
        app.ticket_step()
        tid = f"fi-{cid[:12]}-{inv.id}"
        assert bridge.calls[-1][:2] == ["dispatch", tid]
        assert "Чому учні не бачать кнопку сертифіката" in bridge.texts[tid] and "artifacts/" + tid in bridge.texts[tid]
        assert "Учні не бачать кнопку сертифіката." in bridge.briefs[tid] and "Чернетка" not in bridge.briefs[tid]
        conv = client.get(f"/assistant/api/conversations/{cid}", headers=H).json()
        assert conv["messages"][-1]["investigations"][0]["state"] == "running"
        assert "report" not in conv["messages"][-1]["investigations"][0]      # the person reads her, not the report
        bridge.artifacts[tid] = REPORT
        bridge.finish(tid, review="failed", why="STATE: COMPLETE\nVERDICT: FAIL\n1. Прод не перевірено.", write=False)
        drive(client, app, lambda: app.store.investigation(inv.id).state == "told")
        conv = client.get(f"/assistant/api/conversations/{cid}", headers=H).json()
        last = conv["messages"][-1]
        assert last["role"] == "assistant" and last["text"].startswith("Знайшла") and last["meta"]["investigation"] == inv.id
        assert "Кнопка не видна через перевизначений шаблон" in prompts[-1] and "Прод не перевірено" in prompts[-1]
        assert conv["messages"][-2]["investigations"][0]["state"] == "told"
        assert not client.app.state.app.turns                                  # the conversation is free again


def test_a_failed_investigation_is_said_plainly(tmp_path, monkeypatch):
    bridge = FakeBridge()
    bridge.signed_in = False
    client, s, prompts = investigation_chat(tmp_path, monkeypatch, bridge, answer="Глибше зараз не вийшло, ось що бачу.")
    with client:
        app = client.app.state.app
        app._ticket_task.cancel()
        cid, inv = asked(app)
        drive(client, app, lambda: app.store.investigation(inv.id).state == "told")
        assert "не вдалося" in prompts[-1] and "перевірку ще не підключено" in prompts[-1]
        assert app.store.messages(cid)[-1].text == "Глибше зараз не вийшло, ось що бачу."


def test_she_waits_while_someone_is_talking_and_tickets_and_inquiries_share_the_queue(tmp_path, monkeypatch):
    bridge = FakeBridge()
    client, s, prompts = investigation_chat(tmp_path, monkeypatch, bridge)
    with client:
        app = client.app.state.app
        app._ticket_task.cancel()
        _, cid_t, d = proposed(client, s)                       # a ticket asked for first
        cid, inv = asked(app)
        app.ticket_step()
        assert app.store.draft(d.id).prep_task and app.store.investigation(inv.id).prep_task is None
        bridge.finish(app.store.draft(d.id).prep_task)
        app.ticket_step()
        assert app.store.investigation(inv.id).prep_task       # then the investigation
        tid = app.store.investigation(inv.id).prep_task
        bridge.artifacts[tid] = REPORT
        bridge.finish(tid, write=False)
        app.ticket_step()
        assert app.store.investigation(inv.id).state == "found"
        app.turns[cid] = server.Turn()                          # the manager is being answered right now
        client.portal.call(app._tell_all_now)
        assert app.store.investigation(inv.id).state == "found" and not prompts
        del app.turns[cid]
        drive(client, app, lambda: app.store.investigation(inv.id).state == "told")


def test_a_restart_keeps_what_was_found(tmp_path, monkeypatch):
    bridge = FakeBridge()
    client, s, prompts = investigation_chat(tmp_path, monkeypatch, bridge)
    with client:
        app = client.app.state.app
        app._ticket_task.cancel()
        cid, inv = asked(app)
        app.ticket_step()
        tid = app.store.investigation(inv.id).prep_task
    bridge.artifacts[tid] = REPORT
    bridge.finish(tid, write=False)
    client2 = TestClient(server.build(s))
    with client2:
        app2 = client2.app.state.app
        app2._ticket_task.cancel()
        drive(client2, app2, lambda: app2.store.investigation(inv.id).state == "told")
        assert [c[0] for c in bridge.calls].count("dispatch") == 1 and len(prompts) == 1


def test_a_failed_telling_keeps_the_finding_and_retry_tells_it(tmp_path, monkeypatch):
    """Telling the finding fails: it is not marked told, not retold by itself, and «Спробувати ще раз» tells that
    finding (the report goes into the turn), not the person's earlier question — also after a restart."""
    bridge = FakeBridge()
    client, s, prompts = investigation_chat(tmp_path, monkeypatch, bridge)
    outcomes = iter(["error", "error", "done"])

    async def run(cfg_, prompt, session, new_session=None, env=None, **kw):
        prompts.append(prompt)
        yield Event("session", data={"session_id": new_session or session})
        if next(outcomes) == "error":
            yield Event("error", "Claude впав")
        else:
            yield Event("done", "Знайшла: кнопку ховає оформлення картки.")
    monkeypatch.setattr(server, "run_turn", run)
    with client:
        app = client.app.state.app
        app._ticket_task.cancel()
        cid, inv = asked(app)
        app.ticket_step()
        tid = app.store.investigation(inv.id).prep_task
        bridge.artifacts[tid] = REPORT
        bridge.finish(tid, write=False)
        drive(client, app, lambda: app.store.investigation(inv.id).tell_error is not None)
        held = app.store.investigation(inv.id)
        assert held.state == "found" and held.answer_id is None
        last = app.store.messages(cid)[-1]
        assert last.role == "system" and last.meta["investigation"] == inv.id
        n = len(prompts)
        for _ in range(3):                                   # it is not retold by itself
            app.ticket_step()
            client.portal.call(app._tell_all_now)
        assert len(prompts) == n
        r = client.post(f"/assistant/api/conversations/{cid}/retry", headers=H)
        assert r.status_code == 202
        drive(client, app, lambda: app.store.investigation(inv.id).tell_error is not None or
              app.store.investigation(inv.id).state == "told")
        assert "Кнопка не видна через перевизначений шаблон" in prompts[-1]          # the finding, not the question
        assert app.store.investigation(inv.id).state == "found"                      # failed again: still held
    client2 = TestClient(server.build(s))                                            # a restart in between
    with client2:
        app2 = client2.app.state.app
        app2._ticket_task.cancel()
        assert client2.post(f"/assistant/api/conversations/{cid}/retry", headers=H).status_code == 202
        drive(client2, app2, lambda: app2.store.investigation(inv.id).state == "told")
        assert app2.store.messages(cid)[-1].text == "Знайшла: кнопку ховає оформлення картки."
        assert "Кнопка не видна через перевизначений шаблон" in prompts[-1]


def test_another_projects_assistant_is_named_in_its_tasks():
    said = [Said("manager@x", "Кнопка має бути зеленою.", True), Said("Мирон Петрович", "Оформлю.", False)]
    task, brief = task_text("fa-1", "feature", "Кнопка", "## Що болить\nСіра", said,
                            project="Contoso", name="Мирон Петрович")
    assert "фабрики Contoso" in task and "Мирон Петрович" in task
    assert "Northwind" not in task and "Олександр" not in task
    assert "# Чернетка асистента (Мирон Петрович" in brief.text and "Олександр" not in brief.text
    inv, _ = task_text("fi-1", "research", "Чому падає?", "", said, question="Чому падає?",
                       project="Contoso", name="Мирон Петрович")
    assert "Мирон Петрович" in inv and "Олександр" not in inv
