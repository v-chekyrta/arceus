"""What the Contoso assistant needs beyond Northwind's: a client channel read in silence, a partner channel answered in,
a year of history, requests to the expert for what she or he lacks, a persona of either gender, the factory in words."""

import asyncio
import json
import time
from pathlib import Path

import yaml

from assistant import server
from assistant.tools_mcp import Tools
from assistant.youtrack import YouTrackError
from tests.test_slack import BOT, PEOPLE, TEAM, make, mention, settle, settings

EXT, PARTNER = "CEXTERNAL", "CPARTNER"
ROOT = Path(__file__).parent.parent
PEOPLE.setdefault("UCLIENT", {"id": "UCLIENT", "team_id": "TCLIENT",
                              "profile": {"email": "client@partner.example", "real_name": "Клієнт"}})


def in_channel(channel, text, ts, user="UCLIENT", ext=True, thread=None, kind="app_mention"):
    ev = {"type": kind, "channel": channel, "user": user, "text": text, "ts": ts}
    if thread:
        ev["thread_ts"] = thread
    return {"team_id": TEAM, "is_ext_shared_channel": ext, "event": ev}


def partners(app, bridge):
    app.s.slack.read_channels = (EXT, PARTNER)
    app.s.slack.listen_channels = (EXT,)
    app.s.slack.partner_channels = (PARTNER,)
    bridge.backfilled.update({EXT, PARTNER})


