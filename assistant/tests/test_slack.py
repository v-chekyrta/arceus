"""The assistant in Slack, against a fake Slack: what she answers, where, for whom, once."""

import asyncio
import json
from pathlib import Path

from starlette.testclient import TestClient

from assistant import server
from assistant.brain import Event
from assistant.slack import SlackBridge, SlackConfig, chunks, slack_error, to_mrkdwn
from tests.test_chat import cfg

TEAM, BOT = "T1", "UBOT"
PEOPLE = {
    "UDANA": {"id": "UDANA", "team_id": TEAM, "profile": {"email": "dana@x.com", "real_name": "Дана"}},
    "USER2": {"id": "USER2", "team_id": TEAM, "profile": {"email": "serhii@x.com", "real_name": "Сергій"}},
    "UOUT": {"id": "UOUT", "team_id": TEAM, "profile": {"email": "stranger@x.com", "real_name": "Хтось"}},
    "UGUEST": {"id": "UGUEST", "team_id": TEAM, "is_restricted": True,
               "profile": {"email": "dana2@x.com", "real_name": "Гість"}},
}


class FakeWeb:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.n = 0
        self.fail_post = 0           # this many posts fail before they succeed
        self.fail_attempts: set[int] = set()   # these post attempts (1, 2, …) fail
        self.attempts = 0
        self.shared = {"C1": False, "CEXT": True}   # who is in a channel; missing: Slack will not say

    def _log(self, name, kw):
        self.calls.append((name, kw))

    def of(self, name):
        return [kw for n, kw in self.calls if n == name]

    async def users_info(self, user):
        self._log("users_info", {"user": user})
        return {"user": PEOPLE[user]}

    async def reactions_add(self, **kw):
        self._log("reactions_add", kw)

    async def reactions_remove(self, **kw):
        self._log("reactions_remove", kw)

    async def chat_postEphemeral(self, **kw):
        self._log("chat_postEphemeral", kw)

    async def conversations_info(self, channel):
        self._log("conversations_info", {"channel": channel})
        if channel not in self.shared:
            raise RuntimeError("missing_scope")
        return {"channel": {"id": channel, "is_ext_shared": self.shared[channel]}}

    async def chat_postMessage(self, **kw):
        self.attempts += 1
        if self.attempts in self.fail_attempts:
            raise RuntimeError("network")
        if self.fail_post:
            self.fail_post -= 1
            raise RuntimeError("network")
        self.n += 1
        self._log("chat_postMessage", kw)
        return {"ts": f"9.{self.n}"}

    async def users_lookupByEmail(self, email):
        self._log("users_lookupByEmail", {"email": email})
        return {"user": next(u for u in PEOPLE.values() if u["profile"]["email"] == email)}

    async def conversations_open(self, users):
        self._log("conversations_open", {"users": users})
        return {"channel": {"id": "D-" + users}}

    async def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack.example/{channel}/{message_ts}"}

    async def chat_update(self, **kw):
        self._log("chat_update", kw)

    async def conversations_replies(self, **kw):
        self._log("conversations_replies", kw)
        return {"messages": [{"ts": "5.0", "user": "USER2", "text": "Клієнтка каже, що відео не грає"},
                             {"ts": "5.1", "user": "UDANA", "text": f"<@{BOT}> глянь"}]}


def settings(tmp: Path) -> server.Settings:
    s = cfg(tmp)
    s.allowed_users = ["dana@x.com", "serhii@x.com", "dana2@x.com"]
    s.ticket_creators = ["dana@x.com", "serhii@x.com"]
    s.expert = "dana@x.com"
    s.slack = SlackConfig(tmp / "assistant.env", web_url="https://f.example/assistant/")
    return s


def make(tmp: Path, monkeypatch, replies=None, gate: asyncio.Event | None = None):
    prompts: list[str] = []

    async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
        prompts.append(prompt)
        if gate is not None:
            await gate.wait()
        text = (replies or {}).get(len(prompts), f"відповідь {len(prompts)}")
        yield Event("delta", text)
        yield Event("done", text)

    monkeypatch.setattr(server, "run_turn", run)
    app = server.App(settings(tmp))
    web = FakeWeb()
    bridge = SlackBridge(app, app.s.slack, web=web)
    bridge.team, bridge.bot_user = TEAM, BOT
    app.after_turn.append(bridge._after_turn)
    app.slack = bridge
    return app, bridge, web, prompts


async def settle(app, bridge, rounds=60):
    """Until nothing is being taken or answered, posting what there is."""
    for _ in range(rounds):
        await asyncio.sleep(0.01)
        if bridge.tasks:
            await asyncio.gather(*list(bridge.tasks), return_exceptions=True)
        if not app.turns and not bridge.tasks:
            await asyncio.sleep(0.01)
            if not app.turns and not bridge.tasks:
                break
    await bridge.post_pending()


def dm(text, ts="1.0", user="UDANA", **extra):
    return {"team_id": TEAM, "event": {"type": "message", "channel_type": "im", "channel": "D1", "user": user,
                                       "text": text, "ts": ts, **extra}}


def mention(text, ts="2.0", thread=None, user="UDANA", ext=False):
    ev = {"type": "app_mention", "channel": "C1", "user": user, "text": f"<@{BOT}> {text}", "ts": ts}
    if thread:
        ev["thread_ts"] = thread
    return {"team_id": TEAM, "is_ext_shared_channel": ext, "event": ev}


