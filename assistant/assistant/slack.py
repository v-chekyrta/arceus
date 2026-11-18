"""The assistant in Slack: the same conversations, answers, files and ticket drafts as the web chat.

Socket Mode: the server opens the connection to Slack, so nothing listens on the internet. Slack wants every event
acknowledged within 3 seconds and resends it otherwise, so a message is written to `slack_inbox` first, then
acknowledged, and only then taken: who wrote it, its files, the conversation. A restart in between finishes taking it
on the next start; the same message twice (a resend, or a mention that also arrives as a channel message) is taken
once.

Where she answers:
- a direct message: the whole DM is one conversation, and only that person sees it in the web chat;
- an @mention in a channel: the thread under it is one conversation, and she answers there, also when people write in
  that thread later without mentioning her;
- never in a channel shared with another company (Slack Connect): the client would read her answers there. What Slack
  says about the channel with the event decides; when it says nothing, she asks (`conversations.info`, which needs
  channels:read / groups:read), and when that is not known either she does not answer there. A late post (a finding,
  a draft) is checked again before it goes into a channel, and held while the channel is not known to be ours.

Two kinds of channel the owner names by id, whatever Slack says about who is in them:
- `listen_channels`: she reads them into her log and their history like a project channel, and never says anything
  there: no answer to a mention, no reaction, no note (a channel with the client, where only people speak);
- `partner_channels`: shared with another company on purpose; she reads them and answers there, and people from the
  other company may talk to her there (tickets stay for `ticket_creators`).
What she reads goes back `history_days` (120 unless set); raising it reads the older part the next time she starts.

Who may talk to her: anyone who is a full member of the workspace she is installed in, never guests or people from
other companies (they get a short note only they can see). Tickets are another matter: only `ticket_creators` may
have one proposed or press its button. When she calls the expert in (`call_expert`), he gets a direct message from her
with who asked, what, and why it is his call.

Her messages reach Slack through one loop that posts what the conversation has and Slack does not yet
(`slack_links.posted_upto`), so an answer, a finding told later and a ticket draft's card all arrive the same way, and
a failed post is posted on the next pass. Messages sent while she is answering wait and are answered together.
Messages of one DM or thread are taken one after another in the order they came (a slow download does not let a
later message overtake it), and a long answer posted in pieces records each piece, so a retry never repeats one.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from assistant import files, secrets
from assistant.store import Attachment, Draft

if TYPE_CHECKING:
    from assistant.server import App

log = logging.getLogger("assistant.slack")

EYES = "eyes"
CHUNK = 3500                    # Slack cuts a message's text at 40 000, but blocks of text read best well under 4 000
THREAD_CONTEXT_CHARS = 6000
THREAD_PAGES = 10               # pages of a thread she reads when called into it
PERSON_TTL_S = 600
CHANNEL_TTL_S = 600
BACKFILL_DAYS = 120             # how far back she reads a project channel, unless slack.history_days says otherwise
BACKFILL_PAGES = 1000           # a guard only: pages of 200 messages read in one go             # how long she trusts what she knows about who is in a channel
POST_EVERY_S = 2.0
CREATORS_EVERY_S = 3600         # how often who may ask for tickets is read anew from slack.creator_channels
# Slack errors that may pass by themselves.
TRANSIENT = {"ratelimited", "rate_limited", "internal_error", "fatal_error", "service_unavailable", "request_timeout",
             "team_added_to_org"}
# A post that can never succeed: skipped, so it does not hold up everything after it.
PERMANENT = {"channel_not_found", "not_in_channel", "is_archived", "msg_too_long", "no_text", "restricted_action",
             "cannot_reply_to_message", "invalid_blocks"}


@dataclass
class SlackConfig:
    secrets: Path                # assistant.env: SLACK_APP_TOKEN and SLACK_BOT_TOKEN (assistant/secrets.py)
    web_url: str = ""            # the web chat, for «повний текст» links under a draft
    read_channels: tuple[str, ...] = ()   # the project's channels she reads (ids); she answers in any she is called to
    # Channels the owner confirmed hold people of this company only: used when Slack does not say (no channels:read /
    # groups:read). Whatever Slack does say wins: once it says a channel is shared, she stays out of it.
    internal_channels: tuple[str, ...] = ()
    # Other names people call her by, beside the first word of persona_name (e.g. Александра, Саша): «Сашо, …» or
    # «Александра, …» at the start speaks to her as plainly as an @mention does.
    call_names: tuple[str, ...] = ()
    # Channels the owner names whatever Slack says about who is in them (see the module's docstring): read, never
    # spoken in; and shared with a partner on purpose, read and answered in.
    listen_channels: tuple[str, ...] = ()
    partner_channels: tuple[str, ...] = ()
    history_days: int = BACKFILL_DAYS   # how far back the channels she reads are read and searched
    # Who may ask for tickets, beside ticket_creators: the members of these channels whose Slack e-mail is of this
    # domain, read anew every hour (store.slack_creators). Both or neither.
    creator_channels: tuple[str, ...] = ()
    creator_domain: str = ""

    @property
    def approved(self) -> tuple[str, ...]:
        return (*self.listen_channels, *self.partner_channels)

    def tokens(self) -> tuple[str, str]:
        """(app token, bot token). Missing ones are said by name, never by value."""
        values = secrets.read(self.secrets)
        missing = [n for n in ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN") if not values.get(n)]
        if missing:
            raise secrets.SecretsError(f"{', '.join(missing)} not set in {self.secrets.name}")
        return values["SLACK_APP_TOKEN"], values["SLACK_BOT_TOKEN"]


@dataclass
class Person:
    email: str | None
    name: str
    refused: str | None          # why she does not talk to them; None: she does


class SlackBridge:
    def __init__(self, app: "App", cfg: SlackConfig, web: Any = None, socket: Any = None):
        self.app, self.cfg = app, cfg
        self.web, self.socket = web, socket      # given by tests; made from the tokens in start() otherwise
        self.team = ""
        self.bot_user = ""
        self.people: dict[str, tuple[float, Person]] = {}
        self.channels: dict[str, tuple[float, bool]] = {}   # channel → (when, shared with another company)
        self.queues: dict[tuple[str, str, str], asyncio.Lock] = {}
        self.backfilled: set[str] = set()                    # project channels whose history was read this run
        self.kick = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self._poster: asyncio.Task | None = None
        self._connector: asyncio.Task | None = None
        self._creators: asyncio.Task | None = None
        self._bot_token = ""

    # lifecycle ---------------------------------------------------------------------------------------------------
    async def start(self) -> None:
        """Never fails the web chat: without Slack (bad token, no network) it keeps trying in the background."""
        self.app.after_turn.append(self._after_turn)
        self._connector = asyncio.create_task(self._connect_loop())

    async def _connect_loop(self) -> None:
        delay = 5
        while True:
            try:
                await self._connect()
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - logged; the web chat goes on without Slack
                log.error("Slack is not connected (%s: %s); next try in %ss", type(e).__name__, e, delay)
                status = getattr(self.app, "status", None)
                if status is not None:
                    # Its own words for a missing key; Slack's error code (invalid_auth…) for the rest.
                    status.slack("failed", error=str(e) if isinstance(e, secrets.SecretsError) else
                                 f"{type(e).__name__}: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def _connect(self) -> None:
        if self.web is None:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
            from slack_sdk.web.async_client import AsyncWebClient
            app_token, self._bot_token = self.cfg.tokens()
            self.web = AsyncWebClient(token=self._bot_token)
            self.socket = SocketModeClient(app_token=app_token, web_client=self.web)
        who = await self.web.auth_test()
        self.team, self.bot_user = who["team_id"], who["user_id"]
        self.socket.socket_mode_request_listeners.append(self._on_request)
        await self.socket.connect()
        log.info("Slack connected: team %s as %s", who.get("team"), who.get("user"))
        self._names = (str(who.get("team") or ""), str(who.get("user") or ""))
        status = getattr(self.app, "status", None)
        if status is not None:
            status.slack("connected", team=self._names[0], bot=self._names[1])
        self._poster = asyncio.create_task(self._post_loop())
        if self.cfg.creator_channels and self.cfg.creator_domain:
            self._creators = asyncio.create_task(self._creators_loop())
        self.resume()

    async def watch(self) -> None:
        """Whether the socket is still up, for the console: Slack's client reconnects by itself after a drop, and the
        status says «reconnecting» meanwhile and «connected» once it is back. Before the first connection the connect
        loop says how that goes."""
        status = getattr(self.app, "status", None)
        if status is None or self.socket is None or not self.team:
            return
        try:
            up = self.socket.is_connected()
            if asyncio.iscoroutine(up):
                up = await up
        except Exception:  # noqa: BLE001 - a client that cannot say is not a connection
            up = False
        state = "connected" if up else "reconnecting"
        if (status.data.get("slack") or {}).get("state") != state:
            log.warning("Slack connection %s", "is back" if up else "lost: Slack's client is reconnecting")
            team, bot = getattr(self, "_names", ("", ""))
            status.slack(state, team=team, bot=bot,
                         error="" if up else "the connection to Slack dropped; reconnecting")

    def resume(self) -> None:
        """What a restart left half-done: messages taken but not stored, and stored but not answered. Also the
        project channels' history since she last read them, once Slack confirms they are ours only."""
        for channel in self.cfg.read_channels:
            self._spawn(self._backfill_if_ours(channel))
        for row in self.app.store.slack_received():
            self._spawn(self._take(row["event"]))
        for link in self.app.store.slack_links():
            self.pump(link.conversation_id)

    async def stop(self) -> None:
        for t in (self._connector, self._poster, self._creators, *self.tasks):
            if t:
                t.cancel()
        if self.socket is not None:
            try:
                await self.socket.close()
            except Exception:  # noqa: BLE001 - shutting down
                pass

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    # events in ---------------------------------------------------------------------------------------------------
    async def _on_request(self, client, req) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse
        try:
            if req.type == "events_api":
                self.receive(req.payload)
            elif req.type == "interactive":
                self._spawn(self._interact(req.payload))
        finally:
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    def receive(self, payload: dict) -> bool:
        """Write a message she should take to the inbox and start taking it. Runs before Slack hears it arrived, so
        it only looks at the event itself: no calls to Slack here."""
        ev = payload.get("event") or {}
        if ev.get("channel") in self.cfg.read_channels:
            self.read(payload, ev)
        key = self.admit(ev, payload.get("team_id") or ev.get("team") or self.team)
        if key is None:
            return False
        ext = payload.get("is_ext_shared_channel")         # absent: not known, and then she does not answer there
        item = {"event": ev, "team": key[0], "ext": ext if isinstance(ext, bool) else None}
        if not self.app.store.slack_take(*key, item):
            return False
        self._spawn(self._take(item))
        return True

    # reading the project's channels ------------------------------------------------------------------------------
    def read(self, payload: dict, ev: dict) -> None:
        """Every message of a project channel goes into her log, written before Slack hears it arrived: a new one, an
        edit, a deletion (a tombstone: older pages of history never bring it back, and notes she made from it go).
        Only while Slack says the channel is ours alone: shared with another company, what she read there goes;
        not said, nothing is kept."""
        store, channel = self.app.store, ev["channel"]
        team = payload.get("team_id") or ev.get("team") or self.team
        ext = payload.get("is_ext_shared_channel")
        if channel in self.cfg.approved:
            self._approve(channel)          # the owner named it: who else is in it does not close it
        elif ext is True:
            self._status(channel, True)
            return
        elif ext is None and self._unknown(channel) is not True:
            return
        elif ext is False:
            self._status(channel, False)
        sub = ev.get("subtype")
        if sub == "message_changed":
            m = ev.get("message") or {}
            if m.get("ts"):
                text = html.unescape(m.get("text") or "")
                store.edit_logged(team, channel, m["ts"], text)
                store.forget_notes_from(f"slack:{channel}:{m['ts']}", "повідомлення змінили в Slack")
                self._spawn(self._tidy(team, channel, m["ts"], m.get("user"), m.get("text") or "", text))
        elif sub == "message_deleted":
            ts = ev.get("deleted_ts") or (ev.get("previous_message") or {}).get("ts")
            if ts:
                store.delete_logged(team, channel, ts)
                store.forget_notes_from(f"slack:{channel}:{ts}", "повідомлення видалили в Slack")
        elif sub in (None, "file_share", "thread_broadcast", "bot_message") and ev.get("ts"):
            self._log(team, channel, ev)
        if channel not in self.backfilled:
            self.backfilled.add(channel)
            self._spawn(self._backfill(team, channel))

    def _log(self, team: str, channel: str, m: dict) -> None:
        text = html.unescape(m.get("text") or "")
        names = [f.get("name") for f in m.get("files") or [] if f.get("name")]
        if names:
            text = (text + "\n[файли: " + ", ".join(names) + "]").strip()
        if not text:
            return
        self.app.store.log_message(team, channel, m["ts"], m.get("thread_ts"), m.get("user") or m.get("bot_id"), text)
        self._spawn(self._tidy(team, channel, m["ts"], m.get("user"), m.get("text") or "", text))

    async def _tidy(self, team: str, channel: str, ts: str, user: str | None, raw: str, stored: str) -> None:
        """Names instead of Slack ids: the author's, and the people mentioned in the text."""
        try:
            if user == self.bot_user:
                self.app.store.name_logged(user, self.app.s.name)
            elif user:
                self.app.store.name_logged(user, (await self.person(user)).name)
            if "<" in raw:
                text = await self.plain(raw.replace(f"<@{self.bot_user}>", f"@{self.app.s.name}"))
                self.app.store.retext_logged(team, channel, ts, raw_text=stored,
                                             text=text + stored[len(html.unescape(raw)):])
        except Exception as e:  # noqa: BLE001 - the raw text stays
            log.warning("tidying %s/%s failed: %s", channel, ts, e)

    async def _backfill_if_ours(self, channel: str) -> None:
        if channel in self.backfilled:
            return
        if channel in self.cfg.approved:
            self._approve(channel)
        if channel in self.cfg.approved or await self.internal(channel, fresh=True) is True:
            self.backfilled.add(channel)
            await self._backfill(self.team, channel)
        else:
            log.warning("not reading %s's history: not known to be a channel of this company only", channel)

    async def _call(self, method: str, **kw):
        """A Slack call that waits out rate limits (429, Retry-After) instead of losing the page."""
        for _ in range(6):
            try:
                return await getattr(self.web, method)(**kw)
            except Exception as e:  # noqa: BLE001
                resp = getattr(e, "response", None)
                if resp is not None and getattr(resp, "status_code", None) == 429:
                    await asyncio.sleep(float((resp.headers or {}).get("Retry-After", 5)))
                    continue
                raise
        raise RuntimeError(f"{method}: rate limited")

    async def _backfill(self, team: str, channel: str) -> None:
        """Windows until the log reaches history_days back: an unfinished older window, once done, may still leave
        the part before it (history_days raised meanwhile)."""
        for _ in range(3):
            if not await self._backfill_window(team, channel):
                return

    async def _backfill_window(self, team: str, channel: str) -> bool:
        """The channel's history, page by page, with the threads in it. A run reads a fixed window [oldest, latest]
        and records its cursor after every page (channel_backfill), apart from the live messages: a run cut short
        goes on from the page it stopped at, and a live message never moves the window. A finished run's next one
        reads from where it ended up to now. A deleted message stays deleted (log_message keeps tombstones)."""
        store = self.app.store
        state = store.backfill_state(channel)
        now = f"{time.time():.6f}"
        wanted = f"{time.time() - self.cfg.history_days * 86400:.6f}"
        if state is None:
            store.backfill_start(channel, wanted, now, covered_from=wanted)
        elif state["done"]:
            covered = state.get("covered_from")
            if covered is None or float(covered) > float(wanted) + 86400:
                # Read less far back than wanted now (history_days was raised, or a database from before it was
                # kept): the whole window again; what is already in the log stays as it is.
                store.backfill_start(channel, wanted, now, covered_from=wanted)
            else:
                store.backfill_start(channel, state["latest"], now)
        state = store.backfill_state(channel)
        cursor, pages = state["cursor"], 0
        try:
            while pages < BACKFILL_PAGES:
                try:
                    got = await self._call("conversations_history", channel=channel, oldest=state["oldest"],
                                           latest=state["latest"], inclusive=True, limit=200,
                                           **({"cursor": cursor} if cursor else {}))
                except Exception as e:  # noqa: BLE001
                    if cursor and _error_of(e) == "invalid_cursor":   # a stale cursor: the window again, from page 1
                        store.backfill_start(channel, state["oldest"], state["latest"])
                        cursor = None
                        continue
                    raise
                pages += 1
                for m in got.get("messages") or []:
                    if m.get("subtype") not in (None, "file_share", "thread_broadcast", "bot_message"):
                        continue
                    self._log(team, channel, m)
                    if m.get("reply_count"):
                        await self._backfill_thread(team, channel, m["ts"])
                cursor = (got.get("response_metadata") or {}).get("next_cursor") if got.get("has_more") else None
                store.backfill_page(channel, cursor or None)
                if not cursor:
                    break
            log.info("read %s: %d page(s) of history%s", channel, pages, "" if not cursor else ", more next time")
            covered = (store.backfill_state(channel) or {}).get("covered_from")
            wanted = time.time() - self.cfg.history_days * 86400
            return not cursor and (covered is None or float(covered) > wanted + 86400)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - what was read stays; the next start goes on from the newest
            self.backfilled.discard(channel)
            log.error("reading %s's history stopped: %s", channel, _error_of(e))
            return False

    async def _backfill_thread(self, team: str, channel: str, thread: str) -> None:
        cursor = None
        while True:
            got = await self._call("conversations_replies", channel=channel, ts=thread, limit=200,
                                   **({"cursor": cursor} if cursor else {}))
            for m in got.get("messages") or []:
                if m.get("ts") != thread and m.get("subtype") in (None, "file_share", "thread_broadcast", "bot_message"):
                    self._log(team, channel, {**m, "thread_ts": thread})
            cursor = (got.get("response_metadata") or {}).get("next_cursor")
            if not got.get("has_more") or not cursor:
                return

    def admit(self, ev: dict, team: str) -> tuple[str, str, str] | None:
        """(team, channel, ts) of a message meant for her, or None."""
        if ev.get("type") not in ("message", "app_mention"):
            return None
        if ev.get("subtype") not in (None, "file_share", "thread_broadcast") or ev.get("bot_id"):
            return None
        user, channel, ts = ev.get("user"), ev.get("channel") or "", ev.get("ts") or ""
        if not user or user == self.bot_user or not channel or not ts:
            return None
        if ev.get("channel_type") == "im" or channel.startswith("D"):
            return team, channel, ts
        if ev.get("channel_type") == "mpim":
            return None
        if channel in self.cfg.listen_channels:
            return None                                  # read only: nothing she writes ever goes there
        if ev["type"] == "app_mention" or f"<@{self.bot_user}>" in (ev.get("text") or ""):
            return team, channel, ts
        thread = ev.get("thread_ts")
        if thread and self.app.store.slack_conversation(team, channel, thread):
            return team, channel, ts                     # a thread she is already answering in
        return None

    async def person(self, user: str) -> Person:
        hit = self.people.get(user)
        if hit and time.time() - hit[0] < PERSON_TTL_S:
            return hit[1]
        info = (await self.web.users_info(user=user))["user"]
        prof = info.get("profile") or {}
        email = (prof.get("email") or "").lower() or None
        name = prof.get("real_name") or prof.get("display_name") or info.get("name") or user
        refused = None
        if info.get("deleted") or info.get("is_bot"):
            refused = "bot"
        elif info.get("is_restricted") or info.get("is_ultra_restricted") or info.get("team_id") != self.team \
                or not email:
            refused = "guest"
        p = Person(email, name, refused)
        self.people[user] = (time.time(), p)
        return p

    @staticmethod
    def _is_dm(ev: dict) -> bool:
        return ev.get("channel_type") == "im" or (ev.get("channel") or "").startswith("D")

    def _queue(self, team: str, ev: dict) -> asyncio.Lock:
        """One queue per DM or thread: its messages are taken one after another, in the order they came, however
        long one of them takes to download (asyncio's lock is first come, first served)."""
        key = (team, ev["channel"], "" if self._is_dm(ev) else (ev.get("thread_ts") or ev["ts"]))
        return self.queues.setdefault(key, asyncio.Lock())

    async def internal(self, channel: str, hint: bool | None = None, fresh: bool = False) -> bool | None:
        """True: only this company is in the channel. False: another company is (Slack Connect). None: not known —
        and then she treats it as not hers to answer in. A DM is checked by its person instead.
        `hint`: what Slack said with the event just received. `fresh`: ask Slack now, whatever was known before
        (before anything of hers is posted into a channel: the channel may have been shared since)."""
        if channel.startswith("D"):
            return True
        if channel in self.cfg.approved:
            self._approve(channel)
            return channel in self.cfg.partner_channels   # a listen channel is never hers to speak in
        if hint is not None:
            self._status(channel, hint)
            return not hint
        hit = self.channels.get(channel)
        if hit and not fresh and time.time() - hit[0] < CHANNEL_TTL_S:
            return not hit[1]
        try:
            ch = (await self.web.conversations_info(channel=channel))["channel"]
        except Exception as e:  # noqa: BLE001 - without channels:read / groups:read it is not known
            log.warning("cannot tell who is in %s: %s", channel, _error_of(e))
            return self._unknown(channel)
        if "is_ext_shared" not in ch:
            return self._unknown(channel)
        shared = bool(ch.get("is_ext_shared") or ch.get("is_pending_ext_shared"))
        self._status(channel, shared)
        return not shared

    def _unknown(self, channel: str) -> bool | None:
        """Slack would not say. A channel the owner confirmed as ours counts as ours — unless Slack has said it is
        shared: then the confirmation is void until Slack says otherwise."""
        if self.app.store.channel_shared(channel) is True:
            return False                    # Slack said it is shared: so it stays until Slack says otherwise
        if channel in self.cfg.internal_channels:
            self._status(channel, False)
            return True
        self._status(channel, None)
        return None

    def silent(self, channel: str) -> bool:
        """A listen channel: nothing of hers ever goes there. Checked by every method that posts, reacts, notes or
        updates in a channel, so no path (a replayed event, an old button, a late card) gets around it."""
        if channel in self.cfg.listen_channels:
            log.warning("not writing to %s: a channel she only listens to", channel)
            return True
        return False

    def _approve(self, channel: str) -> None:
        """A channel the owner named (listen or partner): what she reads there stays usable whoever is in it."""
        if self.app.store.channel_shared(channel) is not False:
            self.app.store.set_channel_status(channel, False)
        self.channels[channel] = (time.time(), False)

    def _status(self, channel: str, shared: bool | None) -> None:
        """What Slack just said about a channel, for this process and for her tools and notes (channel_status).
        Not known: what she read there and noted from it is out of use until it is known again. Shared with another
        company: it is gone for good."""
        if shared is None:
            self.channels.pop(channel, None)
        else:
            self.channels[channel] = (time.time(), shared)
        self.app.store.set_channel_status(channel, shared)
        if shared:
            self.app.store.purge_channel(channel)

    async def _take(self, item: dict) -> None:
        async with self._queue(item["team"], item["event"]):
            await self._take_now(item)

    async def _take_now(self, item: dict) -> None:
        ev, team = item["event"], item["team"]
        channel, ts = ev["channel"], ev["ts"]
        store = self.app.store
        try:
            if channel in self.cfg.listen_channels:      # an event stored before the channel was named so
                store.slack_settle(team, channel, ts, "refused")
                return
            ours = await self.internal(channel, item.get("ext"))
            if ours is not True:
                await self._note(channel, ev, (
                    "У каналах, спільних з іншими компаніями, я не відповідаю: мої відповіді тут бачили б і клієнти."
                    if ours is False else "Не можу перевірити, чи в цьому каналі немає людей з інших компаній, тож "
                                          "тут не відповідаю.") + " Напишіть мені в особисті.")
                store.slack_settle(team, channel, ts, "refused")
                return
            p = await self.person(ev["user"])
            if p.refused == "guest" and channel in self.cfg.partner_channels:
                # The partner's people may talk to her there. Never under an e-mail or a name that could pass for
                # someone of the team (tickets are checked against e-mails): their Slack id goes with them.
                p = Person(None, f"{p.name} (інша компанія, {ev['user']})", None)
            if p.refused:
                if p.refused != "bot":
                    await self._note(channel, ev, "Я відповідаю лише людям нашої команди. Гостям і людям з "
                                                  "інших компаній, на жаль, не можу.")
                store.slack_settle(team, channel, ts, "refused")
                return
            dm = self._is_dm(ev)
            thread = "" if dm else (ev.get("thread_ts") or ts)
            text = await self.plain(ev.get("text") or "")
            new = store.slack_conversation(team, channel, thread) is None
            title = f"Slack · {p.name}" if dm else "Slack · " + _title(text or p.name)
            cid = store.link_slack(team, channel, thread, p.email if dm else None, p.email or p.name, title)
            # Spoken to: a DM, or her name in it. In a shared thread people also talk among themselves; 👀 goes only
            # where she is spoken to, and only then must the turn end in words (see server QUIET_NOTE).
            raw = ev.get("text") or ""
            addressed = dm or ev.get("type") == "app_mention" or f"<@{self.bot_user}>" in raw or \
                called_by_name(raw, self.app.s.name, self.cfg.call_names)
            if addressed:
                await self._react(channel, ts, True, team)
            if new and not dm and thread != ts:
                text = await self._thread_before(channel, thread, ts) + text
            ids, notes = await self._files(cid, ev.get("files") or [], p.email or p.name)
            if notes:
                text = (text + "\n\n" + "\n".join(notes)).strip()
            if not text and not ids:
                text = "(лише згадка, без тексту)"
            msg = store.add_user_message(cid, text, p.email or p.name, ids, meta={"addressed": addressed, "name": p.name})
            store.slack_settle(team, channel, ts, "stored", cid, msg.id)
            self.pump(cid)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the person must hear that it did not get through
            log.exception("taking Slack message %s/%s failed", channel, ts)
            store.slack_settle(team, channel, ts, "refused")
            await self._react(channel, ts, False, team)
            await self._note(channel, ev, f"Не вдалося прийняти це повідомлення ({type(e).__name__}). "
                                          "Напишіть, будь ласка, ще раз.")

    async def _note(self, channel: str, ev: dict, text: str) -> None:
        """A note only the author sees."""
        if self.silent(channel):
            return
        try:
            await self.web.chat_postEphemeral(channel=channel, user=ev["user"], text=text,
                                              thread_ts=ev.get("thread_ts"))
        except Exception as e:  # noqa: BLE001
            log.warning("ephemeral note failed: %s", e)

    async def _react(self, channel: str, ts: str, on: bool, team: str) -> None:
        if self.silent(channel):
            return
        try:
            if on:
                await self.web.reactions_add(channel=channel, timestamp=ts, name=EYES)
            else:
                await self.web.reactions_remove(channel=channel, timestamp=ts, name=EYES)
        except Exception as e:  # noqa: BLE001 - 👀 is a courtesy; the answer comes either way
            if "already_reacted" not in str(e) and "no_reaction" not in str(e):
                log.warning("reaction %s failed: %s", "add" if on else "remove", e)
        self.app.store.slack_reacted(team, channel, ts, on)

    async def _thread_before(self, channel: str, thread: str, ts: str) -> str:
        """She was called into a thread already going: what was said in it before, so she knows what it is about.
        Every page of it (rate limits waited out); if some of it could not be read, she is told so, not left to
        think she saw the whole thread."""
        messages, cursor, incomplete = [], None, False
        try:
            for _ in range(THREAD_PAGES):
                got = await self._call("conversations_replies", channel=channel, ts=thread, limit=200,
                                       **({"cursor": cursor} if cursor else {}))
                messages += got.get("messages") or []
                cursor = (got.get("response_metadata") or {}).get("next_cursor") if got.get("has_more") else None
                if not cursor or any(m.get("ts") == ts for m in messages):
                    break
            else:
                incomplete = bool(cursor)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - what was read is still worth having
            log.warning("reading thread %s failed: %s", thread, _error_of(e))
            incomplete = True
        lines = []
        for m in messages:
            if m.get("ts") == ts:
                break
            who = "асистент" if m.get("user") == self.bot_user else (await self._name(m.get("user")) or "хтось")
            lines.append(f"{who}: {await self.plain(m.get('text') or '')}")
        body = "\n".join(lines)
        if len(body) > THREAD_CONTEXT_CHARS:
            body = "(початок треду пропущено)\n" + body[-THREAD_CONTEXT_CHARS:]
        if incomplete:
            body = (body + "\n" if body else "") + "(частину треду прочитати не вдалося: якщо бракує контексту, спитай)"
        return f"[Що писали в цьому треді Slack до мене]\n{body}\n[Кінець треду]\n\n" if body else ""

    async def _name(self, user: str | None) -> str | None:
        if not user:
            return None
        try:
            return (await self.person(user)).name
        except Exception:  # noqa: BLE001
            return None

    async def plain(self, text: str) -> str:
        """Slack's markup as plain text: mentions as names, links as their address."""
        text = text.replace(f"<@{self.bot_user}>", "").strip()
        for uid in set(re.findall(r"<@([A-Z0-9]+)(?:\|[^>]*)?>", text)):
            name = await self._name(uid) or uid
            text = re.sub(rf"<@{uid}(?:\|[^>]*)?>", "@" + name, text)
        text = re.sub(r"<#[A-Z0-9]+\|([^>]*)>", r"#\1", text)
        text = re.sub(r"<!(here|channel|everyone)>", r"@\1", text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
        text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
        text = re.sub(r"<mailto:[^|>]+\|([^>]+)>", r"\1", text)
        return html.unescape(text)

    async def _files(self, cid: str, slack_files: list[dict], who: str) -> tuple[list[str], list[str]]:
        """Download the message's files into the conversation's folder, the same way the web chat stores them."""
        import aiohttp
        ids, notes = [], []
        token = self._bot_token or getattr(self.web, "token", "")
        for f in slack_files[:files.MAX_FILES_PER_MESSAGE]:
            name = files.safe_name(f.get("name") or f.get("title") or "файл")
            url = f.get("url_private_download") or f.get("url_private")
            if f.get("mode") in ("tombstone", "hidden_by_limit") or not url:
                notes.append(f"(файл «{name}» Slack не віддав)")
                continue
            if (f.get("size") or 0) > files.MAX_FILE_BYTES:
                notes.append(f"(файл «{name}» більший за {files.MAX_FILE_BYTES // (1024 * 1024)} МБ, його я не беру)")
                continue
            aid = uuid.uuid4().hex
            folder = self.app.uploads / cid / aid
            folder.mkdir(parents=True)
            path = folder / name
            try:
                async with aiohttp.ClientSession() as http, http.get(
                        url, headers={"Authorization": f"Bearer {token}"}) as r:
                    if r.status != 200 or r.content_type == "text/html":
                        raise OSError(f"HTTP {r.status} {r.content_type}")
                    size = 0
                    with path.open("wb") as out:
                        async for chunk in r.content.iter_chunked(1 << 16):
                            size += len(chunk)
                            if size > files.MAX_FILE_BYTES:
                                raise OverflowError
                            out.write(chunk)
            except (OSError, OverflowError, aiohttp.ClientError) as e:
                log.warning("file %s from Slack failed: %s", name, e)
                notes.append(f"(файл «{name}» не завантажився)")
                import shutil
                shutil.rmtree(folder, ignore_errors=True)
                continue
            mime = files.guess_mime(name, f.get("mimetype"))
            a = Attachment(aid, cid, None, name, mime, size, files.kind_of(name, mime), str(path), "pending", {},
                           who, time.time())
            self.app.store.add_attachment(a)
            self.app._ensure_processing(aid)
            ids.append(aid)
        return ids, notes

    # answering ---------------------------------------------------------------------------------------------------
    def pump(self, cid: str) -> None:
        """Answer what is waiting in a Slack conversation, unless she is answering in it now (then after that)."""
        from assistant.server import Turn
        app = self.app
        if cid in app.turns or app.store.slack_link(cid) is None:
            return
        msgs = app.store.messages(cid)
        # A reply answers the person's messages up to its `upto` (those sent while it was written come after it); a
        # finding she told later answers none of them.
        answered = max((m.meta.get("upto", m.id) for m in msgs if m.role == "assistant"
                        and "investigation" not in m.meta), default=0)
        waiting = [m for m in msgs if m.role == "user" and m.id > answered]
        if not waiting:
            return

        def failed_on_latest(m) -> bool:
            # A turn that ended in an error, and it was the one answering the latest waiting message. A message that
            # came while that turn ran is still unanswered and gets its own turn.
            if m.role != "system":
                return False
            covered = m.meta.get("answers")
            return waiting[-1].id in covered if covered else m.id > waiting[-1].id
        if any(failed_on_latest(m) for m in msgs):
            return                      # answering it failed: the person's next message asks again
        turn = Turn()
        app.turns[cid] = turn
        asyncio.get_running_loop().create_task(
            app._answer(cid, waiting[-1].author or "", waiting[-1], turn, earlier=tuple(waiting[:-1])))

    def _after_turn(self, cid: str) -> None:
        if self.app.store.slack_link(cid) is None:
            return
        self.kick.set()
        asyncio.get_running_loop().call_soon(self.pump, cid)

    # posting -----------------------------------------------------------------------------------------------------
    async def _post_loop(self) -> None:
        while True:
            try:
                await self.post_pending()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - next pass
                log.exception("posting to Slack failed")
            try:
                await asyncio.wait_for(self.kick.wait(), POST_EVERY_S)
            except TimeoutError:
                pass
            self.kick.clear()

    async def _creators_loop(self) -> None:
        while True:
            try:
                await self.refresh_creators()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the list read last time stays until the next try
                log.exception("reading who may ask for tickets from Slack failed")
            await asyncio.sleep(CREATORS_EVERY_S)

    async def refresh_creators(self) -> set[str]:
        """Members of slack.creator_channels with an e-mail of slack.creator_domain, people only. A channel Slack
        will not list fails the whole read, so a half-read list never takes the right away from anyone."""
        members: set[str] = set()
        for channel in self.cfg.creator_channels:
            cursor = None
            while True:
                resp = await self.web.conversations_members(channel=channel, limit=200, cursor=cursor)
                members.update(resp.get("members") or [])
                cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        emails: set[str] = set()
        for user in sorted(members):
            info = (await self.web.users_info(user=user))["user"]
            if info.get("is_bot") or info.get("deleted") or user == "USLACKBOT":
                continue
            email = ((info.get("profile") or {}).get("email") or "").lower()
            if email.endswith("@" + self.cfg.creator_domain):
                emails.add(email)
        self.app.store.set_slack_creators(emails)
        log.info("who may ask for tickets: %d from Slack", len(emails))
        return emails

    async def post_pending(self) -> None:
        store = self.app.store
        await self._call_expert()
        await self._ask_for_capabilities()
        for link in store.slack_links():
            thread = link.thread_ts or None
            if thread and self._has_news(link) and await self.internal(link.channel, fresh=True) is not True:
                # Asked anew before every post into a channel: it may have been shared with a client since.
                log.warning("holding posts to %s: not known to be a channel of this company only", link.channel)
                continue
            for m in store.messages(link.conversation_id):
                if m.id <= link.posted_upto or m.role not in ("assistant", "system"):
                    continue
                if m.meta.get("silent"):
                    if m.meta.get("reaction") and not await self._quiet_reaction(m):
                        break
                else:
                    text = to_mrkdwn(m.text) if m.role == "assistant" else slack_error(m.text)
                    if not await self._post_text(link, m.id, text):
                        break
                store.slack_posted(link.conversation_id, m.id)
                link.posted_upto = m.id
                answered = m.meta.get("answers") or ([m.meta["upto"]] if m.meta.get("upto") else [])
                for w in store.slack_waiting_eyes(link.conversation_id, answered):
                    await self._react(w["channel"], w["ts"], False, w["team"])
            for d in store.drafts_of(link.conversation_id):
                if d.message_id is None or d.message_id > link.posted_upto:
                    continue
                await self._card(link.channel, thread, d)

    def _has_news(self, link) -> bool:
        """Something of hers for this conversation that Slack does not have yet: a message, or a card to show."""
        store = self.app.store
        if any(m.id > link.posted_upto and m.role in ("assistant", "system")
               for m in store.messages(link.conversation_id)):
            return True
        for d in store.drafts_of(link.conversation_id):
            if d.message_id is None or d.message_id > link.posted_upto:
                continue
            card = store.slack_card(d.id)
            if card is None or card["shown"] != _shown(d):
                return True
        return False

    async def _quiet_reaction(self, m) -> bool:
        """Her silent turn's one reaction, on the last message of the turn. True when done or when it never can be
        (already there, the message gone); False on a passing failure, to try again on the next pass."""
        target = self.app.store.slack_inbox_of(int(m.meta.get("upto") or 0))
        if target is None or self.silent(target["channel"]):
            return True
        try:
            await self.web.reactions_add(channel=target["channel"], timestamp=target["ts"], name=m.meta["reaction"])
        except Exception as e:  # noqa: BLE001
            err = _error_of(e)
            # Only a failure that may pass is tried again: no answer from Slack at all, or one of its own passing
            # errors. Any other refusal (already_reacted, missing_scope, thread_locked, too_many_emoji, …) will not
            # change by trying, and must not hold up her answers after it.
            if getattr(e, "response", None) is None or err in TRANSIENT:
                log.error("reaction :%s: failed, will try again: %s", m.meta["reaction"], err)
                return False
            log.warning("reaction :%s: on %s given up: %s", m.meta["reaction"], target["ts"], err)
        return True

    async def _call_expert(self) -> None:
        """Her call for the expert: a direct message to him with who asked, what, and her reason, plus where to look.
        Not sent yet (Slack away, the expert not found): sent on the next pass."""
        store, expert = self.app.store, self.app.s.expert
        pending = store.escalations_new()
        if not pending or not expert:
            return
        try:
            uid = (await self.web.users_lookupByEmail(email=expert))["user"]["id"]
            dm = (await self.web.conversations_open(users=uid))["channel"]["id"]
        except Exception as e:  # noqa: BLE001
            log.error("cannot reach the expert %s in Slack: %s", expert, _error_of(e))
            return
        for esc in pending:
            where = []
            link = store.slack_link(esc["conversation_id"])
            if link and link.thread_ts:
                try:
                    got = await self.web.chat_getPermalink(channel=link.channel, message_ts=link.thread_ts)
                    where.append(f"<{got['permalink']}|Тред у Slack>")
                except Exception:  # noqa: BLE001 - the web link is still there
                    pass
            if self.cfg.web_url:
                where.append(f"<{self.cfg.web_url.rstrip('/')}/#{esc['conversation_id']}|Розмова у вебчаті>")
            asked = f"\n>{_esc(_cut(esc['asked'], 600))}" if esc.get("asked") else ""
            text = (f"<@{uid}>, мені потрібна твоя думка. {_esc(esc['reason'])}\n"
                    f"Просить: {_esc(esc['requester'] or 'невідомо хто')}{asked}"
                    + ("\n" + " · ".join(where) if where else ""))
            try:
                await self.web.chat_postMessage(channel=dm, text=text, mrkdwn=True, unfurl_links=False)
            except Exception as e:  # noqa: BLE001
                log.error("calling the expert failed: %s", _error_of(e))
                return
            store.escalation_sent(esc["id"])

    async def _expert_dm(self) -> tuple[str, str] | None:
        """(his user id, the DM with him), or None while Slack cannot say (then the next pass tries again)."""
        expert = self.app.s.expert
        if not expert:
            return None
        try:
            uid = (await self.web.users_lookupByEmail(email=expert))["user"]["id"]
            return uid, (await self.web.conversations_open(users=uid))["channel"]["id"]
        except Exception as e:  # noqa: BLE001
            log.error("cannot reach the expert %s in Slack: %s", expert, _error_of(e))
            return None

    async def _ask_for_capabilities(self) -> None:
        """Her requests for what she lacks (request_capability): one DM each to the expert. Unlike call_expert they
        carry no link to the conversation and open none of it to him: only who she was helping and what for."""
        store = self.app.store
        pending = store.capability_requests_new()
        if not pending:
            return
        to = await self._expert_dm()
        if to is None:
            return
        uid, dm = to
        for r in pending:
            lines = [f"<@{uid}>, мені бракує можливості на проєкті {_esc(r['project'] or self.app.s.project)}: "
                     f"*{_esc(r['need'])}*",
                     f"Для чого: {_esc(r['why'])}"]
            if r.get("source"):
                lines.append(f"Що не відкрилося: {_esc(r['source'])}")
            if r.get("error"):
                lines.append(f"Що відповіло: {_esc(_cut(r['error'], 400))}")
            lines.append(f"{'Допомагав' if self.app.s.gender == 'm' else 'Допомагала'}: "
                         f"{_esc(r['requester'] or 'невідомо кому')}"
                         + (f" · просили вже {r['times']} рази" if r["times"] > 1 else ""))
            try:
                await self.web.chat_postMessage(channel=dm, text="\n".join(lines), mrkdwn=True, unfurl_links=False)
            except Exception as e:  # noqa: BLE001 - stays new: the next pass sends it
                log.error("asking the expert for a capability failed: %s", _error_of(e))
                return
            store.capability_request_sent(r["id"])

    async def _post_text(self, link, message_id: int, text: str) -> bool:
        """True when all of it is posted, or when it never can be (then it is skipped and logged). A long answer goes
        in pieces, each one recorded once posted, so a retry (after a failure or a restart) goes on from the first
        piece Slack does not have."""
        if self.silent(link.channel):
            return True                                  # never posted there: skipped, not retried
        pieces = chunks(text)
        for i in range(link.posted_parts, len(pieces)):
            try:
                await self.web.chat_postMessage(channel=link.channel, thread_ts=link.thread_ts or None, text=pieces[i],
                                                mrkdwn=True, unfurl_links=False, unfurl_media=False)
            except Exception as e:  # noqa: BLE001
                err = _error_of(e)
                log.error("posting to %s failed: %s", link.channel, err)
                return err in PERMANENT
            self.app.store.slack_posted_part(link.conversation_id, message_id, i + 1)
            link.posted_parts = i + 1
        link.posted_parts = 0
        return True

    async def _card(self, channel: str, thread: str | None, d: Draft) -> None:
        shown = _shown(d)
        card = self.app.store.slack_card(d.id)
        if self.silent(card["channel"] if card else channel):
            return
        if card and card["shown"] == shown:
            return
        text, blocks = self.card_blocks(d)
        try:
            if card:
                await self.web.chat_update(channel=card["channel"], ts=card["ts"], text=text, blocks=blocks)
                ts = card["ts"]
            else:
                got = await self.web.chat_postMessage(channel=channel, thread_ts=thread, text=text, blocks=blocks,
                                                      unfurl_links=False, unfurl_media=False)
                ts = got["ts"]
        except Exception as e:  # noqa: BLE001 - shown on the next pass
            log.error("draft card %s failed: %s", d.id, _error_of(e))
            return
        self.app.store.slack_card_shown(d.id, card["channel"] if card else channel, ts, shown)

    def card_blocks(self, d: Draft) -> tuple[str, list[dict]]:
        kind = {"bug": "Баг", "feature": "Нова можливість", "research": "Дослідження"}.get(d.kind, d.kind)
        head = f"Правка {d.target_key}" if d.target_key else f"Чернетка тікета · {kind}"
        text = f"{head}: {d.summary}"
        link = f"{self.cfg.web_url.rstrip('/')}/#{d.conversation_id}" if self.cfg.web_url else ""
        blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{_esc(head)}*\n{_esc(d.summary)}"}}]
        if d.state == "preparing":
            blocks.append(_context("Готую тікет, за кілька хвилин він з'явиться тут."))
            return text, blocks
        body = to_mrkdwn(d.description)
        if len(body) > 2800:
            body = body[:2800].rsplit("\n", 1)[0] + "\n…"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body or " "}})
        more = []
        if d.checked == "notes" and d.review_notes:
            more.append("Перевірка лишила зауваження: " + _esc(_cut(d.review_notes, 600)))
        elif d.checked == "unchecked":
            more.append("З кодом не звірено" + (f": {_esc(_cut(d.review_notes, 300))}" if d.review_notes else "."))
        if link:
            more.append(f"<{link}|Повний текст і редагування у вебчаті>")
        if more:
            blocks.append(_context("\n".join(more)))
        if d.error and d.state == "draft":
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Не вийшло:* " + _esc(d.error)}})
        if d.state == "draft":
            blocks.append({"type": "actions", "elements": [
                {"type": "button", "action_id": "fa_create", "value": str(d.id), "style": "primary",
                 "text": {"type": "plain_text", "text": f"Оновити {d.target_key}" if d.target_key else "Створити тікет"}},
                {"type": "button", "action_id": "fa_dismiss", "value": str(d.id),
                 "text": {"type": "plain_text", "text": "Відкласти"}}]})
        elif d.state == "creating":
            blocks.append(_context("Створюю…"))
        elif d.state == "created":
            url = d.issue_url or ""
            if d.target_key:
                blocks.append(_context(f"Оновлено <{url}|{d.issue_key}> ({_esc(d.acted_by or '')})."))
            else:
                blocks.append(_context(f"Створено <{url}|{d.issue_key}> ({_esc(d.acted_by or '')}). Щоб розробка "
                                       "взялася, переведіть тікет у To Do і поставте тег agent."))
        elif d.state == "dismissed":
            blocks.append(_context(f"Відкладено ({_esc(d.acted_by or '')}), тікет не створено."))
        return text, blocks

    async def _interact(self, payload: dict) -> None:
        """A press on a draft's button: created once, however many times it is pressed, and only by someone she
        talks to and who can see that conversation."""
        if payload.get("type") != "block_actions":
            return
        user = (payload.get("user") or {}).get("id") or ""
        for act in payload.get("actions") or []:
            if act.get("action_id") not in ("fa_create", "fa_dismiss"):
                continue
            try:
                d = self.app.store.draft(int(act.get("value") or 0))
            except ValueError:
                d = None
            p = await self.person(user)
            conv = self.app.store.conversation(d.conversation_id) if d else None
            channel = (payload.get("channel") or {}).get("id") or ""
            if self.silent(channel):
                continue                                 # a button there can only be old: nothing answers it
            if d is None or p.refused or not p.email or not self.app.visible(conv, p.email):
                if channel:
                    await self._note(channel, {"user": user}, "Цю кнопку можуть натиснути лише люди з команди "
                                                              "проєкту, яким видно цю розмову.")
                continue
            if act["action_id"] == "fa_create":
                code, body = await self.app.make_ticket(d, p.email)
                if code in (403, 409) and channel:
                    await self._note(channel, {"user": user}, body.get("error", ""))
            else:
                self.app.store.dismiss_draft(d.id, p.email)
            self.kick.set()


