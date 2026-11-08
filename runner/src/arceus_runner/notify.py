"""What the runner tells the project's Slack channel, through its incoming webhook.

The channel answers four questions at a glance: is the agent working, is it waiting for a person, has it nothing to
do, is something broken. Everything else stays in events.jsonl and Loki: a message per tool call would bury the
questions in noise within the hour, and Slack takes about one message a second per channel anyway.

    when                                 what                                       how often
    a task changes state                 started, back at work, question, MRs out,  once per transition, at once
                                         done, failed, stopped, held by security
    a code review starts, and its result who asked, how many MRs; per MR approved,     once each, at once
                                         needs changes, could not be confirmed
    a test run of the platform starts,   by command or on schedule; the outcome, the    once each, at once
    and when it finishes                 agent's short summary, the report's link
    a running task did something         tool calls (and how many came back with    at most every summary_every,
                                         an error), the engine's step, the latest   only when there was any,
                                                                                    in working hours by default
    the agent writes nothing             quiet for agent_quiet_after                once per quiet spell
    a usage limit is out                 paused until the window resets             once per pause
    a question or held text waits        reminder, with the mention                 every question_reminder, in
                                                                                    working hours
    nothing to take                      the queue is empty, what waits on people,  once per idle spell, after
                                         what the agent can be given                idle_after, working hours only
    a working day starts                 status: in work, waiting, in review,       once a working day
                                         failed, queue
    health                               a check fails, a service is unreachable    on the change, and once when
                                         for service_down_after, and back           it is back
    the runner                           a tick failing                             at most once an hour
                                         started (with how long it was down),       only with runner_lifecycle;
                                         stopped                                    restarts closer than
                                                                                    restart_quiet are counted

A runner that died cannot say so; the daily status is the heartbeat whose absence says it, and with runner_lifecycle
the next start says how long it was down. Watching `runner_last_tick_seconds` from outside is the alert for it.

Durability. Every message goes through the `slack_outbox` table and a sender thread of its own posts it, so the loop
never waits for Slack and a Slack outage stops nothing. A task's state is told from runner.db itself, compared with
the state last told (`slack_task_state`), and the new state and its message are written in one transaction: a
runner that died between a state change and telling it tells it on its next start. A message Slack took is not sent
again; one whose post timed out may have been posted and is sent again with the same visible number.
Questions and reminders are dropped unsent when the task moved on before they went out.

Text from tickets and from the agent is escaped: an `<!channel>` in it is text, never a mention. Only the people
config.yaml maps to Slack members are mentioned, and only on a question, a failure or a held text.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import random
import re
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from arceus_runner.arch_report import second_check
from arceus_runner.checks import Check
from arceus_runner.config import Backoff, Config, Schedule
from arceus_runner.db import ACTIVE_STATES, Task, TaskStore
from arceus_runner.events import EventLog
from arceus_runner.literals import balanced, marked
from arceus_runner.slack import Webhook

# Who goes first when several wait: a person is needed > a task moved > the runner's own news > routine.
URGENT, TRANSITION, RUNNER, ROUTINE = 0, 1, 2, 3
# A finished code review (code_review.py): the overall outcome, then each merge request's.
# A finished test run of the platform (qa.py).
QA_HEADS = {"passed": "✅ Platform test found no problems",
            "partial": "☑️ Platform test found no problems in what it checked, but could not check everything",
            "found": "🐞 Platform test found {n} problem{s}",
            "blocked": "⛔️ Platform test could not test", "unfinished": "⚠️ Platform test did not finish",
            "stopped": "⏹ Platform test stopped"}
# A finished architecture review (architect.py).
ARCH_HEADS = {"clean": "✅ Architecture review found nothing to raise",
              "partial": "☑️ Architecture review found nothing to raise in what it read, but did not read everything",
              "found": "🏛 Architecture review: {n} finding{s}",
              "unchanged": "⚪️ Architecture review: nothing changed since the last one",
              "unfinished": "⚠️ Architecture review did not finish", "stopped": "⏹ Architecture review stopped"}
REVIEW_HEADS = {"approved": "✅ Code review approved", "attention": "👀 Code review approved, a person has to look first",
                "ready": "☑️ Code review found no problems, not everything is approved", "changes": "🛠 Code review: changes needed",
                "unverified": "⚠️ Code review could not confirm", "nothing": "⚪️ Code review: nothing left to review",
                "switched_off": "⚪️ Code review switched off while it ran"}
REVIEW_WORDS = {"approved": "approved, ready to merge", "attention": "approved, a person has to look at some changes",
                "ready": "no problems found, not approved", "changes": "changes needed",
                "unverified": "could not be confirmed", "closed": "merged or closed meanwhile"}
MIN_GAP = 1.1          # seconds between two posts: Slack takes about one a second per channel
BROKEN_RETRY = 3600    # a webhook Slack refused is tried again this often, in case a person fixed it
DRAIN_SECONDS = 5      # on stop: what is left (the stop itself) gets this long to go out
KEEP_DONE = 30 * 86400  # sent and dropped rows are kept this long, for `doctor` and a look back
TEXT_LIMIT = 600
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def is_run(task_id: object) -> bool:
    """A background agent's run (workspace_run.py), not a ticket: its engine task is qa-<n> or arch-<n>."""
    return isinstance(task_id, str) and re.fullmatch(r"(?:qa|arch)-\d+", task_id) is not None


def esc(text: object) -> str:
    """Slack reads &, < and > as markup (<!channel>, <@U…>, links): in someone else's text they are text."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clip(text: object, limit: int = TEXT_LIMIT) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def clip_sentences(text: object, limit: int = TEXT_LIMIT) -> str:
    """The whole sentences of `text` that fit in `limit`, so a long summary reads as finished, not cut mid-word;
    only when not even the first sentence fits is it cut at a word instead."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit + 1]
    end = max(head.rfind(m) for m in (". ", "! ", "? ", ".\u00a0"))
    if end >= 0:
        return head[:end + 1] + " …"
    cut = head[:limit].rsplit(" ", 1)[0] if " " in head[:limit] else head[:limit]
    return cut.rstrip(",;:") + "…"