def test_direct_message_gets_eyes_then_her_answer(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        assert bridge.receive(dm("Привіт, що з відео?"))
        await settle(app, bridge)
        assert web.of("reactions_add") == [{"channel": "D1", "timestamp": "1.0", "name": "eyes"}]
        posts = web.of("chat_postMessage")
        assert [p["text"] for p in posts] == ["відповідь 1"] and posts[0]["thread_ts"] is None
        assert web.of("reactions_remove") == [{"channel": "D1", "timestamp": "1.0", "name": "eyes"}]
        assert "[Дана (dana@x.com)," in prompts[0] and "Привіт, що з відео?" in prompts[0]
        # The same DM goes on in the same conversation, with the same Claude session.
        assert bridge.receive(dm("І ще одне", ts="1.5"))
        await settle(app, bridge)
        assert len(app.store.slack_links()) == 1 and len(prompts) == 2
        await bridge.stop()
    asyncio.run(go())


def test_a_resent_event_and_a_mention_seen_twice_are_taken_once(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        assert bridge.receive(dm("раз"))
        assert not bridge.receive(dm("раз"))                     # Slack resent it
        m = mention("що там?")
        assert bridge.receive(m)
        as_message = {"team_id": TEAM, "event": {**m["event"], "type": "message", "channel_type": "channel"}}
        assert not bridge.receive(as_message)                     # the same message as a channel message
        await settle(app, bridge)
        assert len(prompts) == 2 and len(web.of("chat_postMessage")) == 2
        await bridge.stop()
    asyncio.run(go())


def test_she_ignores_herself_bots_edits_and_channel_chatter(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        assert not bridge.receive(dm("я сама", user=BOT))
        assert not bridge.receive({"team_id": TEAM, "event": {"type": "message", "channel_type": "im", "channel": "D1",
                                                              "bot_id": "B9", "user": "UX", "text": "бот", "ts": "3"}})
        assert not bridge.receive(dm("змінено", subtype="message_changed"))
        assert not bridge.receive({"team_id": TEAM, "event": {"type": "message", "channel_type": "channel",
                                                              "channel": "C1", "user": "UDANA", "text": "обід?",
                                                              "ts": "4"}})
        await settle(app, bridge)
        assert prompts == [] and web.calls == []
        await bridge.stop()
    asyncio.run(go())


def test_anyone_of_the_team_may_write_but_guests_and_shared_channels_get_a_note(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        bridge.receive(dm("а мені?", ts="1.1", user="UGUEST"))
        bridge.receive(mention("глянь тікет", ext=True))
        await settle(app, bridge)
        assert prompts == [] and web.of("chat_postMessage") == []
        notes = web.of("chat_postEphemeral")
        assert len(notes) == 2 and any("спільних з іншими компаніями" in n["text"] for n in notes)
        assert app.store.conversations() == []
        bridge.receive(dm("покажи код", ts="1.2", user="UOUT"))   # a colleague not on any list: she answers
        await settle(app, bridge)
        assert len(prompts) == 1 and "stranger@x.com" in prompts[0]
        await bridge.stop()
    asyncio.run(go())


def test_a_mention_opens_a_thread_she_keeps_answering_in(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        bridge.receive(mention("що з відео?", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        # Called into a thread that was already going: she reads what was said before.
        assert "Клієнтка каже, що відео не грає" in prompts[0]
        assert web.of("chat_postMessage")[0]["thread_ts"] == "5.0"
        follow = {"team_id": TEAM, "event": {"type": "message", "channel_type": "channel", "channel": "C1",
                                             "user": "USER2", "text": "а на телефоні?", "ts": "5.2", "thread_ts": "5.0"}}
        assert bridge.receive(follow)                             # no mention: the thread is hers
        await settle(app, bridge)
        assert len(prompts) == 2 and web.of("chat_postMessage")[1]["thread_ts"] == "5.0"
        await bridge.stop()
    asyncio.run(go())


def test_messages_sent_while_she_answers_wait_and_are_answered_together(tmp_path, monkeypatch):
    async def go():
        gate = asyncio.Event()
        app, bridge, web, prompts = make(tmp_path, monkeypatch, gate=gate)
        bridge.receive(dm("перше"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if prompts:
                break
        bridge.receive(dm("друге", ts="1.1"))
        bridge.receive(dm("третє", ts="1.2"))
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert len(prompts) == 1                                  # not a second turn beside the first
        gate.set()
        await settle(app, bridge)
        assert len(prompts) == 2 and "друге" in prompts[1] and "третє" in prompts[1]
        assert [p["text"] for p in web.of("chat_postMessage")] == ["відповідь 1", "відповідь 2"]
        await bridge.stop()
    asyncio.run(go())


def test_a_failed_post_is_posted_on_the_next_pass_once(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        web.fail_post = 1
        bridge.receive(dm("привіт"))
        await settle(app, bridge)
        assert web.of("chat_postMessage") == []
        await bridge.post_pending()
        await bridge.post_pending()
        assert [p["text"] for p in web.of("chat_postMessage")] == ["відповідь 1"]
        await bridge.stop()
    asyncio.run(go())


def test_a_restart_finishes_what_was_taken_and_answers_what_was_left(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        # Taken and acknowledged, then the server stopped before storing it.
        item = {"event": dm("встигла?")["event"], "team": TEAM, "ext": False}
        assert app.store.slack_take(TEAM, "D1", "1.0", item)
        # Stored, then the server stopped before answering.
        cid = app.store.link_slack(TEAM, "C1", "7.0", None, "dana@x.com", "t")
        app.store.add_user_message(cid, "без відповіді", "dana@x.com", [])
        app2, bridge2, web2, prompts2 = make(tmp_path, monkeypatch)
        assert app2.store.messages(cid)[-1].role == "user"        # no «press retry» note in a Slack conversation
        bridge2.resume()
        await settle(app2, bridge2)
        assert len(prompts2) == 2 and len(web2.of("chat_postMessage")) == 2
        await bridge2.stop()
    asyncio.run(go())


def test_a_failed_turn_says_so_in_slack_words_and_the_next_message_asks_again(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)

        async def broken(_cfg, prompt, *a, **k):
            prompts.append(prompt)
            yield Event("error", "Claude не відповів за 10 хв, відповідь перервано.")
        monkeypatch.setattr(server, "run_turn", broken)
        bridge.receive(dm("питання"))
        await settle(app, bridge)
        text = web.of("chat_postMessage")[0]["text"]
        assert "не відповів" in text and "Напишіть мені ще раз" in text and "Спробувати" not in text
        async def works(_cfg, prompt, *a, **k):
            prompts.append(prompt)
            yield Event("done", "тепер так")
        monkeypatch.setattr(server, "run_turn", works)
        bridge.receive(dm("ще раз", ts="1.1"))
        await settle(app, bridge)
        assert "питання" in prompts[-1] and "ще раз" in prompts[-1]   # the unanswered question goes with it
        await bridge.stop()
    asyncio.run(go())


def test_without_the_teams_key_slack_hears_it_plainly_and_no_claude_starts(tmp_path, monkeypatch):
    """The real brain, its key missing: what the person in Slack reads, and that no Claude ran on anyone's sign-in."""
    from assistant import brain

    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        monkeypatch.setattr(server, "run_turn", brain.run_turn)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-owner")
        marker = tmp_path / "claude-ran"
        fake = tmp_path / "claude"
        fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
        fake.chmod(0o755)
        app.s.brain.claude_bin, app.s.brain.auth = str(fake), "api_key"
        app.s.brain.secrets, app.s.brain.claude_home = tmp_path / "assistant.env", tmp_path / "claude-home"
        bridge.receive(dm("питання"))
        await settle(app, bridge)
        text = web.of("chat_postMessage")[0]["text"]
        assert "API-ключ" in text and "налаштуваннях асистента" in text and not marker.exists()
        await bridge.stop()
    asyncio.run(go())


def test_a_draft_card_and_its_button_make_one_ticket(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        made = []

        def create(self, d, footer):
            made.append(d.id)
            return {"key": "NWND-9", "url": "https://yt/NWND-9", "tagged": None, "board": None}
        monkeypatch.setattr(server.App, "_create", create)
        monkeypatch.setattr(type(app.s.youtrack), "ready", property(lambda self: True))
        bridge.receive(dm("зроби тікет"))
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        msg_id = app.store.messages(cid)[-1].id
        d = app.store.add_draft(cid, "bug", "Відео не грає", "## Що болить\nВідео **не** грає")
        app.store.attach_drafts(cid, msg_id)
        await bridge.post_pending()
        card = web.of("chat_postMessage")[-1]
        buttons = [b for b in card["blocks"] if b["type"] == "actions"][0]["elements"]
        assert buttons[0]["text"]["text"] == "Створити тікет" and buttons[0]["value"] == str(d.id)
        assert "https://f.example/assistant/#" + cid in json.dumps(card["blocks"])
        press = {"type": "block_actions", "user": {"id": "UDANA"}, "channel": {"id": "D1"},
                 "actions": [{"action_id": "fa_create", "value": str(d.id)}]}
        await asyncio.gather(bridge._interact(press), bridge._interact(press))
        await bridge.post_pending()
        assert made == [d.id] and app.store.draft(d.id).state == "created"
        update = web.of("chat_update")[-1]
        assert "NWND-9" in json.dumps(update["blocks"]) and "actions" not in json.dumps(update["blocks"])
        # Someone she does not talk to cannot press it.
        d2 = app.store.add_draft(cid, "bug", "Друге", "опис")
        app.store.attach_drafts(cid, msg_id)
        await bridge._interact({**press, "user": {"id": "UOUT"}, "actions": [{"action_id": "fa_create",
                                                                              "value": str(d2.id)}]})
        assert app.store.draft(d2.id).state == "draft"
        await bridge.stop()
    asyncio.run(go())


def test_a_slack_dm_is_seen_in_the_web_chat_only_by_its_person(tmp_path):
    s = settings(tmp_path)
    s.allowed_users = ["dana@x.com", "serhii@x.com"]
    web = server.build(s)
    app = web.state.app
    cid = app.store.link_slack(TEAM, "D1", "", "dana@x.com", "dana@x.com", "Slack · Дана")
    m = app.store.add_user_message(cid, "особисте", "dana@x.com", [])
    app.store.add_draft(cid, "bug", "Особистий тікет", "опис")
    app.store.attach_drafts(cid, m.id)
    did = app.store.drafts_of(cid)[0].id
    thread = app.store.link_slack(TEAM, "C1", "5.0", None, "dana@x.com", "Slack · тред")
    with TestClient(web) as c:
        dana, serhii = {"X-Assistant-User": "dana@x.com"}, {"X-Assistant-User": "serhii@x.com"}
        mine = [x["id"] for x in c.get("/assistant/api/conversations", headers=dana).json()["conversations"]]
        theirs = [x["id"] for x in c.get("/assistant/api/conversations", headers=serhii).json()["conversations"]]
        assert cid in mine and thread in mine and cid not in theirs and thread in theirs
        assert c.get(f"/assistant/api/conversations/{cid}", headers=serhii).status_code == 404
        assert c.get(f"/assistant/api/conversations/{cid}/events", headers=serhii).status_code == 404
        assert c.get(f"/assistant/api/drafts/{did}", headers=serhii).status_code == 404
        assert c.post(f"/assistant/api/drafts/{did}/create", headers=serhii).status_code == 404
        assert c.get(f"/assistant/api/conversations/{cid}", headers=dana).json()["slack"] == "dm"
        # Written from the web, it would never reach Slack: refused, with where to write instead.
        r = c.post(f"/assistant/api/conversations/{cid}/messages", headers=dana, json={"text": "з вебу"})
        assert r.status_code == 409 and "Slack" in r.json()["error"]


def test_formatting_for_slack():
    assert to_mrkdwn("## Що болить\n**Важливо**: [тікет](https://yt/X-1)\n- раз") == \
        "*Що болить*\n*Важливо*: <https://yt/X-1|тікет>\n• раз"
    assert to_mrkdwn("a < b & c\n```\n**x** <y>\n```") == "a &lt; b &amp; c\n```\n**x** <y>\n```"
    assert slack_error("Відповідь перервалася. Натисніть «Спробувати ще раз».").startswith("Відповідь перервалася.")
    long = ("слово " * 400 + "\n\n") * 3
    parts = chunks(long, 1000)
    assert all(len(p) <= 1000 for p in parts) and " ".join(" ".join(parts).split()) == " ".join(long.split())


def test_without_a_slack_section_there_is_no_slack(tmp_path):
    s = cfg(tmp_path)
    assert s.slack is None and server.App(s).slack is None


def test_a_channel_nobody_can_vouch_for_is_treated_as_shared(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        m = mention("глянь", ts="6.0")
        m["event"]["channel"] = "CNONE"
        del m["is_ext_shared_channel"]                            # Slack did not say, and cannot be asked
        assert bridge.receive(m)
        await settle(app, bridge)
        assert prompts == [] and web.of("chat_postMessage") == []
        assert "Не можу перевірити" in web.of("chat_postEphemeral")[0]["text"]
        await bridge.stop()
    asyncio.run(go())


def test_a_late_answer_is_held_when_the_channel_became_shared(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        bridge.receive(mention("розберись", ts="8.0"))
        await settle(app, bridge)
        assert len(web.of("chat_postMessage")) == 1
        cid = app.store.slack_links()[0].conversation_id
        # Moments later a finding arrives, and meanwhile a client's company joined the channel. What she knew about
        # the channel is still fresh in her memory; she asks Slack again anyway.
        web.shared["C1"] = True
        app.store.add_message(cid, "assistant", "Знайшла причину.", meta={"investigation": 1})
        await bridge.post_pending()
        assert len(web.of("chat_postMessage")) == 1                # not posted where the client would read it
        del web.shared["C1"]                                      # Slack will not say: still held
        await bridge.post_pending()
        assert len(web.of("chat_postMessage")) == 1
        web.shared["C1"] = False
        await bridge.post_pending()
        assert web.of("chat_postMessage")[-1]["text"] == "Знайшла причину."
        # A draft's card is checked the same way.
        d = app.store.add_draft(cid, "bug", "Відео", "опис")
        app.store.attach_drafts(cid, app.store.messages(cid)[-1].id)
        web.shared["C1"] = True
        await bridge.post_pending()
        assert not any(p.get("blocks") for p in web.of("chat_postMessage"))
        await bridge.stop()
    asyncio.run(go())


def test_messages_of_one_thread_are_taken_in_the_order_they_came(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        real = bridge._files

        async def slow_first(cid, files_, who):
            if files_:
                await asyncio.sleep(0.2)                          # the first message's screenshot takes a while
            return await real(cid, [], who)
        monkeypatch.setattr(bridge, "_files", slow_first)
        bridge.receive(dm("FIRST зі скріншотом", ts="1.0", files=[{"name": "a.png"}]))
        bridge.receive(dm("SECOND уточнення", ts="1.1"))
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        said = [m.text for m in app.store.messages(cid) if m.role == "user"]
        assert said == ["FIRST зі скріншотом", "SECOND уточнення"]
        everything = "\n".join(prompts)
        assert everything.index("FIRST") < everything.index("SECOND")
        await bridge.stop()
    asyncio.run(go())


def test_a_long_answer_cut_off_midway_goes_on_from_the_missing_piece(tmp_path, monkeypatch):
    async def go():
        long = "\n\n".join(f"абзац {i} " + "слово " * 500 for i in range(3))   # three pieces
        app, bridge, web, _ = make(tmp_path, monkeypatch, replies={1: long})
        web.fail_attempts = {2}
        bridge.receive(dm("розпиши"))
        await settle(app, bridge)
        assert [p["text"][:7] for p in web.of("chat_postMessage")] == ["абзац 0"]
        # The server restarts before the next pass: the next one goes on from piece two.
        app2, bridge2, web2, _ = make(tmp_path, monkeypatch)
        await bridge2.post_pending()
        assert [p["text"][:7] for p in web2.of("chat_postMessage")] == ["абзац 1", "абзац 2"]
        await bridge2.post_pending()
        assert len(web2.of("chat_postMessage")) == 2
        assert app2.store.slack_links()[0].posted_parts == 0
        await bridge.stop()
        await bridge2.stop()
    asyncio.run(go())


def test_only_ticket_creators_get_a_ticket_proposed_or_made(tmp_path, monkeypatch):
    from assistant.tools_mcp import Tools
    from assistant.youtrack import YouTrackError
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        bridge.receive(dm("зроби тікет", user="UOUT"))
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        asked = app.store.last_user_message(cid)
        # While the turn about the stranger's request runs, a ticket creator writes in the same conversation.
        app.store.add_user_message(cid, "дякую", "dana@x.com", [])
        tools = Tools(app.s.youtrack, app.store, cid, creators=app.s.ticket_creators, expert=app.s.expert,
                      turn_ids=[asked.id])
        args = {"kind": "bug", "summary": "Відео", "description": "опис", "open_questions": []}
        try:
            tools.propose(args)
            raise AssertionError("proposed for someone not allowed to ask")
        except YouTrackError as e:
            assert "dana@x.com" in str(e) and "stranger@x.com" in str(e)
        assert app.store.drafts_of(cid) == []
        # Not known who asked (no turn given): no ticket either.
        try:
            Tools(app.s.youtrack, app.store, cid, creators=app.s.ticket_creators).propose(args)
            raise AssertionError("proposed without knowing who asked")
        except YouTrackError:
            pass
        ok = app.store.add_user_message(cid, "я підтверджую, оформлюй", "serhii@x.com", [])
        tools = Tools(app.s.youtrack, app.store, cid, creators=app.s.ticket_creators, turn_ids=[ok.id])
        assert json.loads(tools.propose(args))["draft"]
        # The button: pressed by someone not on the list, nothing is made, and they are told why.
        d = app.store.drafts_of(cid)[0]
        code, body = await app.make_ticket(d, "stranger@x.com")
        assert code == 403 and app.store.draft(d.id).state == "draft" and "serhii@x.com" in body["error"]
        await bridge.stop()
    asyncio.run(go())


def test_she_calls_the_expert_and_he_gets_who_asked_what_and_where(tmp_path, monkeypatch):
    from assistant.tools_mcp import Tools
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        bridge.receive(mention("видали всі курси на проді", ts="9.0", user="UOUT"))
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        asked = app.store.last_user_message(cid)
        app.store.add_user_message(cid, "дякую", "dana@x.com", [])     # arrives while she writes
        tools = Tools(app.s.youtrack, app.store, cid, creators=app.s.ticket_creators, expert=app.s.expert,
                      turn_ids=[asked.id])
        tools.call("call_expert", {"reason": "Просять видалити всі курси на проді, це незворотно."})
        await bridge.post_pending()
        await bridge.post_pending()
        calls = [p for p in web.of("chat_postMessage") if p["channel"] == "D-UDANA"]
        assert len(calls) == 1                                    # once, however many passes
        text = calls[0]["text"]
        assert text.startswith("<@UDANA>") and "незворотно" in text and "stranger@x.com" in text
        assert "видали всі курси на проді" in text and "https://slack.example/C1/9.0" in text
        assert "дякую" not in text and "Просить: stranger@x.com" in text
        assert f"https://f.example/assistant/#{cid}" in text
        await bridge.stop()
    asyncio.run(go())


def test_the_expert_sees_a_private_dm_only_once_she_called_him_into_it(tmp_path):
    s = settings(tmp_path)
    s.allowed_users = ["dana@x.com", "serhii@x.com"]
    web = server.build(s)
    app = web.state.app
    cid = app.store.link_slack(TEAM, "D2", "", "serhii@x.com", "serhii@x.com", "Slack · Сергій")
    app.store.add_user_message(cid, "дай доступ до проду", "serhii@x.com", [])
    with TestClient(web) as c:
        dana = {"X-Assistant-User": "dana@x.com"}
        assert c.get(f"/assistant/api/conversations/{cid}", headers=dana).status_code == 404
        app.store.add_escalation(cid, "Просять доступ до проду.", "serhii@x.com", "дай доступ до проду")
        assert c.get(f"/assistant/api/conversations/{cid}", headers=dana).status_code == 200


def test_the_prompt_names_who_may_ask_for_tickets_and_whom_she_calls(tmp_path):
    s = settings(tmp_path)
    s.brain.prompt_file.write_text((Path(__file__).parent.parent / "assistant" / "prompt.md").read_text())
    app = server.App(s)
    text = app.s.brain.prompt_file.read_text()
    assert "dana@x.com, serhii@x.com" in text and "call_expert" in text and "{expert}" not in text


def test_each_turn_tells_its_tools_exactly_which_messages_it_answers(tmp_path, monkeypatch):
    async def go():
        gate = asyncio.Event()
        app, bridge, web, prompts = make(tmp_path, monkeypatch, gate=gate)
        seen = []
        inner = server.run_turn

        async def spy(cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
            seen.append(mcp["env"]["ASSISTANT_TURN_MESSAGES"] if mcp else None)
            async for ev in inner(cfg, prompt, session, new_session, env, read_dirs, mcp):
                yield ev
        monkeypatch.setattr(server, "run_turn", spy)
        app.s.config_path = tmp_path / "c.yaml"
        bridge.receive(dm("перше", user="UOUT"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if prompts:
                break
        bridge.receive(dm("друге", ts="1.1"))
        bridge.receive(dm("третє", ts="1.2"))
        await asyncio.sleep(0.1)
        gate.set()
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        ids = [m.id for m in app.store.messages(cid) if m.role == "user"]
        assert seen == [str(ids[0]), f"{ids[1]},{ids[2]}"]
        await bridge.stop()
    asyncio.run(go())


def _finding_turn(tmp_path, monkeypatch, app, inv):
    """Tell a finding after a restart and return the turn ids its tools were given."""
    seen = []

    async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
        seen.append(mcp["env"]["ASSISTANT_TURN_MESSAGES"])
        yield Event("done", "знайшла")
    monkeypatch.setattr(server, "run_turn", run)
    app.store.investigation_found(inv.id, "звіт", "passed", "")

    async def go():
        from assistant.server import Turn
        await app._tell(app.store.investigation(inv.id), Turn())
    asyncio.run(go())
    return [int(x) for x in seen[0].split(",") if x]


def test_a_finding_answers_exactly_the_messages_of_the_turn_that_asked_for_it(tmp_path, monkeypatch):
    from assistant.tools_mcp import Tools
    from assistant.youtrack import YouTrackError
    s = settings(tmp_path)
    app = server.App(s)
    app.s.config_path = tmp_path / "c.yaml"
    cid = app.store.create_conversation("x").id
    q1 = app.store.add_user_message(cid, "перше питання", "dana@x.com", [])
    q2 = app.store.add_user_message(cid, "розберись, чому падає імпорт", "dana@x.com", [])  # sent during reply 1
    app.store.add_message(cid, "assistant", "відповідь на перше", meta={"upto": q1.id, "answers": [q1.id]})
    # Turn 2 answers only q2, and its tools start the investigation.
    Tools(s.youtrack, app.store, cid, engine_on=True, creators=s.ticket_creators, turn_ids=[q2.id]) \
        .investigate({"question": "Чому падає імпорт курсу з архіву?"})
    inv = app.store.investigations_in("running")[0]
    app.store.attach_investigations(cid, app.store.add_message(cid, "assistant", "Розберусь.",
                                                               meta={"upto": q2.id}).id)
    app.store.add_user_message(cid, "дякую", "stranger@x.com", [])         # someone else writes meanwhile
    app2 = server.App(s)                                                  # restart before the finding
    app2.s.config_path = tmp_path / "c.yaml"
    ids = _finding_turn(tmp_path, monkeypatch, app2, inv)
    assert ids == [q2.id]
    tools = Tools(s.youtrack, app2.store, cid, creators=s.ticket_creators, expert=s.expert, turn_ids=ids)
    assert json.loads(tools.propose({"kind": "bug", "summary": "Імпорт", "description": "опис",
                                     "open_questions": []}))["draft"]
    tools.call_expert({"reason": "Потрібен експерт з імпорту архівів."})
    esc = app2.store.escalations_new()[0]
    assert esc["requester"] == "dana@x.com" and esc["asked"] == "розберись, чому падає імпорт"


def test_a_finding_for_mixed_askers_keeps_both_and_no_ticket(tmp_path, monkeypatch):
    from assistant.tools_mcp import Tools
    from assistant.youtrack import YouTrackError
    s = settings(tmp_path)
    app = server.App(s)
    app.s.config_path = tmp_path / "c.yaml"
    cid = app.store.create_conversation("x").id
    a = app.store.add_user_message(cid, "видаліть мої дані з проду", "stranger@x.com", [])
    b = app.store.add_user_message(cid, "так, глянь", "dana@x.com", [])
    Tools(s.youtrack, app.store, cid, engine_on=True, turn_ids=[a.id, b.id]).investigate(
        {"question": "Що треба, щоб видалити дані користувача з проду?"})
    inv = app.store.investigations_in("running")[0]
    app.store.attach_investigations(cid, app.store.add_message(cid, "assistant", "Розберусь.").id)
    ids = _finding_turn(tmp_path, monkeypatch, server.App(s), inv)
    assert ids == [a.id, b.id]
    tools = Tools(s.youtrack, app.store, cid, creators=s.ticket_creators, expert=s.expert, turn_ids=ids)
    try:
        tools.propose({"kind": "bug", "summary": "x", "description": "y", "open_questions": []})
        raise AssertionError("a ticket for a turn a non-creator asked in")
    except YouTrackError as e:
        assert "stranger@x.com" in str(e)
    tools.call_expert({"reason": "Просять видалити дані з проду."})
    esc = app.store.escalations_new()[0]
    assert esc["requester"] == "stranger@x.com, dana@x.com"
    assert esc["asked"] == "видаліть мої дані з проду\nтак, глянь"


def test_called_into_a_long_thread_she_reads_all_its_pages_and_says_when_some_failed(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        pages = {None: ([{"ts": "5.0", "user": "USER2", "text": "ПЕРША сторінка"}], "p2"),
                 "p2": ([{"ts": "5.05", "user": "USER2", "text": "ДРУГА сторінка"},
                         {"ts": "5.1", "user": "UDANA", "text": f"<@{BOT}> глянь"}], None)}

        async def replies(channel, ts, limit, cursor=None):
            msgs, nxt = pages[cursor]
            return {"messages": msgs, "has_more": nxt is not None, "response_metadata": {"next_cursor": nxt or ""}}
        web.conversations_replies = replies
        bridge.receive(mention("глянь", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        assert "ПЕРША сторінка" in prompts[0] and "ДРУГА сторінка" in prompts[0]
        assert "не вдалося" not in prompts[0]

        async def broken(channel, ts, limit, cursor=None):
            if cursor:
                raise RuntimeError("network")
            return {"messages": [{"ts": "6.0", "user": "USER2", "text": "початок"}], "has_more": True,
                    "response_metadata": {"next_cursor": "p2"}}
        web.conversations_replies = broken
        bridge.receive(mention("а тут?", ts="6.5", thread="6.0"))
        await settle(app, bridge)
        assert "початок" in prompts[-1] and "частину треду прочитати не вдалося" in prompts[-1]
        await bridge.stop()
    asyncio.run(go())


def _thread_msg(text, ts, user="USER2"):
    return {"team_id": TEAM, "is_ext_shared_channel": False,
            "event": {"type": "message", "channel_type": "channel", "channel": "C1", "user": user, "text": text,
                      "ts": ts, "thread_ts": "5.0"}}


def quiet_run(monkeypatch, app, decide):
    """A Claude that, when `decide(prompt)` says so, calls stay_quiet the way the tools server does."""
    from assistant.tools_mcp import Tools
    seen = []

    async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
        e = (mcp or {}).get("env", {})
        seen.append({"prompt": prompt, "quiet_ok": e.get("ASSISTANT_QUIET_OK") == "1", "key": e.get("ASSISTANT_TURN_KEY")})
        reaction = decide(prompt)
        if reaction is not None:
            from assistant.youtrack import YouTrackError
            t = Tools(app.s.youtrack, app.store, e["ASSISTANT_CONVERSATION"], turn_key=e["ASSISTANT_TURN_KEY"],
                      quiet_ok=e.get("ASSISTANT_QUIET_OK") == "1")
            yield Event("tool", "mcp__assistant__stay_quiet")
            try:
                t.call("stay_quiet", {"reaction": reaction or None, "why": "розмова не до мене"})
                yield Event("delta", "внутрішнє міркування, не для людей")
                yield Event("done", "")
                return
            except YouTrackError:
                pass                    # as with the real tools server: the refusal goes back to her, she answers
        yield Event("delta", "відповідь")
        yield Event("done", "відповідь")
    monkeypatch.setattr(server, "run_turn", run)
    return seen


def test_in_a_shared_thread_she_answers_when_spoken_to_and_may_keep_quiet_or_just_react_otherwise(tmp_path,
                                                                                                    monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        app.s.config_path = tmp_path / "c.yaml"
        seen = quiet_run(monkeypatch, app, lambda p: "pray" if "дякую" in p else ("" if "Сергію" in p else None))
        bridge.receive(mention("що з релізом?", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        assert seen[-1]["quiet_ok"] is False                       # spoken to: words, no way to stay quiet
        assert web.of("chat_postMessage")[-1]["text"] == "відповідь"
        eyes = len(web.of("reactions_add"))
        bridge.receive(_thread_msg("Сергію, глянеш завтра?", "5.2"))
        await settle(app, bridge)
        assert seen[-1]["quiet_ok"] is True
        assert len(web.of("chat_postMessage")) == 1               # nothing posted
        assert len(web.of("reactions_add")) == eyes               # and no 👀 on a message not to her
        bridge.receive(_thread_msg("дякую, все працює", "5.3"))
        await settle(app, bridge)
        assert len(web.of("chat_postMessage")) == 1
        assert web.of("reactions_add")[-1] == {"channel": "C1", "timestamp": "5.3", "name": "pray"}
        cid = app.store.slack_links()[0].conversation_id
        quiet = [m for m in app.store.messages(cid) if m.meta.get("silent")]
        assert len(quiet) == 2 and all(m.text == "" for m in quiet) and quiet[1].meta["reaction"] == "pray"
        # Nothing to answer again: a resume after a restart runs no new turn.
        n = len(seen)
        app2, bridge2, web2, _ = make(tmp_path, monkeypatch)
        quiet_run(monkeypatch, app2, lambda p: None)
        bridge2.resume()
        await settle(app2, bridge2)
        assert len(seen) == n and web2.of("chat_postMessage") == [] and web2.of("reactions_add") == []
        await bridge.stop()
        await bridge2.stop()
    asyncio.run(go())


def test_a_batch_with_a_message_to_her_cannot_end_quietly_even_if_the_last_one_is_chatter(tmp_path, monkeypatch):
    async def go():
        gate = asyncio.Event()
        app, bridge, web, prompts = make(tmp_path, monkeypatch, gate=gate)
        app.s.config_path = tmp_path / "c.yaml"
        bridge.receive(mention("перше", ts="5.1", thread="5.0"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if prompts:
                break
        bridge.receive(mention("а ще: скільки учнів на курсі?", ts="5.2", thread="5.0"))
        bridge.receive(_thread_msg("ок, я пішов на обід", "5.3"))
        await asyncio.sleep(0.05)
        seen = quiet_run(monkeypatch, app, lambda p: "+1")
        gate.set()
        await settle(app, bridge)
        assert seen and seen[-1]["quiet_ok"] is False and "скільки учнів" in seen[-1]["prompt"]
        cid = app.store.slack_links()[0].conversation_id
        msgs = app.store.messages(cid)
        assert not any(m.meta.get("silent") for m in msgs) and not any(m.role == "system" for m in msgs)
        assert msgs[-1].role == "assistant" and msgs[-1].text == "відповідь"
        assert web.of("chat_postMessage")[-1]["text"] == "відповідь"
        await bridge.stop()
    asyncio.run(go())


def test_stay_quiet_is_refused_where_she_must_answer_and_takes_only_known_reactions(tmp_path):
    from assistant.tools_mcp import Tools
    from assistant.youtrack import YouTrackError
    app = server.App(settings(tmp_path))
    cid = app.store.create_conversation("x").id
    for t, args in ((Tools(app.s.youtrack, app.store, cid, turn_key="1", quiet_ok=False), {}),
                    (Tools(app.s.youtrack, app.store, cid, turn_key="1", quiet_ok=True), {"reaction": "skull"})):
        try:
            t.call("stay_quiet", args)
            raise AssertionError("quiet where it must not be")
        except YouTrackError:
            pass
    assert app.store.quiet(cid, "1") is None


def test_a_quiet_reaction_that_failed_is_tried_again_and_the_thread_never_gets_text(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        app.s.config_path = tmp_path / "c.yaml"
        quiet_run(monkeypatch, app, lambda p: "heart" if "супер" in p else None)
        bridge.receive(mention("привіт", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        real = web.reactions_add
        fails = {"n": 1}

        async def flaky(**kw):
            if kw.get("name") == "heart" and fails["n"]:
                fails["n"] -= 1
                raise RuntimeError("network")
            return await real(**kw)
        web.reactions_add = flaky
        bridge.receive(_thread_msg("супер, дякую всім", "5.4"))
        await settle(app, bridge)
        assert not any(r.get("name") == "heart" for r in web.of("reactions_add"))
        await bridge.post_pending()
        assert [r for r in web.of("reactions_add") if r.get("name") == "heart"] == \
            [{"channel": "C1", "timestamp": "5.4", "name": "heart"}]
        assert len(web.of("chat_postMessage")) == 1                # only the first, spoken-to answer
        await bridge.stop()
    asyncio.run(go())


def test_a_quiet_turn_reads_as_silence_in_a_rebuilt_session(tmp_path, monkeypatch):
    app = server.App(settings(tmp_path))
    cid = app.store.link_slack(TEAM, "C1", "5.0", None, "dana@x.com", "t")
    q = app.store.add_user_message(cid, "дякую", "serhii@x.com", [], meta={"addressed": False})
    app.store.add_message(cid, "assistant", "", meta={"silent": True, "reaction": "pray", "upto": q.id})
    app.store.set_session(cid, "gone")
    prompts = []

    async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
        prompts.append(prompt)
        if session:
            yield Event("error", "x", data={"num_turns": 0})
            return
        yield Event("done", "ок")
    monkeypatch.setattr(server, "run_turn", run)
    nxt = app.store.add_user_message(cid, f"<@{BOT}> а ти що думаєш?", "dana@x.com", [], meta={"addressed": True})

    async def go():
        from assistant.server import Turn
        await app._answer(cid, "dana@x.com", nxt, Turn())
    asyncio.run(go())
    assert "[асистент промовчала, поставила реакцію :pray:]" in prompts[-1]


class SlackRefusal(Exception):
    def __init__(self, error):
        super().__init__(error)
        self.response = {"error": error}


def test_a_reaction_slack_refuses_for_good_is_given_up_and_does_not_hold_up_her_next_answer(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        app.s.config_path = tmp_path / "c.yaml"
        quiet_run(monkeypatch, app, lambda p: "tada" if "релізнули" in p else None)
        bridge.receive(mention("привіт", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        real = web.reactions_add
        tries = []

        async def refuse(**kw):
            if kw.get("name") == "tada":
                tries.append(kw)
                raise SlackRefusal("missing_scope")
            return await real(**kw)
        web.reactions_add = refuse
        bridge.receive(_thread_msg("релізнули!", "5.6"))
        await settle(app, bridge)
        await bridge.post_pending()
        assert len(tries) == 1                                     # given up, not tried on every pass
        bridge.receive(mention("а що далі?", ts="5.7", thread="5.0"))
        await settle(app, bridge)
        assert [p["text"] for p in web.of("chat_postMessage")] == ["відповідь", "відповідь"]
        await bridge.stop()
    asyncio.run(go())


def test_what_she_writes_in_a_turn_that_may_end_quietly_is_not_shown_until_she_says_it(tmp_path, monkeypatch):
    async def go():
        app = server.App(settings(tmp_path))
        app.s.config_path = tmp_path / "c.yaml"
        cid = app.store.link_slack(TEAM, "C1", "5.0", None, "dana@x.com", "t")
        gate, seen_q = asyncio.Event(), asyncio.Queue()

        async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
            yield Event("delta", "Внутрішній текст тихого ходу")
            await gate.wait()
            from assistant.tools_mcp import Tools
            Tools(app.s.youtrack, app.store, cid, turn_key=mcp["env"]["ASSISTANT_TURN_KEY"], quiet_ok=True) \
                .call("stay_quiet", {})
            yield Event("done", "")
        monkeypatch.setattr(server, "run_turn", run)
        m = app.store.add_user_message(cid, "Кириле, глянеш?", "serhii@x.com", [], meta={"addressed": False})
        turn = server.Turn()
        turn.subscribers.add(seen_q)
        app.turns[cid] = turn
        task = asyncio.create_task(app._answer(cid, "serhii@x.com", m, turn))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if turn.text:
                break
        assert turn.text and turn.shown == ""                     # a late watcher's snapshot and partial
        gate.set()
        await task
        events = []
        while not seen_q.empty():
            events.append(seen_q.get_nowait())
        assert not any(e["type"] == "delta" for e in events)
        assert "Внутрішній" not in json.dumps(events, ensure_ascii=False)
        assert events[-1]["type"] == "done" and events[-1]["message"]["text"] == ""
    asyncio.run(go())


def test_a_quiet_turn_leaves_the_eyes_on_a_message_that_came_while_it_ran(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        app.s.config_path = tmp_path / "c.yaml"
        quiet_run(monkeypatch, app, lambda p: None)
        bridge.receive(mention("привіт", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        gate, gate2, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
        from assistant.tools_mcp import Tools

        async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
            e = mcp["env"]
            if e.get("ASSISTANT_QUIET_OK") == "1":
                await gate.wait()
                Tools(app.s.youtrack, app.store, e["ASSISTANT_CONVERSATION"], turn_key=e["ASSISTANT_TURN_KEY"],
                      quiet_ok=True).call("stay_quiet", {})
                yield Event("done", "")
                return
            second.set()
            await gate2.wait()
            yield Event("done", "відповідь на друге")
        monkeypatch.setattr(server, "run_turn", run)
        bridge.receive(_thread_msg("Сергію, глянеш?", "5.2"))         # a quiet turn starts on this
        for _ in range(30):
            await asyncio.sleep(0.01)
        bridge.receive(mention("а скільки модулів?", ts="5.3", thread="5.0"))   # to her, while the turn runs
        await asyncio.gather(*list(bridge.tasks))
        assert any(r["timestamp"] == "5.3" for r in web.of("reactions_add"))   # 👀 on the message to her
        gate.set()
        await asyncio.wait_for(second.wait(), 2)                   # the quiet turn is stored, the next one runs
        await bridge.post_pending()                                # the quiet turn is delivered now
        assert "5.3" not in [r["timestamp"] for r in web.of("reactions_remove")]   # still waiting: 👀 stays
        gate2.set()
        await settle(app, bridge)
        assert web.of("chat_postMessage")[-1]["text"] == "відповідь на друге"
        assert [r["timestamp"] for r in web.of("reactions_remove")].count("5.3") == 1
        await bridge.stop()
    asyncio.run(go())


def test_a_failed_turn_takes_the_eyes_off_what_it_answered_and_leaves_the_next_ones(tmp_path, monkeypatch):
    async def go():
        gate, gate2, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        calls = {"n": 0}
        prompts = []

        async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
            calls["n"] += 1
            prompts.append(prompt)
            if calls["n"] == 1:
                await gate.wait()
                yield Event("error", "Досягнуто ліміту, спробуйте пізніше.")
                return
            second.set()
            await gate2.wait()
            yield Event("done", "друга відповідь")
        monkeypatch.setattr(server, "run_turn", run)
        bridge.receive(dm("перше", ts="1.0"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if calls["n"]:
                break
        bridge.receive(dm("друге", ts="1.1"))                     # arrives while the first turn runs
        await asyncio.gather(*list(bridge.tasks))
        gate.set()
        await asyncio.wait_for(second.wait(), 2)                   # the unanswered «друге» gets its own turn
        cid = app.store.slack_links()[0].conversation_id
        error = [m for m in app.store.messages(cid) if m.role == "system"][0]
        assert error.meta["answers"] == [app.store.messages(cid)[0].id]
        await bridge.post_pending()
        assert "Досягнуто ліміту" in web.of("chat_postMessage")[0]["text"]
        removed = [r["timestamp"] for r in web.of("reactions_remove")]
        assert "1.0" in removed and "1.1" not in removed          # the second one still waits for its answer
        gate2.set()
        await settle(app, bridge)
        assert "друге" in prompts[-1]
        assert web.of("chat_postMessage")[-1]["text"] == "друга відповідь"
        assert [r["timestamp"] for r in web.of("reactions_remove")].count("1.1") == 1
        await bridge.stop()
    asyncio.run(go())


def test_she_sees_the_writers_name_beside_the_email(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        bridge.receive(mention("а як тобі тут?", ts="5.1", thread="5.0", user="USER2"))
        await settle(app, bridge)
        assert "[Сергій (serhii@x.com)," in prompts[0]
        await bridge.stop()
    asyncio.run(go())


def test_her_name_speaks_to_her_only_when_she_is_called_by_it():
    from assistant.slack import called_by_name
    p, extra = "Олександра Бондар", ("Александра", "Саша")
    for text in ("Олександро, а що з тікетом?", "Олександра, глянь будь-ласка", "олександра: є хвилинка?",
                 "Александра, посмотри", "Сашо, а ти що скажеш?", "а ти що скажеш, Саша?", "Дякую, Олександро!",
                 "Олександра — є питання", "Олександро, глянь `код`", "> цитата\nОлександро, а ти?",
                 "Олександро, що означає помилка, про яку написав <@UOTHER>?", "Олександро, глянь <@UOTHER>",
                 "&gt; <@UOTHER> писав про це\nОлександро, а ти що скажеш?", "«<@UOTHER> каже ок» Олександро, а ти?"):
        assert called_by_name(text, p, extra), text
    for text in ("А на ретро будемо кликати Олександру?", "нехай Олександра цим займається",
                 "Щось таке враження, що Тарас пише замість Олександри)))", "передам Олександрові",
                 "<@UOTHER> Олександра Коваль теж там була", "Олександрина задача", "Сашко, глянь", "Так це Петро писав",
                 "Він написав: «Олександро, глянь».", "> Олександро, глянь", "&gt; Олександро, глянь",
                 "<@UOTHER> Олександра, глянь", "<@UOTHER> Олександро, глянь", 'він каже "Олександро, глянь"',
                 "`Олександро, глянь`", "```\nОлександро, глянь\n```"):
        assert not called_by_name(text, p, extra), text
    assert not called_by_name("Олександро, глянь", "", ())


def test_in_her_thread_called_by_name_without_a_tag_she_must_answer(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        app.s.name = "Олександра Бондар"
        app.s.config_path = tmp_path / "c.yaml"
        seen = quiet_run(monkeypatch, app, lambda p: "")               # she would keep quiet if she were allowed
        bridge.receive(mention("що з релізом?", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        eyes = len(web.of("reactions_add"))
        bridge.receive(_thread_msg("Олександро, а на проді те саме?", "5.2"))
        await settle(app, bridge)
        assert seen[-1]["quiet_ok"] is False and "не тегнули" not in seen[-1]["prompt"]
        assert web.of("chat_postMessage")[-1]["text"] == "відповідь" and len(web.of("chat_postMessage")) == 2
        assert {"channel": "C1", "timestamp": "5.2", "name": "eyes"} in web.of("reactions_add")[eyes:]
        assert {"channel": "C1", "timestamp": "5.2", "name": "eyes"} in web.of("reactions_remove")
        # Her name in the third person, quoted, or with someone else tagged is not spoken to her: she may stay quiet.
        for i, text in enumerate(("на ретро кликнемо Олександру", "Він написав: «Олександро, глянь».",
                                  "&gt; Олександро, глянь", "<@UOTHER> Олександра, глянь"), start=3):
            n = len(web.of("reactions_add"))
            bridge.receive(_thread_msg(text, f"5.{i}"))
            await settle(app, bridge)
            assert seen[-1]["quiet_ok"] is True and len(web.of("chat_postMessage")) == 2, text
            assert all(r["name"] != "eyes" for r in web.of("reactions_add")[n:]), text
        # Spoken to her, a colleague only mentioned in the question (or tagged inside a quote): she must answer.
        for i, text in enumerate(("Олександро, глянь", "Олександро, що означає помилка, про яку написав <@UOTHER>?",
                                  "&gt; <@UOTHER> писав про це\nОлександро, а ти що скажеш?"), start=0):
            ts = f"6.{i}"
            bridge.receive(_thread_msg(text, ts))
            await settle(app, bridge)
            assert seen[-1]["quiet_ok"] is False and len(web.of("chat_postMessage")) == 3 + i, text
            assert {"channel": "C1", "timestamp": ts, "name": "eyes"} in web.of("reactions_add"), text
        await bridge.stop()
    asyncio.run(go())


def test_a_reply_right_after_hers_is_flagged_as_likely_about_it(tmp_path, monkeypatch):
    """«Так це Петро писав, а не я» under her «Павле, …»: she stayed quiet, sure the thread was over."""
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        app.s.config_path = tmp_path / "c.yaml"
        seen = quiet_run(monkeypatch, app, lambda p: None)
        bridge.receive(mention("що з релізом?", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        bridge.receive(_thread_msg("Так це Петро писав, а не я", "5.2"))
        await settle(app, bridge)
        assert seen[-1]["quiet_ok"] is True
        assert server.AFTER_HER in seen[-1]["prompt"] and "виправляють тебе" in seen[-1]["prompt"]
        assert "завершеним" in seen[-1]["prompt"]
        # After a turn she kept quiet, the next message is not «right after her reply».
        quiet_run(monkeypatch, app, lambda p: "")
        bridge.receive(_thread_msg("Кириле, а ти що думаєш?", "5.3"))
        await settle(app, bridge)
        seen2 = quiet_run(monkeypatch, app, lambda p: "")
        bridge.receive(_thread_msg("ну окей", "5.4"))
        await settle(app, bridge)
        assert server.AFTER_HER not in seen2[-1]["prompt"]
        await bridge.stop()
    asyncio.run(go())


def test_a_rebuilt_session_keeps_the_writers_names(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        bridge.receive(mention("а як тобі тут?", ts="5.1", thread="5.0", user="USER2"))
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        old = app.store.add_user_message(cid, "старе, без імені", "dana@x.com", [])
        assert old.meta.get("name") is None
        app.store.set_session(cid, "lost")
        with app.store._lock:
            app.store._db.execute("UPDATE conversations SET rebuild=1 WHERE id=?", (cid,))
        bridge.receive(mention("а далі?", ts="5.3", thread="5.0", user="USER2"))
        await settle(app, bridge)
        history = prompts[-1].split("\n---\n")[0]
        assert "сесію відновлено" in history and "[Сергій (serhii@x.com)]\n" in history and "а як тобі тут?" in history
        assert "[dana@x.com]\nстаре, без імені" in history
        await bridge.stop()
    asyncio.run(go())


class MembersWeb(FakeWeb):
    """Two pages of channel members; a bot, a deleted account and someone of another domain among them."""
    def __init__(self, pages):
        super().__init__()
        self.pages, self.fail_members = pages, False

    async def conversations_members(self, channel, limit, cursor=None):
        self._log("conversations_members", {"channel": channel, "cursor": cursor})
        if self.fail_members:
            raise RuntimeError("ratelimited")
        i = int(cursor or 0)
        nxt = str(i + 1) if i + 1 < len(self.pages) else ""
        return {"members": self.pages[i], "response_metadata": {"next_cursor": nxt}}


MEMBERS = {
    "URG1": {"id": "URG1", "profile": {"email": "Olga@Acme.example", "real_name": "Ольга"}},
    "URG2": {"id": "URG2", "profile": {"email": "dmytro@acme.example", "real_name": "Дмитро"}},
    "UCW": {"id": "UCW", "profile": {"email": "max@partner.example", "real_name": "Max"}},
    "UBOT2": {"id": "UBOT2", "is_bot": True, "profile": {"email": "bot@acme.example"}},
    "UGONE": {"id": "UGONE", "deleted": True, "profile": {"email": "gone@acme.example"}},
}


def test_channel_members_of_the_domain_may_ask_for_tickets_and_leavers_lose_it(tmp_path, monkeypatch):
    from assistant.tools_mcp import Tools
    from assistant.youtrack import YouTrackError
    monkeypatch.setitem(PEOPLE, "URG1", MEMBERS["URG1"])
    for k, v in MEMBERS.items():
        monkeypatch.setitem(PEOPLE, k, v)

    async def go():
        app, bridge, _, _ = make(tmp_path, monkeypatch)
        app.s.ticket_creators = ["dana@x.com"]
        app.s.slack.creator_channels, app.s.slack.creator_domain = ("CINT",), "acme.example"
        web = MembersWeb([["URG1", "UCW", "UBOT2"], ["URG2", "UGONE"]])
        bridge.web = web
        assert await bridge.refresh_creators() == {"olga@acme.example", "dmytro@acme.example"}
        assert app.s.creators(app.store) == ["dana@x.com", "dmytro@acme.example", "olga@acme.example"]
        # In words, not as a list of e-mails; and who runs the project stays the configured list.
        assert "учасники каналу проєкту з поштою @acme.example" in app.s.creators_label()
        cid = app.store.create_conversation("olga@acme.example", "t").id
        asked = app.store.add_user_message(cid, "оформи тікет", "olga@acme.example", [])
        tools = Tools(app.s.youtrack, app.store, cid, creators=app.s.creators(app.store),
                      turn_ids=[asked.id], creators_label=app.s.creators_label(), leads=app.s.ticket_creators)
        args = {"kind": "bug", "summary": "Відео", "description": "опис", "open_questions": []}
        assert json.loads(tools.propose(args))["draft"]
        assert tools.leads == ["dana@x.com"]
        d = app.store.drafts_of(cid)[0]
        code, body = await app.make_ticket(d, "max@partner.example")
        assert code == 403 and "учасники каналу проєкту" in body["error"]
        # A failed read keeps the list as it was; a full read without Olga takes her right away.
        web.fail_members = True
        try:
            await bridge.refresh_creators()
            raise AssertionError("a failed read passed")
        except RuntimeError:
            pass
        assert "olga@acme.example" in app.store.slack_creators()
        web.fail_members, web.pages = False, [["URG2"]]
        await bridge.refresh_creators()
        assert app.store.slack_creators() == {"dmytro@acme.example"}
        try:
            Tools(app.s.youtrack, app.store, cid, creators=app.s.creators(app.store), turn_ids=[asked.id],
                  creators_label=app.s.creators_label()).propose(args)
            raise AssertionError("proposed for someone who left the channel")
        except YouTrackError as e:
            assert "учасники каналу проєкту" in str(e)
        await bridge.stop()
    asyncio.run(go())


def test_channel_creators_configured_but_not_read_yet_is_nobody_not_anyone(tmp_path):
    s = settings(tmp_path)
    s.ticket_creators = []
    s.slack.creator_channels, s.slack.creator_domain = ("CINT",), "acme.example"
    app = server.App(s)
    assert app.s.creators(app.store) == [server.NOBODY]
    s.brain.prompt_file.write_text((Path(__file__).parent.parent / "assistant" / "prompt.md").read_text())
    text = server.render_prompt(s)
    assert "будь-кого" not in text and "учасники каналу проєкту з поштою @acme.example" in text