# formatting ------------------------------------------------------------------------------------------------------
def name_forms(persona: str, extra: tuple[str, ...] = ()) -> tuple[set[str], set[str]]:
    """(her names, their vocatives): «Олександра» → {олександра}, {олександро}. Casefolded."""
    first = (persona.split() or [""])[0]
    names = {n.casefold() for n in (first, *extra) if n}
    return names, {n[:-1] + "о" for n in names if n.endswith("а") and len(n) > 2}


def called_by_name(text: str, persona: str, extra: tuple[str, ...] = ()) -> bool:
    """Spoken to by name, as in «Олександро, глянь», «Олександра, а що з тікетом?», «а ти що скажеш, Саша?».

    Not her name in the third person («кликати Олександру», «нехай Олександра цим займається»), nor in what is quoted
    (a «…» or "…" quote, a > quote block, code), nor in a message that opens by tagging someone else
    («<@U…> Олександра, глянь» is spoken to them; whoever means her there can tag her). A colleague tagged further on
    is only mentioned: «Олександро, що за помилка, про яку написав <@U…>?» is still to her."""
    names, vocatives = name_forms(persona, extra)
    if not names:
        return False
    plain = html.unescape(text)
    plain = re.sub(r"```.*?```|`[^`\n]*`", " ", plain, flags=re.S)             # code
    plain = re.sub(r"^\s*>.*$", " ", plain, flags=re.M)                       # quote blocks
    plain = re.sub(r"«[^»]*»|“[^”]*”|„[^“”]*[“”]|\"[^\"]*\"", " ", plain)     # quoted words
    if re.match(r"\s*<@[^>]*>", plain):
        return False                                       # opens by tagging another person: spoken to them
    plain = re.sub(r"<[@#!][^>]*>", " ", plain)
    words = [(m.group(0).casefold(), m.start(), m.end()) for m in re.finditer(r"\w+", plain)]
    for i, (w, start, end) in enumerate(words):
        if w in vocatives:
            return True
        if w not in names:
            continue
        before, after = plain[:start].strip(), plain[end:].lstrip()
        if i == 0 and not before.strip("«\"'(") and (not after or after[0] in ",!:—–-"):
            return True                                    # «Олександра, …» opening the message
        if before.endswith(",") and re.fullmatch(r"[?!.)]*\W*", after):
            return True                                    # «…, Олександра?» closing it
    return False


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= 50 else line[:47].rstrip() + "…"