def summary_text(text: object, limit: int = TEXT_LIMIT) -> str:
    """An agent's summary for the channel: its code (paths, names) in backticks, as Slack shows code, cut at whole
    sentences, and a code span the cut fell into closed."""
    text = marked(" ".join(str(text or "").split()))
    clipped = clip_sentences(text, limit)
    return balanced(clipped) if clipped != text else clipped


def started_how(r: dict) -> str:
    """How a background run was started, when that says something: the schedule. Who typed the command on the
    server is the runner's own user, which tells the channel nothing."""
    return ", on schedule" if r.get("source") == "schedule" else ""


def ago(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{hours // 24} days"


def working_at(ts: float, schedule: Schedule) -> bool:
    local = datetime.fromtimestamp(ts, UTC).astimezone(schedule.tz)
    wh = schedule.working_hours
    return DAYS[local.weekday()] in wh.days and wh.start <= local.time() < wh.end


# -- the outbox ---------------------------------------------------------------------------------------------------


class Outbox:
    """slack_outbox and its bookkeeping, over one SQLite connection (the loop's, or the sender thread's own)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def put(self, key: str, kind: str, priority: int, text: str, at: float, expires: float,
            task_id: str | None = None, live_while: dict | None = None) -> bool:
        """False when a message with this key is already there: the same news is told once."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO slack_outbox (key, kind, priority, text, task_id, live_while, created_at,"
            " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, kind, priority, text, task_id, json.dumps(live_while) if live_while else None, at, expires))
        return cur.rowcount == 1

    def next_pending(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM slack_outbox WHERE status = 'pending' ORDER BY priority, id"
                                 " LIMIT 1").fetchone()

    def pending(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM slack_outbox WHERE status = 'pending'").fetchone()[0]

    def finish(self, row_id: int, status: str, at: float, error: str | None = None) -> None:
        self.conn.execute("UPDATE slack_outbox SET status = ?, done_at = ?, last_error = COALESCE(?, last_error)"
                          " WHERE id = ?", (status, at, error, row_id))

    def attempt_failed(self, row_id: int, error: str, uncertain: bool) -> None:
        self.conn.execute("UPDATE slack_outbox SET attempts = attempts + 1, last_error = ?,"
                          " uncertain = MAX(uncertain, ?) WHERE id = ?", (error, int(uncertain), row_id))

    def status_of(self, key: str) -> str | None:
        row = self.conn.execute("SELECT status FROM slack_outbox WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def prune(self, before: float) -> None:
        self.conn.execute("DELETE FROM slack_outbox WHERE status != 'pending' AND done_at < ?", (before,))

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM slack_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO slack_meta (key, value) VALUES (?, ?)"
                          " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def drop_meta(self, key: str) -> None:
        self.conn.execute("DELETE FROM slack_meta WHERE key = ?", (key,))

    def told_states(self) -> dict[str, str]:
        return dict(self.conn.execute("SELECT task_id, sig FROM slack_task_state").fetchall())

    def set_told_state(self, task_id: str, sig: str) -> None:
        self.conn.execute("INSERT INTO slack_task_state (task_id, sig) VALUES (?, ?)"
                          " ON CONFLICT (task_id) DO UPDATE SET sig = excluded.sig", (task_id, sig))

    def still(self, task_id: str | None, live_while: dict) -> bool:
        """Whether what the message assumes still holds: its task in that state (and question), or a runner state
        in slack_meta (an idle spell) still the one it was written in."""
        if "meta" in live_while and self.meta(live_while["meta"]) != live_while.get("value"):
            return False
        if "state" not in live_while:
            return True
        row = self.conn.execute("SELECT state, question_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row or row[0] != live_while["state"]:
            return False
        return "question_id" not in live_while or row[1] == live_while["question_id"]

    def drop_pending(self, kind: str, at: float, why: str) -> int:
        cur = self.conn.execute("UPDATE slack_outbox SET status = 'dropped', done_at = ?, last_error = ?"
                                " WHERE status = 'pending' AND kind = ?", (at, why, kind))
        return cur.rowcount

    def drop_pending_but(self, kind: str, keep: str, at: float, why: str) -> int:
        """Like drop_pending, sparing the messages whose key matches the GLOB `keep`."""
        cur = self.conn.execute("UPDATE slack_outbox SET status = 'dropped', done_at = ?, last_error = ?"
                                " WHERE status = 'pending' AND kind = ? AND key NOT GLOB ?", (at, why, kind, keep))
        return cur.rowcount


# -- the sender thread ----------------------------------------------------------------------------------------------


class Sender:
    """Posts the outbox, highest priority first, at most one message per MIN_GAP, in a thread of its own. It writes
    to events.jsonl (never through the notifier: a failing Slack does not report itself to Slack) and keeps its
    state in slack_meta for `doctor`."""

    def __init__(self, db_path: Path, webhook: Webhook, project: str, events: EventLog, backoff: Backoff,
                 clock: Callable[[], float] = time.time, rng: Callable[[], float] = random.random,
                 schedule: Schedule | None = None):
        self.db_path = db_path
        self.schedule = schedule  # for messages meant for working hours only
        self.webhook = webhook
        self.project = project
        self.events = events
        self.backoff = backoff
        self.clock = clock
        self.rng = rng
        self.paused_until = 0.0
        self.last_post = 0.0
        self.failures = 0
        self.broken: str | None = None
        self.last_error: str | None = None
        self._ob: Outbox | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def outbox(self) -> Outbox:
        """The thread's own connection: an SQLite connection stays on the thread that opened it."""
        if self._ob is None:
            conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30)
            conn.row_factory = sqlite3.Row
            self._ob = Outbox(conn)
            # A restart keeps Slack's Retry-After, the backoff and a refused webhook: no post before they allow.
            saved = json.loads(self._ob.meta("webhook") or "{}")
            self.paused_until = max(self.paused_until, float(saved.get("paused_until") or 0))
            self.failures = int(saved.get("failures") or 0)
            self.broken = saved.get("broken") or None
            self.last_error = saved.get("last_error") or None
        return self._ob

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="slack-sender", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop the thread after giving what is left DRAIN_SECONDS to go out (the runner's stop among it)."""
        self._stop.set()
        if self._thread:
            self._thread.join(DRAIN_SECONDS + 15)
        self.webhook.close()

    def _run(self) -> None:
        try:
            with contextlib.suppress(sqlite3.Error):  # a busy database now: pruned at the next start
                self.outbox().prune(self.clock() - KEEP_DONE)
            while not self._stop.is_set():
                self._step()
                self._stop.wait(MIN_GAP)
            deadline = self.clock() + DRAIN_SECONDS
            while self.clock() < deadline and self.outbox().pending() and self.clock() >= self.paused_until:
                self._step()
                time.sleep(MIN_GAP)
        finally:
            if self._ob:
                self._ob.conn.close()

    def _step(self) -> None:
        try:
            self.send_one()
        except Exception as e:  # a bug here must not end the thread: the outbox keeps the messages
            self.events.emit("slack_sender_failed", error=f"{type(e).__name__}: {e}")
            self._stop.wait(30)

    def send_one(self) -> bool:
        """Post the next message that is due. True when a post was made."""
        ob = self.outbox()  # first: it brings back a pause saved by the runner before this one
        now = self.clock()
        if now < self.paused_until or now - self.last_post < MIN_GAP:
            return False
        while True:
            row = ob.next_pending()
            if row is None:
                return False
            if row["expires_at"] <= now:
                ob.finish(row["id"], "dropped", now, "expired")
                self.events.emit("slack_dropped", id=row["id"], kind=row["kind"], reason="expired",
                                 attempts=row["attempts"])
                continue
            live = json.loads(row["live_while"]) if row["live_while"] else {}
            moved = "the task moved on" if "state" in live else "the idle spell ended"
            why = (f"{moved} before it went out" if not ob.still(row["task_id"], live) else
                   "the working day ended before it went out"
                   if live.get("hours") and self.schedule and not working_at(now, self.schedule) else None)
            if why:
                ob.finish(row["id"], "dropped", now, "stale")
                self.events.emit("slack_dropped", id=row["id"], kind=row["kind"], task_id=row["task_id"], reason=why)
                continue
            break
        self.last_post = now
        posted = self.webhook.post(f"{row['text']}\n_{esc(self.project)} · #{row['id']}_")
        if posted.verdict == "sent":
            ob.finish(row["id"], "sent", self.clock())
            self.events.emit("slack_sent", id=row["id"], kind=row["kind"], task_id=row["task_id"],
                             attempts=row["attempts"] + 1, maybe_twice=bool(row["uncertain"]) or None)
            if self.broken or self.failures:
                self.events.emit("slack_restored", after=self.broken or self.last_error)
            self.failures, self.broken, self.last_error = 0, None, None
            self._save_state()
            return True
        if posted.verdict == "rejected":
            ob.finish(row["id"], "rejected", self.clock(), posted.detail)
            self.events.emit("slack_rejected", id=row["id"], kind=row["kind"], reason=posted.detail)
            return True
        ob.attempt_failed(row["id"], posted.detail, posted.uncertain)
        if posted.verdict == "broken":
            if self.broken != posted.detail:
                self.events.emit("slack_webhook_broken", reason=posted.detail, waiting=ob.pending())
            self.broken = posted.detail
            self.paused_until = self.clock() + BROKEN_RETRY
        else:
            self.failures += 1
            self.last_error = posted.detail
            delay = self._backoff()
            if posted.retry_after is not None:
                delay = max(posted.retry_after, MIN_GAP)  # Slack said when: sooner is refused, later is a waste
            self.paused_until = self.clock() + delay
            self.events.emit("slack_retry", id=row["id"], reason=posted.detail, retry_in_s=round(delay),
                             maybe_posted=posted.uncertain or None)
        self._save_state()
        return True

    def _backoff(self) -> float:
        b = self.backoff
        base = min(b.start.total_seconds() * (2 ** (self.failures - 1)), b.max.total_seconds())
        return base * (1 + b.jitter * (2 * self.rng() - 1))

    def _save_state(self) -> None:
        check = self.check()
        self.outbox().set_meta("webhook", json.dumps({
            "status": check.status, "detail": check.detail, "at": self.clock(), "paused_until": self.paused_until,
            "failures": self.failures, "broken": self.broken, "last_error": self.last_error}))

    def check(self) -> Check:
        """Never `fail`: a fail stops new tasks, and Slack being down must not stop the work it reports on."""
        if self.broken:
            return Check("slack.webhook", "warn", f"Slack refuses the webhook ({self.broken}); messages wait in "
                                                  "runner.db. Replace the URL in secrets.env and restart")
        if self.failures >= 3:
            return Check("slack.webhook", "warn", f"Slack unreachable, {self.failures} tries ({self.last_error}); "
                                                  "messages wait in runner.db")
        return Check("slack.webhook", "ok", "posting")


def webhook_state(store: TaskStore) -> Check | None:
    """What the running runner last knew of the webhook, and how many messages wait: for `doctor`."""
    ob = Outbox(store.conn)
    raw = ob.meta("webhook")
    waiting = ob.pending()
    if raw is None:
        return Check("slack.webhook", "ok", f"nothing posted yet; {waiting} waiting") if waiting else None
    data = json.loads(raw)
    return Check("slack.webhook", data["status"], f"{data['detail']}; {waiting} waiting")


# -- what to tell ---------------------------------------------------------------------------------------------------


@dataclass
class Activity:
    """A running task's activity since the last summary."""
    calls: int = 0  # every tool call; `tools` holds only the most used kinds of each report, for the detail
    tools: Counter = field(default_factory=Counter)
    errors: int = 0
    last_target: str = ""
    step: str = ""
    fresh: bool = False  # something happened since the last summary; the step alone is not news


class Notifier:
    """Decides what the channel hears; runs on the loop's thread. Events come in through EventLog.listen, the
    state of tasks, health and the clock through `health` and `tick`."""

    def __init__(self, cfg: Config, store: TaskStore, events: EventLog, issue_url: Callable[[str], str],
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        assert cfg.slack
        self.cfg = cfg
        self.n = cfg.slack.notify
        self.people = cfg.slack.people
        self.store = store
        self.outbox = Outbox(store.conn)
        self.events = events
        self.issue_url = issue_url
        self.now = now
        self.activity: dict[str, Activity] = {}
        self.summary_from = now()
        self.down_since: dict[str, datetime] = {}
        self.sender: Sender | None = None
        if not self.outbox.meta("seeded"):
            # The first start with Slack: where every task already is, is not news.
            with self.store.transaction():
                for t in self.store.tasks():
                    self.outbox.set_told_state(t.task_id, self.sig(t))
                self.outbox.set_meta("seeded", self.now().isoformat())
        with self.store.transaction():
            # Before the sender starts: what an older runner queued and this one no longer tells does not go out now.
            at = self.now().timestamp()
            if not self.n.runner_lifecycle:
                for kind in ("runner_started", "runner_stopped"):
                    self.outbox.drop_pending(kind, at, "starts and stops are not told")
            if self.outbox.drop_pending_but("idle", "idle_offer:*", at, "an older runner's idle notice"):
                # Its 💤 had no commands, or was a repeat: this spell is told once more, the new way.
                self.outbox.drop_meta("idle_told")
        events.listen(self.on_event)

    # -- putting a message in the outbox --

    def put(self, key: str, kind: str, priority: int, text: str, task_id: str | None = None,
            live_while: dict | None = None) -> bool:
        at = self.now().timestamp()
        life = self.n.routine_expire if priority == ROUTINE else self.n.expire
        # Titles, reasons and questions come from tickets and the agent: the runner's own tokens never leave in them.
        text = self.events.redact(text)
        return self.outbox.put(key, kind, priority, text, at, at + life.total_seconds(), task_id, live_while)

    def mention(self, login: str | None) -> str:
        sid = self.people.get(login or "")
        return f"<@{sid}> " if sid else ""

    def line(self, task_id: str, task: Task | None = None) -> str:
        """The ticket as a link with its title: `ACME-12 Fix the login form`; a test run as its number."""
        if is_run(task_id):
            kind, n = task_id.split("-", 1)
            return f"*the {'platform test' if kind == 'qa' else 'architecture review'}, run {esc(n)}*"
        task = task or self.store.task(task_id)
        key = task_id.split(".review-")[0]
        link = f"*<{self.issue_url(key)}|{esc(key)}>*"
        if task_id != key:
            link = f"the code review of {link}"
        return f"{link} {esc(clip(task.summary, 120))}" if task and task_id == key else link

    def working(self) -> bool:
        return working_at(self.now().timestamp(), self.cfg.schedule)

    # -- events -------------------------------------------------------------------------------------------------

    def on_event(self, record: dict) -> None:
        name = str(record.get("event") or "")
        if name.startswith("slack_"):
            return  # the notifier's and the sender's own lines: nothing to tell
        handler = getattr(self, f"_on_{name}", None)
        try:
            if handler:
                handler(record)
            task_id = record.get("task_id")
            if isinstance(task_id, str) and ".review-" not in task_id and not is_run(task_id):
                # At once, not at the end of the tick: a task can pass through a state within one tick (answered,
                # then finished) and each step is told. Inside the caller's transaction when it has one, so the
                # telling stands or falls with the state change.
                self.sync_task(task_id)
        except Exception as e:  # telling must never break the work it tells about
            self.events.emit("slack_notify_failed", on=name, error=f"{type(e).__name__}: {e}")

    def _on_runner_started(self, r: dict) -> None:
        now = self.now().timestamp()
        last = float(self.outbox.meta("start_told") or 0)
        if now - last < self.n.restart_quiet.total_seconds():
            self.outbox.set_meta("restarts_untold", str(int(self.outbox.meta("restarts_untold") or 0) + 1))
            return
        if not self.n.runner_lifecycle:
            # Not told, but counted as told: switched back on, the next start says what happened since this one.
            self.outbox.set_meta("start_told", str(now))
            self.outbox.set_meta("restarts_untold", "0")
            return
        text = f"🟢 Arceus runner started ({esc(r.get('mode'))} mode)."
        beat = float(self.outbox.meta("heartbeat") or 0)
        if beat and now - beat > self.n.restart_quiet.total_seconds():
            since = datetime.fromtimestamp(beat, UTC).astimezone(self.cfg.schedule.tz)
            text += f" It was down since {since:%a %H:%M} ({ago(now - beat)})."
        untold = int(self.outbox.meta("restarts_untold") or 0)
        if untold:
            times = "time" if untold == 1 else "times"
            text += f" It restarted {untold} more {times} since the last notice: see `journalctl -u arceus-runner`."
        # Keyed by the notifier's clock, as everything it decides: two starts are two messages, never one key.
        self.put(f"started:{now}", "runner_started", RUNNER, text)
        self.outbox.set_meta("start_told", str(now))
        self.outbox.set_meta("restarts_untold", "0")

    def _on_runner_stopped(self, r: dict) -> None:
        now = self.now().timestamp()
        self.outbox.set_meta("heartbeat", str(now))
        if now - float(self.outbox.meta("start_told") or 0) < self.n.restart_quiet.total_seconds():
            return  # a quick restart: the next start counts it
        if not self.n.runner_lifecycle:
            return
        why = "it was asked to stop" if r.get("reason") == "signal" else "it exited"
        # Ahead of the runner's other news: it has DRAIN_SECONDS to go out before the process ends.
        self.put(f"stopped:{now}", "runner_stopped", TRANSITION,
                 f"⚪️ Arceus runner stopped: {why}. Nothing is taken or followed until it starts again.")

    def _on_review_dispatched(self, r: dict) -> None:
        row = self.store.review(r["review"]) if hasattr(self.store, "review") else None
        if row is None:
            return
        n = len(json.loads(row.snapshot or "{}").get("mrs") or [])
        by = "again on the agent's fixes" if row.comment_id.startswith("auto-") else f"asked by {esc(row.author)}"
        self.put(f"review:{row.id}:started", "review_started", TRANSITION,
                 f"🔍 Code review of {self.line(row.task_id)} started, {by}: {n} merge request{'s' if n != 1 else ''}.",
                 task_id=row.task_id)

    def _on_review_published(self, r: dict) -> None:
        row = self.store.review(r["review"]) if hasattr(self.store, "review") else None
        if row is None:
            return
        outcome = str(r.get("outcome") or "")
        head = REVIEW_HEADS.get(outcome, "🔍 Code review finished")
        words = {u: REVIEW_WORDS.get(o, esc(o)) for u, o in (r.get("mrs") or {}).items()}
        repos = {m["url"]: m["repo"] for m in json.loads(row.result or "{}").get("mrs", [])}
        lines = [f"{head}: {self.line(row.task_id)}"]
        lines += [f"• <{u}|{esc(repos.get(u, u))}>: {w}" for u, w in words.items()]
        lines.append(f"Details on the ticket: {self.issue_url(row.task_id)}")
        self.put(f"review:{row.id}:published", "review_published", TRANSITION, "\n".join(lines), task_id=row.task_id)

    def _on_qa_dispatched(self, r: dict) -> None:
        self.put(f"qa:{r['task_id']}:started", "qa_started", TRANSITION,
                 f"🧪 Platform test started: run {esc(r.get('run'))}{started_how(r)}.")

    def _on_qa_published(self, r: dict) -> None:
        run = self.store.run(r["task_id"]) if hasattr(self.store, "run") else None
        if run is None:
            return
        n = int(r.get("findings") or 0)
        head = QA_HEADS.get(run.outcome or "", "🧪 Platform test finished").format(n=n, s="s" if n != 1 else "")
        result = json.loads(run.result or "{}")
        lines = [f"{head}: run {run.n}."]
        said = (result.get("blocked") if run.outcome == "blocked" else "") or result.get("summary")
        if said:
            lines.append(esc(summary_text(said, 600)))
        if run.reason and run.outcome not in ("found", "passed", "partial"):
            # Why this is not a clean result, in the runner's words: the agent's summary may sound like one.
            lines.append(f"Why: {esc(clip(run.reason, 300))}")
        lines.append(f"Report (through the SSH tunnel): {r.get('url')}")
        self.put(f"qa:{run.id}:published", "qa_published", TRANSITION, "\n".join(lines))

    def _on_arch_dispatched(self, r: dict) -> None:
        self.put(f"arch:{r['task_id']}:started", "arch_started", TRANSITION,
                 f"🏛 Architecture review started: run {esc(r.get('run'))}{started_how(r)}.")

    def _on_arch_published(self, r: dict) -> None:
        run = self.store.run(r["task_id"]) if hasattr(self.store, "run") else None
        if run is None:
            return
        result = json.loads(run.result or "{}")
        n = len(result.get("findings") or [])
        head = ARCH_HEADS.get(run.outcome or "", "🏛 Architecture review finished").format(n=n, s="s" if n != 1 else "")
        ledger = result.get("ledger") or {}
        counts = [f"{len(ledger[k])} {w}" for k, w in (("new", "new"), ("still", "still open"), ("fixed", "fixed"))
                  if ledger.get(k)]
        lines = [f"{head}: run {run.n}" + (f" ({', '.join(counts)})." if counts else ".")]
        so = result.get("second_opinion") or {}
        if so.get("status") == "done":
            lines.append(f"Second check: {second_check(so, n)}.")
        elif so.get("status") == "unavailable":
            lines.append("The second check of the findings did not run: see the report.")
        elif so.get("status") == "skipped":
            lines.append(f"The second check of the findings was skipped: {esc(so.get('why') or 'no reason given')}.")
        if result.get("summary"):
            lines.append(esc(summary_text(result["summary"], 600)))
        if run.reason and run.outcome not in ("found", "clean", "partial", "unchanged"):
            lines.append(f"Why: {esc(clip(run.reason, 300))}")
        lines.append(f"Report (through the SSH tunnel): {r.get('url')}")
        self.put(f"arch:{run.id}:published", "arch_published", TRANSITION, "\n".join(lines))

    def _on_agent_activity(self, r: dict) -> None:
        a = self.activity.setdefault(r["task_id"], Activity())
        a.calls += int(r.get("tools") or 0)
        a.tools.update(r.get("by_tool") or {})
        a.errors += int(r.get("errors") or 0)
        a.last_target = r.get("last_target") or r.get("last_tool") or a.last_target
        a.fresh = True

    def _on_engine_step(self, r: dict) -> None:
        a = self.activity.setdefault(r["task_id"], Activity())
        a.step = f"{r.get('stage')} ({r.get('state')})"
        a.fresh = True

    def _on_agent_quiet(self, r: dict) -> None:
        timeout = ago(self.cfg.limits.attempt_timeout.total_seconds())
        step = f" (step: {esc(r['step'])})" if r.get("step") else ""
        self.put(f"quiet:{r['task_id']}:{r['ts']}", "agent_quiet", TRANSITION,
                 f"🤫 {self.line(r['task_id'])}: the agent has written nothing for {r.get('minutes')} min{step}. "
                 f"It may be stuck; an attempt with no result after {timeout} is stopped.",
                 task_id=r["task_id"],
                 live_while={"state": "dispatched"} if ".review-" not in r["task_id"] and not is_run(r["task_id"]) else None)

    def _on_attempt_paused_for_limit(self, r: dict) -> None:
        limits = ", ".join(esc(x).replace("_", " ") for x in r.get("limits") or []) or "a usage limit"
        self.put(f"limit:{r['task_id']}:{r['ts']}", "paused_for_limit", TRANSITION,
                 f"⏸ {self.line(r['task_id'])}: paused, {limits} is used up. It goes on by itself when the window "
                 "resets; the attempt's clock stops meanwhile.", task_id=r["task_id"])

    def _on_tick_failed(self, r: dict) -> None:
        error = str(r.get("error") or "")
        digest = hashlib.sha256(error.encode()).hexdigest()[:12]
        hour = int(self.now().timestamp() // 3600)
        self.put(f"tick_failed:{digest}:{hour}", "tick_failed", RUNNER,
                 f"⚠️ A runner tick failed: `{esc(clip(error, 300))}`. The loop goes on; the same error is told at "
                 "most once an hour.")

    # -- health -------------------------------------------------------------------------------------------------

    def health(self, check: Check) -> None:
        """Every check of every poll. A fail is told at once, an unreachable service after service_down_after,
        any other warning on its change; and once more when it is back."""
        if check.name.startswith("slack."):
            return  # Slack's own trouble is not news for Slack
        try:
            self._health(check)
        except Exception as e:  # the health poll goes on whatever happens to its telling
            self.events.emit("slack_notify_failed", check=check.name, error=f"{type(e).__name__}: {e}")

    def _health(self, check: Check) -> None:
        told = json.loads(self.outbox.meta("health") or "{}")
        before = told.get(check.name)
        now = self.now()
        tell = None
        if check.status == "ok":
            self.down_since.pop(check.name, None)
            if before:
                tell = f"✅ {esc(check.name)} is back to normal: {esc(clip(check.detail, 200))}."
                told.pop(check.name)
        elif check.status == "fail" and before != "fail":
            tell = (f"🚨 {esc(check.name)} failed: {esc(clip(check.detail, 300))}. New tasks wait until it is fixed; "
                    "a running one goes on.")
            told[check.name] = "fail"
        elif check.status == "warn" and not before:
            if check.name.startswith("service."):
                since = self.down_since.setdefault(check.name, now)
                if now - since < self.n.service_down_after:
                    return
                service = esc(check.name.removeprefix("service."))
                tell = (f"⚠️ {service} has not answered for {ago((now - since).total_seconds())}: "
                        f"{esc(clip(check.detail, 200))}. New tasks wait; the runner keeps trying.")
            else:
                tell = f"⚠️ {esc(check.name)}: {esc(clip(check.detail, 300))}."
            told[check.name] = "warn"
        if tell:
            with self.store.transaction():
                self.put(f"health:{check.name}:{check.status}:{now.timestamp():.0f}", "health", RUNNER, tell)
                self.outbox.set_meta("health", json.dumps(told))

    # -- the clock ----------------------------------------------------------------------------------------------

    def tick(self, situation: str, queue_known: bool = True) -> None:
        """After every tick of the loop. `situation`: working, waiting (on people), idle, unknown (the queue could
        not be read, or health keeps new work out) or observe. `queue_known`: the tracker answered its last poll,
        whatever the situation: a task can be working while the queue cannot be read."""
        self.sync_tasks()
        self.remind()
        self.summarize()
        self.daily(situation, queue_known and situation != "unknown")
        self.idle(situation)
        self.outbox.set_meta("heartbeat", str(self.now().timestamp()))

    @staticmethod
    def sig(t: Task) -> str:
        return f"{t.state}|{t.question_id or ''}"

    def sync_tasks(self) -> None:
        """Tell each task whose state is not the one told yet: also what changed while no event said so (a crash
        between the state change and its event)."""
        told = self.outbox.told_states()
        for t in self.store.tasks():
            if told.get(t.task_id) != self.sig(t):
                self.sync_task(t.task_id, t)

    def sync_task(self, task_id: str, t: Task | None = None) -> None:
        t = t or self.store.task(task_id)
        if t is None:
            return
        sig = self.sig(t)
        prev = self.outbox.told_states().get(t.task_id)
        if prev == sig:
            return
        own = contextlib.nullcontext() if self.store.conn.in_transaction else self.store.transaction()
        with own:
            message = self.transition(t, prev.split("|")[0] if prev else None)
            if message:
                priority, text, live = message
                self.put(f"task:{t.task_id}:{sig}:{t.updated_at}", f"task_{t.state}", priority, text,
                         task_id=t.task_id, live_while=live)
            self.outbox.set_told_state(t.task_id, sig)

    def transition(self, t: Task, prev: str | None) -> tuple[int, str, dict | None] | None:
        if t.people_work and t.state != "security_hold":
            return None  # people's merge requests the runner only reviews: their ticket tells the rest
        line = self.line(t.task_id, t)
        who = self.mention(t.tag_author)
        if t.state in ACTIVE_STATES:
            if prev in ACTIVE_STATES:
                return None  # claimed, dispatched, publishing: one stretch of work
            text = {
                None: f"▶️ Started {line}\nQueued by {esc(t.tag_author)}"
                      + ("; a research ticket: the answer goes to the ticket." if t.research else "."),
                "needs_input": f"▶️ Got an answer on {line}; back at work.",
                "code_review": f"🔧 Working on review notes for {line}.",
                "security_hold": f"▶️ A security reviewer let {line} through; back at work.",
            }.get(prev, f"🔁 Picked {line} up again after `/continue`.")
            return TRANSITION, text, None
        if t.state == "needs_input":
            return URGENT, self.question(t, line, who), {"state": "needs_input", "question_id": t.question_id}
        if t.state == "security_hold":
            hold = self.store.open_hold_of(t.task_id)
            what = f"the {esc(hold['kind'])}" if hold else "a text for the agent"
            reviewers = "".join(self.mention(r) for r in self.cfg.security.reviewers)
            return URGENT, (f"🛡 {reviewers}{line} waits for a security reviewer: the screen held {what}. "
                            f"Decide on the ticket: {self.issue_url(t.task_id)}"), {"state": "security_hold"}
        if t.state == "code_review":
            mrs = "\n".join(f"• {u}" for u in json.loads(t.mr_urls or "[]"))
            again = t.review_rounds or t.review_answers
            head = f"📬 Pushed the review fixes for {line}" if again and prev == "dispatched" \
                else f"📬 Merge requests ready for review for {line}"
            return TRANSITION, f"{head}:\n{mrs}" if mrs else f"{head}.", None
        if t.state == "done":
            text = {
                "merge requests merged": f"✅ {line}: every merge request is merged. Done.",
                "no code changes": f"✅ {line} finished without code changes; the result passed review.",
            }.get(t.reason or "", f"✅ Answer posted on {line}." if t.research else f"✅ {line} is done.")
            return TRANSITION, text, None
        if t.state == "failed":
            again = " To try again, comment `/continue` on the ticket." if t.branch else ""
            return URGENT, f"❌ {who}{line} failed: {esc(clip(t.reason or 'no reason given', 300))}.{again}", None
        if t.state == "stopped":
            return TRANSITION, f"⏹ {line} stopped: {esc(clip(t.reason or 'stopped', 300))}.", None
        return None

    def question(self, t: Task, line: str, who: str) -> str:
        q = json.loads(t.question or "{}")
        if q.get("id") != t.question_id:
            q = {}  # an earlier question's words (or a task from before they were kept): the ticket has them
        head = "is blocked and needs a decision" if q.get("blocked") else "needs an answer"
        lines = [f"❓ {who}{line} {head}."]
        if q.get("text"):
            lines += ["", "> " + esc(clip(q["text"])).replace("\n", "\n> ")]
        lines += [f"{i}. {esc(clip(o, 200))}" for i, o in enumerate(q.get("options") or [], 1)]
        lines += ["", f"Answer with a comment on the ticket: {self.issue_url(t.task_id)}"]
        return "\n".join(lines)

    def remind(self) -> None:
        """An unanswered question or a held text: again every question_reminder, in working hours only."""
        if not self.working():
            return
        every = self.n.question_reminder.total_seconds()
        now = self.now().timestamp()
        for t in self.store.tasks("needs_input", "security_hold"):
            if t.state == "needs_input":
                since = (t.question_at_ms or 0) / 1000
                live = {"state": "needs_input", "question_id": t.question_id}
                ref = t.question_id or t.question_at_ms
            else:
                hold = self.store.open_hold_of(t.task_id)
                since = datetime.fromisoformat(hold["held_at"]).timestamp() if hold else 0
                live, ref = {"state": "security_hold"}, hold["digest"] if hold else ""
            if not since or now - since < every:
                continue
            n = int((now - since) // every)
            if t.state == "needs_input":
                left = self.cfg.limits.needs_input_fail_after.total_seconds() - (now - since)
                who = self.mention(t.tag_author)
                text = (f"⏰ {who}{self.line(t.task_id, t)} still waits for an answer (asked {ago(now - since)} ago). "
                        f"Without one it fails in {ago(max(0, left))}: {self.issue_url(t.task_id)}")
            else:
                who = "".join(self.mention(r) for r in self.cfg.security.reviewers)
                text = (f"⏰ {who}{self.line(t.task_id, t)} still waits for a security reviewer (held "
                        f"{ago(now - since)} ago): {self.issue_url(t.task_id)}")
            self.put(f"remind:{t.task_id}:{ref}:{n}", "reminder", URGENT, text, task_id=t.task_id, live_while=live)

    def summarize(self) -> None:
        now = self.now()
        if now - self.summary_from < self.n.summary_every:
            return
        if not self.working() and not self.n.summaries_off_hours:
            return  # it adds up and goes in the morning
        busy = {k: a for k, a in self.activity.items() if a.fresh}
        if not busy:
            return
        since = self.summary_from.astimezone(self.cfg.schedule.tz)
        lines = [f"🔨 *Progress since {since:%H:%M}*"]
        for task_id, a in busy.items():
            parts = []
            if a.calls:
                top = ", ".join(f"{esc(name)} {n}" for name, n in a.tools.most_common(4))
                rest = a.calls - sum(n for _, n in a.tools.most_common(4))
                top += f", other {rest}" if rest > 0 else ""
                parts.append(f"{a.calls} tool calls ({top})")
            if a.errors:
                parts.append(f"{a.errors} came back with an error" if a.calls
                             else f"{a.errors} tool results came back with an error")
            if a.step:
                parts.append(f"step: {esc(a.step)}")
            if a.last_target:
                parts.append(f"last: {esc(clip(a.last_target, 120))}")
            lines.append(f"• {self.line(task_id)} — " + "; ".join(parts))
        self.put(f"summary:{now.timestamp():.0f}", "summary", ROUTINE, "\n".join(lines))
        self.activity = {k: Activity(step=a.step) for k, a in self.activity.items()
                         if k in {t.task_id for t in self.store.tasks(*ACTIVE_STATES)}}
        self.summary_from = now

    def idle(self, situation: str) -> None:
        """Nothing to take: said once per idle spell, after idle_after of it in working hours, with what the agent can
        be given. Waiting on people (a question, a held text) or work ends the spell; not knowing (the queue
        unreadable, health keeping new work out) neither ends it nor counts toward it, so a tracker outage does not
        start a new one. The daily status does not tell it: only the 💤 says what the agent can take."""
        if situation == "unknown":
            return
        if situation != "idle":
            if self.outbox.meta("idle_since"):
                # Over: a 💤 still waiting for Slack would now say something false.
                with self.store.transaction():
                    self.outbox.drop_pending("idle", self.now().timestamp(), "the idle spell ended before it went out")
                    for key in ("idle_since", "idle_told", "idle_repeats"):
                        self.outbox.drop_meta(key)
            return
        now = self.now().timestamp()
        since = float(self.outbox.meta("idle_since") or 0)
        if not since:
            self.outbox.set_meta("idle_since", str(now))
            return
        told = self.outbox.meta("idle_told")
        if told and self.outbox.status_of(told) != "dropped":
            return  # queued, posted or refused by Slack (or done so long ago that it is pruned): told
        # Not told yet, or its 💤 expired or outlived the working day unsent while the spell went on: (again) now.
        if not self.working() or now - since < self.n.idle_after.total_seconds():
            return
        review = [t for t in self.store.tasks("code_review") if not t.people_work]
        waiting = f" {len(review)} task(s) wait in code review with people." if review else ""
        if review and self.cfg.review_agent.enabled:
            waiting += f" `{esc(self.cfg.review_agent.command)}` on their ticket hands the review to the agent."
        tag = self.cfg.tracker.tags.queue
        text = (f"💤 Nothing to take: the queue is empty. Add the `{esc(tag)}` tag to a ticket to start the agent."
                f"{waiting}\n" + self.offer())
        key = f"idle_offer:{since:.0f}:{now:.0f}"
        with self.store.transaction():
            # Only while this spell lasts and the working day does: the sender checks both before it posts.
            self.put(key, "idle", ROUTINE, text,
                     live_while={"meta": "idle_since", "value": self.outbox.meta("idle_since"), "hours": True})
            self.outbox.set_meta("idle_told", key)

    def offer(self) -> str:
        return ("What the agent can take:\n" + "\n".join(f"• {line}" for line in self.can_do())
                + "\nOnly people on the project's list can start it; its questions are answered on the ticket.")

    def can_do(self) -> list[str]:
        """What a person can give the agent, as this project is set up."""
        tags = self.cfg.tracker.tags
        days = self.cfg.limits.continue_window.days
        out = [f"`{esc(tags.queue)}` tag on a ticket: it does the ticket across the group's repositories and opens "
               "merge requests for review",
               f"`{esc(tags.queue)}` with `{esc(tags.research)}`: it investigates and answers on the ticket, no code "
               "changes",
               f"`/continue` in a comment on a failed or stopped ticket (within {days} days): it picks the work up "
               "again, branches kept"]
        r = self.cfg.review_agent
        if r.enabled:
            verdict = ("approves what is ready (with green CI) and sends the rest back to the author"
                       if self.cfg.code_host.approver else
                       "writes its verdict on each one (no approver account is set, so it does not approve)")
            out.append(f"`{esc(r.command)}` (or `@{esc(self.cfg.tracker.bot_user)} review`) in a comment on a ticket in "
                       "Code Review: a separate agent reviews its merge requests against the ticket, builds them and "
                       f"runs the tests without changing the code, then {verdict}")
        else:
            out.append(f"Code review on request (`{esc(r.command)}` on a ticket in Code Review) is off for this "
                       "project: `review_agent.enabled: true` in config.yaml turns it on")
        return out

    def daily(self, situation: str, queue_known: bool = True) -> None:
        """At the first tick of each working day: where everything stands. Also the heartbeat: a working day
        without it means the runner is not running."""
        if not self.n.daily_status or not self.working():
            return
        today = self.now().astimezone(self.cfg.schedule.tz).date().isoformat()
        if self.outbox.meta("daily") == today:
            return
        now = self.now().timestamp()
        lines = [f"☀️ *Status for {self.now().astimezone(self.cfg.schedule.tz):%a %d %b}*"]

        def section(title: str, tasks: list[Task], note: Callable[[Task], str] = lambda t: "") -> None:
            if tasks:
                lines.append(f"{title}:")
                lines.extend(f"• {self.line(t.task_id, t)}{note(t)}" for t in tasks)

        section("In work", self.store.tasks(*ACTIVE_STATES))
        section("Waiting for an answer", self.store.tasks("needs_input"),
                lambda t: f" — asked {ago(now - (t.question_at_ms or 0) / 1000)} ago" if t.question_at_ms else "")
        section("Waiting for a security reviewer", self.store.tasks("security_hold"))
        section("In code review with people", [t for t in self.store.tasks("code_review") if not t.people_work],
                lambda t: f" — {len(json.loads(t.mr_urls or '[]'))} MR(s)")
        section("Failed in the last day", [t for t in self.store.tasks("failed")
                                           if t.failed_at_ms and now - t.failed_at_ms / 1000 < 86400],
                lambda t: f" — {esc(clip(t.reason, 160))}")
        queued = len(self.store.accepted_untaken())
        if situation == "observe":
            lines.append("Observe mode: the runner watches the tracker and takes nothing.")
        elif not queue_known:
            # Not "empty": the tracker does not answer, or a failing check keeps new work out. The tasks above are
            # runner.db's own, still true; the queue is the tracker's, not read.
            lines.insert(1, "_As runner.db last knew it: the tracker is not answering or a health check is failing._")
            lines.append("Queue: not known right now; new tickets are not being taken.")
        elif queued:
            lines.append(f"Queue: {queued} ticket(s) waiting.")
        elif len(lines) == 1:
            lines.append("Nothing in work and nothing queued.")
        else:
            lines.append("Queue: empty.")
        with self.store.transaction():
            self.put(f"daily:{today}", "daily", ROUTINE, "\n".join(lines))
            self.outbox.set_meta("daily", today)


def test_message(webhook: Webhook, project: str) -> str:
    """One message, posted now and outside the outbox: `arceus-runner slack-test`. The verdict's words."""
    posted = webhook.post(f"👋 Arceus can post to this channel. Project {esc(project)} will report here.")
    return "posted" if posted.verdict == "sent" else f"not posted: {posted.verdict}, {posted.detail}"


def build(cfg: Config, store: TaskStore, events: EventLog, webhook_url: str, issue_url: Callable[[str], str],
          now: Callable[[], datetime] = lambda: datetime.now(UTC), transport=None) -> Notifier:
    """The notifier and its sender, not started."""
    notifier = Notifier(cfg, store, events, issue_url, now)
    notifier.sender = Sender(cfg.paths.db, Webhook(webhook_url, cfg.http, transport), cfg.project, events,
                             cfg.http.backoff, schedule=cfg.schedule)
    return notifier