def test_the_client_channel_is_read_but_she_never_says_anything_there(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        partners(app, bridge)
        now = time.time()
        bridge.receive(in_channel(EXT, "Коли буде реліз Android?", f"{now:.6f}", kind="message"))
        bridge.receive(in_channel(EXT, f"<@{BOT}> підкажіть статус", f"{now + 1:.6f}"))      # a mention
        bridge.receive(in_channel(EXT, f"<@{BOT}> і тут?", f"{now + 2:.6f}", user="UDANA", thread=f"{now:.6f}"))
        await settle(app, bridge)
        assert prompts == [] and app.store.conversations() == []
        for call in ("chat_postMessage", "chat_postEphemeral", "reactions_add"):
            assert web.of(call) == [], call
        logged = [m["text"] for m in app.store.channel_messages([EXT])]
        assert len(logged) == 3 and "Коли буде реліз Android?" in logged     # kept although Slack says shared
        assert await bridge.internal(EXT, hint=True) is False                 # never hers to speak in
        assert len(app.store.channel_messages([EXT])) == 3                    # and not purged for being shared
        dm = app.store.link_slack(TEAM, "D1", "", "dana@x.com", "dana@x.com", "dm")
        t = Tools(app.s.youtrack, app.store, dm, channels=app.s.slack.read_channels)
        assert "реліз Android" in t.channel_read({"query": "реліз"})
        await bridge.stop()
    asyncio.run(go())


def test_in_the_partner_channel_she_answers_people_of_the_other_company_without_tickets(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        partners(app, bridge)
        bridge.receive(in_channel(PARTNER, f"<@{BOT}> як влаштовано звіт?", f"{time.time():.6f}"))
        await settle(app, bridge)
        assert len(prompts) == 1 and "Клієнт (інша компанія, UCLIENT)" in prompts[0]
        posted = web.of("chat_postMessage")
        assert posted and posted[-1]["channel"] == PARTNER and web.of("chat_postEphemeral") == []
        assert await bridge.internal(PARTNER, fresh=True) is True
        cid = app.store.slack_links()[0].conversation_id
        asked = app.store.last_user_message(cid)
        t = Tools(app.s.youtrack, app.store, cid, creators=app.s.ticket_creators, turn_ids=[asked.id])
        try:
            t._may_ask_tickets()
            raise AssertionError("a partner got a ticket")
        except YouTrackError as e:
            assert "dana@x.com" in str(e)
        # Any other shared channel stays closed, as before.
        bridge.receive(in_channel("COTHERSHARED", f"<@{BOT}> а тут?", f"{time.time():.6f}", user="UDANA"))
        await settle(app, bridge)
        assert len(prompts) == 1 and any("спільних" in n["text"] for n in web.of("chat_postEphemeral"))
        await bridge.stop()
    asyncio.run(go())


def test_a_year_of_history_and_a_base_that_read_less_reads_the_rest(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        partners(app, bridge)
        app.s.slack.history_days = 365
        windows = []

        async def history(channel, oldest, latest, limit, cursor=None, **_):
            windows.append((channel, float(oldest), float(latest)))
            return {"messages": [{"ts": f"{time.time() - 330 * 86400:.6f}", "user": "USER2",
                                  "text": "старе рішення з минулого року"}], "has_more": False}
        web.conversations_history = history
        # A base from before: 120 days read and done, nothing recorded about how far back.
        app.store.backfill_start(EXT, f"{time.time() - 120 * 86400:.6f}", f"{time.time() - 3600:.6f}")
        app.store.backfill_page(EXT, None)
        bridge.backfilled.clear()
        bridge.team = TEAM
        await bridge._backfill_if_ours(EXT)                  # as at a start: the owner named them, so they are read
        await bridge._backfill_if_ours(PARTNER)
        year = time.time() - 365 * 86400
        assert all(abs(oldest - year) < 120 for _, oldest, _ in windows)          # both from a year back
        assert app.store.backfill_state(EXT)["done"] and app.store.backfill_state(EXT)["covered_from"]
        windows.clear()
        await bridge._backfill(TEAM, EXT)                                          # covered: only what is new
        assert windows and windows[0][1] > time.time() - 7200
        dm = app.store.link_slack(TEAM, "D1", "", "dana@x.com", "dana@x.com", "dm")
        t = Tools(app.s.youtrack, app.store, dm, channels=app.s.slack.read_channels, history_days=365)
        assert "старе рішення" in t.channel_read({"query": "рішення", "days": 365})
        assert "старе рішення" not in Tools(app.s.youtrack, app.store, dm, channels=app.s.slack.read_channels
                                            ).channel_read({"query": "рішення", "days": 365})   # 120 days only
        before = time.strftime("%Y-%m-%d", time.localtime(time.time() - 300 * 86400))
        assert "старе рішення" in t.channel_read({"query": "рішення", "days": 60, "before": before})
        await bridge.stop()
    asyncio.run(go())


def test_a_missing_capability_reaches_the_expert_once_privately_and_a_failed_dm_is_sent_later(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        cid = app.store.link_slack(TEAM, "D2", "", "serhii@x.com", "serhii@x.com", "Slack · Сергій")
        asked = app.store.add_user_message(cid, "глянь документ https://docs.google.com/document/d/abc", "serhii@x.com", [])
        t = Tools(app.s.youtrack, app.store, cid, expert=app.s.expert, turn_ids=[asked.id], project="Contoso")
        req = {"need": "Читати документи Google Drive з папки проєкту", "why": "Сергій просить звірити вимоги з документом",
               "source": "https://docs.google.com/document/d/abc",
               "error": "403 with Authorization: Bearer ya29.abcdefghijklmnop xoxb-1-2-secret"}
        first = json.loads(t.call("request_capability", req))
        again = json.loads(t.call("request_capability", {**req, "need": "читати документи  Google Drive з папки проєкту!"}))
        assert first["new"] and not again["new"] and first["request"] == again["request"]
        web.fail_post = 1
        await bridge.post_pending()                                   # Slack fails: stays to send
        await bridge.post_pending()
        await bridge.post_pending()
        dms = [p for p in web.of("chat_postMessage") if p["channel"] == "D-UDANA"]
        assert len(dms) == 1
        text = dms[0]["text"]
        assert "Google Drive" in text and "Contoso" in text and "serhii@x.com" in text and "docs.google.com" in text
        assert "ya29" not in text and "xoxb" not in text and "[приховано]" in text and "2 рази" in text
        assert "глянь документ" not in text and cid not in text       # not the conversation itself
        assert not app.visible(app.store.conversation(cid), "dana@x.com")   # and no way into it
        await bridge.stop()
    asyncio.run(go())


def test_youtrack_refusing_the_bot_tells_the_expert_whatever_she_decides(tmp_path, monkeypatch):
    from assistant import tools_mcp

    class Refusing:
        def __init__(self, _cfg):
            pass

        def issue(self, key):
            raise YouTrackError("YouTrack не пускає бота: токен недійсний або в акаунта бота немає прав на цей проєкт.", 403)

        search = issue

        def close(self):
            pass
    monkeypatch.setattr(tools_mcp, "Client", Refusing)
    app = server.App(settings(tmp_path))
    cid = app.store.create_conversation("dana@x.com").id
    t = Tools(app.s.youtrack, app.store, cid, expert=app.s.expert, project="Contoso")
    for _ in range(3):
        try:
            t.call("youtrack_issue", {"key": "SHOP-1"})
        except YouTrackError:
            pass
    reqs = app.store.capability_requests_new()
    assert len(reqs) == 1 and reqs[0]["times"] == 3 and "YouTrack" in reqs[0]["need"] and "403" in reqs[0]["error"]


def runner_config(tmp: Path) -> Path:
    p = tmp / "runner.yaml"
    p.write_text(yaml.safe_dump({
        "tracker": {"project": "SHOP", "bot_user": "Agent01", "allowed_users": ["lead.dev", "qa.lead"],
                    "tags": {"queue": "agent", "needs_input": "agent-needs-input", "failed": "agent-failed",
                             "research": "agent-research"},
                    "states": {"queued": "To Do", "in_progress": "In Progress", "code_review": "Code Review",
                               "merged": "Testing Dev"},
                    "platforms": {"android": ["mobile/shop-android"], "ios": ["mobile/shop-ios"]}},
        "code_host": {"url": "https://gitlab.com", "group": "acme-mobile",
                      "bot_user": "review.bot"},
        "schedule": {"poll_working": "2m", "poll_off_hours": "10m", "review_debounce": "5m"}}))
    return p


def test_myron_speaks_as_a_man_and_knows_this_factory_and_the_project(tmp_path):
    s = settings(tmp_path)
    s.brain.prompt_file.write_text((ROOT / "assistant" / "prompt.md").read_text())
    s.persona_file, s.gender = ROOT / "tests" / "fixtures" / "persona-he.md", "m"
    s.name, s.project, s.youtrack.project = "Мирон Петрович", "Contoso", "SHOP"
    s.expert_name = "Андрія"                       # assistant.expert_name: how he names the expert to people
    s.factory_config = runner_config(tmp_path)
    s.knowledge_file = tmp_path / "contoso.md"
    s.knowledge_file.write_text("Contoso — платформа замовлень для складів.")
    text = server.App(s).s.brain.prompt_file.read_text()
    assert "Ти Мирон Петрович" in text and "{" not in text.replace("{\"", "")
    assert "сам вирішуєш" in text and "сама вирішуєш" not in text and "покликав Андрія" in text
    assert "«це вже є в SHOP-…" in text and "маяк" not in text
    for fact in ("«To Do»", "«agent»", "lead.dev, qa.lead", "@review.bot", "Submit review",
                 "/continue", "«agent-needs-input»", "«agent-research»", "«Testing Dev»", "mobile/shop-android"):
        assert fact in text, fact
    assert text.rstrip().endswith("Contoso — платформа замовлень для складів.")


def test_alexandra_stays_herself(tmp_path):
    s = settings(tmp_path)
    s.brain.prompt_file.write_text((ROOT / "assistant" / "prompt.md").read_text())
    s.persona_file = ROOT / "tests" / "fixtures" / "persona-she.md"
    s.expert_name = "Андрія"
    text = server.App(s).s.brain.prompt_file.read_text()
    assert "сама вирішуєш" in text and "покликала Андрія" in text and "Мельник" not in text and "маяк" in text
    assert "Ти жінка" in text and "{сама|сам}" not in text and "request_capability" in text


def test_nothing_reaches_the_client_channel_by_any_path(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        partners(app, bridge)
        # An event stored before the channel was named a listen channel, taken again at a start.
        item = {"event": {"type": "app_mention", "channel": EXT, "user": "UDANA", "text": f"<@{BOT}> x",
                          "ts": "7.0"}, "team": TEAM, "ext": True}
        app.store.slack_take(TEAM, EXT, "7.0", item)
        await bridge._take(item)
        # An old card's button pressed there, and posts of a thread that lives there.
        await bridge._interact({"type": "block_actions", "user": {"id": "UDANA"}, "channel": {"id": EXT},
                                "actions": [{"action_id": "fa_create", "value": "1"}]})
        cid = app.store.link_slack(TEAM, EXT, "7.0", None, "dana@x.com", "old thread")
        app.store.add_message(cid, "assistant", "пізня знахідка")
        await bridge.post_pending()
        link = app.store.slack_link(cid)
        assert await bridge._post_text(link, 1, "x") is True
        await bridge._react(EXT, "7.0", True, TEAM)
        for call in ("chat_postMessage", "chat_postEphemeral", "reactions_add", "chat_update"):
            assert [c for c in web.of(call) if c.get("channel") == EXT] == [], call
        await bridge.stop()
    asyncio.run(go())


def test_a_partner_named_like_a_ticket_creator_still_gets_no_ticket(tmp_path, monkeypatch):
    PEOPLE["UFAKE"] = {"id": "UFAKE", "team_id": "TCLIENT", "profile": {"real_name": "dana@x.com"}}

    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        partners(app, bridge)
        bridge.receive(in_channel(PARTNER, f"<@{BOT}> зроби тікет", f"{time.time():.6f}", user="UFAKE"))
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        asked = app.store.last_user_message(cid)
        assert asked.author != "dana@x.com" and "UFAKE" in asked.author
        t = Tools(app.s.youtrack, app.store, cid, creators=app.s.ticket_creators, turn_ids=[asked.id])
        try:
            t._may_ask_tickets()
            raise AssertionError("an impostor got a ticket")
        except YouTrackError:
            pass
        await bridge.stop()
    asyncio.run(go())


def test_secrets_of_every_common_shape_are_masked(tmp_path):
    app = server.App(settings(tmp_path))
    cid = app.store.create_conversation("dana@x.com").id
    t = Tools(app.s.youtrack, app.store, cid, expert=app.s.expert)
    t.call("request_capability", {"need": "доступ до сховища звітів", "why": "людина просить звіт",
                                  "source": "https://x.example/f?token=abc123&a=1 https://u:pw@host/x",
                                  "error": "password=hunter2 ya29.Abc-def Authorization: Basic dXNlcjpwYXNz api_key: K1"})
    r = app.store.capability_requests_new()[0]
    joined = r["source"] + r["error"]
    for bad in ("abc123", "pw@", "hunter2", "ya29", "dXNlcjpwYXNz", "K1"):
        assert bad not in joined, bad


def test_an_interrupted_shorter_read_goes_on_to_the_whole_year_in_one_start(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        partners(app, bridge)
        app.s.slack.history_days = 365
        seen = []

        async def history(channel, oldest, latest, limit, cursor=None, **_):
            seen.append(float(oldest))
            return {"messages": [], "has_more": False}
        web.conversations_history = history
        app.store.backfill_start(EXT, f"{time.time() - 120 * 86400:.6f}", f"{time.time():.6f}")   # cut short
        await bridge._backfill(TEAM, EXT)
        assert len(seen) == 2 and abs(seen[-1] - (time.time() - 365 * 86400)) < 120
        await bridge.stop()
    asyncio.run(go())


def test_paging_back_by_ts_misses_nothing_on_a_busy_day(tmp_path):
    app = server.App(settings(tmp_path))
    app.store.set_channel_status(EXT, False)
    day = time.time() - 3 * 86400
    for i in range(90):
        app.store.log_message(TEAM, EXT, f"{day + i:.6f}", None, "U", f"повідомлення {i}")
    dm = app.store.link_slack(TEAM, "D1", "", "dana@x.com", "dana@x.com", "dm")
    t = Tools(app.s.youtrack, app.store, dm, channels=(EXT,), history_days=365)
    first = json.loads(t.channel_read({"days": 30, "limit": 80}))["messages"]
    rest = json.loads(t.channel_read({"days": 30, "limit": 80, "before": first[-1]["ts"]}))["messages"]
    assert len(first) + len(rest) == 90


def test_braces_that_are_not_gender_forms_survive(tmp_path):
    s = settings(tmp_path)
    s.brain.prompt_file.write_text('{сама|сам} і {"pattern": "android|ios"} {a|b}')
    s.gender = "m"
    assert server.App(s).s.brain.prompt_file.read_text() == 'сам і {"pattern": "android|ios"} {a|b}'