def _shown(d: Draft) -> str:
    """What a draft's card shows: when this changes, the card is shown again."""
    return f"{d.state}|{d.error or ''}|{d.issue_key or ''}|{d.checked or ''}|{d.summary}"


def _error_of(e: Exception) -> str:
    resp = getattr(e, "response", None)
    try:
        return str(resp["error"]) if resp is not None else type(e).__name__
    except Exception:  # noqa: BLE001
        return type(e).__name__


def to_mrkdwn(md: str) -> str:
    """Her Markdown in Slack's own markup: *bold*, <url|text>, • lists, headings as bold lines. Code stays code."""
    out = []
    for i, part in enumerate(re.split(r"(```.*?```)", md, flags=re.S)):
        if i % 2:
            out.append(part)
            continue
        part = _esc(part)
        part = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", part)
        part = re.sub(r"\*\*(.+?)\*\*", r"*\1*", part)
        part = re.sub(r"__(.+?)__", r"_\1_", part)
        part = re.sub(r"(?m)^#{1,6}\s+(.+?)\s*#*$", r"*\1*", part)
        part = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", part)
        out.append(part)
    return "".join(out)


def slack_error(text: str) -> str:
    """A system note from the web chat, in Slack's terms: there is no «Спробувати ще раз» button here."""
    text = re.sub(r"\s*Натисніть «Спробувати ще раз»\.?", "", text).strip()
    return _esc(text) + "\nНапишіть мені ще раз, і я спробую знову."


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Split a long answer at paragraph, then line, then word boundaries, never inside a word if it can be helped."""
    text = text.strip() or " "
    out = []
    while len(text) > size:
        cut = max(text.rfind("\n\n", 0, size), text.rfind("\n", 0, size))
        if cut < size // 2:
            cut = text.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    out.append(text)
    return out
