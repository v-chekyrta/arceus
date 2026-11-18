"""Conversations and messages in one SQLite file. Every conversation is shared by the people allowed in, except a
Slack direct message: only its person sees it."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    claude_session TEXT,
    rebuild INTEGER NOT NULL DEFAULT 0            -- something she had in mind was taken back: start the session anew
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,            -- user | assistant | system
    author TEXT,
    text TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    message_id INTEGER REFERENCES messages(id),   -- NULL while it waits in the composer
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL,          -- pending | ready | failed
    info TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS attachments_by_message ON attachments(message_id);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    message_id INTEGER REFERENCES messages(id),   -- the reply it was proposed in; NULL while that reply is written
    kind TEXT NOT NULL,                           -- bug | feature | research
    summary TEXT NOT NULL,
    description TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft',          -- preparing | draft | creating | created | dismissed
    error TEXT,
    issue_key TEXT,
    issue_url TEXT,
    tagged TEXT,
    board TEXT,                                   -- the board it was put on, or why it was not
    prep_task TEXT,                               -- the Night Shift task writing it (preparing)
    prep_started REAL,
    checked TEXT,                                 -- passed | notes (Codex objected) | unchecked | stale (edited since)
    review_notes TEXT,                            -- Codex's remaining objections, or why it was not checked
    target_key TEXT,                              -- an edit of this existing ticket (NULL: a new ticket)
    target_summary TEXT,                          -- that ticket's title and text when the edit was proposed:
    target_before TEXT,                           --   shown beside the edit, and checked before it is applied
    acted_by TEXT,                                -- who created or dismissed it
    acted_at REAL,
    edited_by TEXT,
    announced INTEGER NOT NULL DEFAULT 0,         -- the assistant has been told it was created
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_by_conversation ON drafts(conversation_id, id);
CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    message_id INTEGER REFERENCES messages(id),   -- her «розберусь» reply; NULL while it is written
    question TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'running',        -- running | found (report in) | failed (why in error) | told
    prep_task TEXT,                               -- the Night Shift task
    prep_started REAL,
    report TEXT,
    checked TEXT,                                 -- passed | notes (Codex objected, in review_notes)
    review_notes TEXT,
    error TEXT,
    answer_id INTEGER,                            -- the message in which she told the person what she found
    tell_error TEXT,                              -- telling it failed: held until a person presses «Спробувати ще раз»
    asked TEXT,                                   -- ids of the people's messages it answers ("12,13"): who asked
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS investigations_by_conversation ON investigations(conversation_id, id);
CREATE TABLE IF NOT EXISTS memory_notes (          -- what she wrote down for herself about the project
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    audience TEXT NOT NULL,                        -- 'project': every chat of the project; 'dm:<email>': that person's DM only
    told_by TEXT,                                  -- whose words it rests on (the turn's authors)
    source TEXT,                                   -- where: 'conv:<id>' or 'slack:<channel>:<ts>'
    channel TEXT,                                  -- the Slack channel it depends on (read there, or said in a thread
                                                   --   of it): usable only while that channel is known to be ours
    state TEXT NOT NULL DEFAULT 'active',          -- active | forgotten
    forgotten_by TEXT,
    forgotten_why TEXT,
    forgotten_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_log (           -- the Slack channels she reads, message by message
    team TEXT NOT NULL,
    channel TEXT NOT NULL,
    ts TEXT NOT NULL,
    thread_ts TEXT,
    user TEXT,
    author TEXT,                                   -- name, filled in once known
    text TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,            -- a tombstone: a deleted message never comes back from an old page
    edited_at REAL,
    PRIMARY KEY (team, channel, ts)
);
CREATE INDEX IF NOT EXISTS channel_log_by_time ON channel_log(channel, ts);
CREATE TABLE IF NOT EXISTS channel_status (        -- whether a channel is ours alone, as Slack last said
    channel TEXT PRIMARY KEY,
    shared INTEGER,                                -- 0: ours only; 1: another company is in it; NULL: not known
    checked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_backfill (      -- reading a channel's history: kept apart from the live messages
    channel TEXT PRIMARY KEY,
    oldest TEXT NOT NULL,                          -- this run reads from here...
    latest TEXT NOT NULL,                          -- ...up to here (fixed when the run starts)
    cursor TEXT,                                   -- the next page; NULL: from the first page
    done INTEGER NOT NULL DEFAULT 0,
    covered_from TEXT                              -- how far back the log reaches; NULL: not recorded (older runs)
);
CREATE TABLE IF NOT EXISTS note_exposures (        -- a note was in front of her in this turn (digest or search)
    note_id INTEGER NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_key TEXT NOT NULL,                        -- the turn: the id of the person's message it answers, or inv<id>
    PRIMARY KEY (note_id, conversation_id, turn_key)
);
CREATE TABLE IF NOT EXISTS turn_quiet (            -- she decided a turn needs no words from her (a shared thread only)
    conversation_id TEXT NOT NULL,
    turn_key TEXT NOT NULL,                        -- the id of the last person's message the turn answers
    reaction TEXT,                                 -- an emoji name to put on that message, or NULL: just silence
    why TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, turn_key)
);
CREATE TABLE IF NOT EXISTS escalations (           -- she called the expert (the expert): something risky or past her
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    reason TEXT NOT NULL,                          -- her words for him: what is asked and why it is his call
    requester TEXT,                                -- who asked
    asked TEXT,                                    -- what they wrote
    state TEXT NOT NULL DEFAULT 'new',             -- new | sent (he has it in Slack)
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS capability_requests (   -- she asked the expert for something she lacks (request_capability)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,                      -- one need, one request: repeats only count
    project TEXT,
    need TEXT NOT NULL,                            -- what to give her
    why TEXT NOT NULL,                             -- what it was for, the latest time
    source TEXT,                                   -- the link that did not open, if any
    error TEXT,                                    -- what answered, secrets taken out
    requester TEXT,                                -- who she was helping
    conversation_id TEXT,                          -- where: never a key to it for the expert
    times INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'new',             -- new | sent
    created_at REAL NOT NULL,
    sent_at REAL
);
CREATE TABLE IF NOT EXISTS slack_links (           -- a conversation that lives in Slack
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
    team TEXT NOT NULL,
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,                       -- '' for a direct message: the whole DM is one conversation
    owner TEXT,                                    -- a DM's person (only they see it in the web chat); NULL: a thread
    posted_upto INTEGER NOT NULL DEFAULT 0,        -- her messages up to this id are in Slack
    posted_parts INTEGER NOT NULL DEFAULT 0,       -- pieces of the next long message already in Slack
    UNIQUE (team, channel, thread_ts)
);
CREATE TABLE IF NOT EXISTS slack_inbox (           -- every Slack message taken, before Slack is told it arrived
    team TEXT NOT NULL,
    channel TEXT NOT NULL,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,                           -- the event as Slack sent it, to finish taking it after a restart
    state TEXT NOT NULL DEFAULT 'received',        -- received | stored | refused
    conversation_id TEXT,
    message_id INTEGER,
    reacted INTEGER NOT NULL DEFAULT 0,            -- 👀 is on it, until her answer is posted
    created_at REAL NOT NULL,
    PRIMARY KEY (team, channel, ts)                -- the same message twice (a resend, or as mention and message): once
);
CREATE TABLE IF NOT EXISTS slack_creators (        -- who may ask for tickets because Slack says they are in a channel
    email TEXT PRIMARY KEY,                        -- of slack.creator_channels with an e-mail of slack.creator_domain
    refreshed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS slack_cards (           -- a draft's card in Slack
    draft_id INTEGER PRIMARY KEY REFERENCES drafts(id),
    channel TEXT NOT NULL,
    ts TEXT NOT NULL,
    shown TEXT NOT NULL                            -- what the card shows now: state and error
);
"""

ADDED_DRAFT_COLUMNS = (("board", "TEXT"), ("prep_task", "TEXT"), ("prep_started", "REAL"), ("checked", "TEXT"),
                       ("review_notes", "TEXT"), ("target_key", "TEXT"), ("target_summary", "TEXT"),
                       ("target_before", "TEXT"))
DROPPED_DRAFT_COLUMNS = ("work", "work_by", "queued_as")   # «У роботу» from the assistant, replaced by the manager's own


@dataclass
class Message:
    id: int
    conversation_id: str
    role: str
    author: str | None
    text: str
    meta: dict = field(default_factory=dict)
    created_at: float = 0.0
    attachments: list["Attachment"] = field(default_factory=list)
    drafts: list["Draft"] = field(default_factory=list)
    investigations: list["Investigation"] = field(default_factory=list)

    def public(self) -> dict:
        return {"id": self.id, "role": self.role, "author": self.author, "text": self.text,
                "meta": self.meta, "created_at": self.created_at,
                "attachments": [a.public() for a in self.attachments],
                "drafts": [d.public() for d in self.drafts],
                "investigations": [i.public() for i in self.investigations]}


@dataclass
class Investigation:
    """«Розберусь»: a question she sent to Night Shift; its report comes back into the conversation as her message."""
    id: int
    conversation_id: str
    message_id: int | None
    question: str
    state: str
    prep_task: str | None
    prep_started: float | None
    report: str | None
    checked: str | None
    review_notes: str | None
    error: str | None
    answer_id: int | None
    tell_error: str | None
    created_at: float
    asked: str | None = None

    @property
    def asked_ids(self) -> tuple[int, ...]:
        return tuple(int(i) for i in (self.asked or "").split(",") if i.strip().isdigit())

    def public(self) -> dict:
        # The report is for her and for the ticket; the person reads her answer, not the report.
        return {"id": self.id, "question": self.question, "state": self.state, "prep_started": self.prep_started,
                "created_at": self.created_at, "answer_id": self.answer_id}


@dataclass
class Draft:
    id: int
    conversation_id: str
    message_id: int | None
    kind: str
    summary: str
    description: str
    state: str
    error: str | None
    issue_key: str | None
    issue_url: str | None
    tagged: str | None
    board: str | None
    prep_task: str | None
    prep_started: float | None
    checked: str | None
    review_notes: str | None
    target_key: str | None
    target_summary: str | None
    target_before: str | None
    acted_by: str | None
    acted_at: float | None
    edited_by: str | None
    announced: int
    created_at: float

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "summary": self.summary, "description": self.description,
                "state": self.state, "error": self.error, "issue_key": self.issue_key, "issue_url": self.issue_url,
                "tagged": self.tagged, "board": self.board, "checked": self.checked,
                "review_notes": self.review_notes, "prep_started": self.prep_started,
                "target_key": self.target_key, "target_summary": self.target_summary,
                "target_before": self.target_before, "acted_by": self.acted_by, "acted_at": self.acted_at,
                "edited_by": self.edited_by}


@dataclass
class Attachment:
    id: str
    conversation_id: str
    message_id: int | None
    name: str
    mime: str
    size: int
    kind: str
    path: str
    status: str
    info: dict
    created_by: str
    created_at: float

    def public(self) -> dict:
        """What the browser gets: never the path on the server."""
        info = self.info
        url = f"/assistant/api/files/{self.id}"
        # A picture the browser can show: the file itself, or the PNG made from a BMP/TIFF/HEIC/AVIF one.
        view = None
        if self.kind == "image" and info.get("view_path"):
            view = url if info["view_path"] == self.path else f"{url}/view"
        return {"id": self.id, "name": self.name, "mime": self.mime, "size": self.size, "kind": self.kind,
                "status": self.status, "transcript": info.get("transcript"), "note": info.get("note", ""),
                "duration_s": info.get("duration_s"), "url": url, "view_url": view}


@dataclass
class SlackLink:
    conversation_id: str
    team: str
    channel: str
    thread_ts: str
    owner: str | None
    posted_upto: int
    posted_parts: int = 0


@dataclass
class Conversation:
    id: str
    title: str
    created_by: str
    created_at: float
    updated_at: float
    claude_session: str | None
    rebuild: int = 0

    def public(self) -> dict:
        return {"id": self.id, "title": self.title, "created_by": self.created_by,
                "created_at": self.created_at, "updated_at": self.updated_at}


class _Result:
    """A statement's outcome, read in full while the connection was held: no cursor outlives the lock."""

    def __init__(self, cur: sqlite3.Cursor):
        self._rows = cur.fetchall()
        self.rowcount, self.lastrowid = cur.rowcount, cur.lastrowid

    def fetchall(self) -> list:
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _Serialized:
    """One SQLite connection shared by the web handlers and the background ticket loop (a worker thread): every
    statement, and the reading of its rows, happens under one lock. Reads used to run unguarded beside a write in
    another thread, and SQLite answered «bad parameter or other API misuse»."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn, self._lock = conn, lock

    def execute(self, sql: str, args: tuple = ()) -> _Result:
        with self._lock:
            return _Result(self._conn.execute(sql, args))

    def executescript(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # SQLite's lower() knows only ASCII: «Сертифікати» would never match «сертифікати».
        conn.create_function("ufold", 1, lambda v: v.casefold() if isinstance(v, str) else v, deterministic=True)
        # Re-entrant: the store's own «with self._lock» blocks run statements, which take it again.
        self._lock = threading.RLock()
        self._db = _Serialized(conn, self._lock)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Columns added after a database was first made (CREATE TABLE IF NOT EXISTS leaves old tables as they are)."""
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(drafts)")}
        for name, decl in ADDED_DRAFT_COLUMNS:
            if name not in have:
                self._db.execute(f"ALTER TABLE drafts ADD COLUMN {name} {decl}")
        inv_have = {r["name"] for r in self._db.execute("PRAGMA table_info(investigations)")}
        if "tell_error" not in inv_have:
            self._db.execute("ALTER TABLE investigations ADD COLUMN tell_error TEXT")
        if "asked" not in inv_have:
            self._db.execute("ALTER TABLE investigations ADD COLUMN asked TEXT")
        conv_have = {r["name"] for r in self._db.execute("PRAGMA table_info(conversations)")}
        if "rebuild" not in conv_have:
            self._db.execute("ALTER TABLE conversations ADD COLUMN rebuild INTEGER NOT NULL DEFAULT 0")
        note_have = {r["name"] for r in self._db.execute("PRAGMA table_info(memory_notes)")}
        if "channel" not in note_have:
            self._db.execute("ALTER TABLE memory_notes ADD COLUMN channel TEXT")
        bf_have = {r["name"] for r in self._db.execute("PRAGMA table_info(channel_backfill)")}
        if "covered_from" not in bf_have:
            self._db.execute("ALTER TABLE channel_backfill ADD COLUMN covered_from TEXT")
        link_have = {r["name"] for r in self._db.execute("PRAGMA table_info(slack_links)")}
        if "posted_parts" not in link_have:
            self._db.execute("ALTER TABLE slack_links ADD COLUMN posted_parts INTEGER NOT NULL DEFAULT 0")
        for name in DROPPED_DRAFT_COLUMNS:
            if name in have:
                self._db.execute(f"ALTER TABLE drafts DROP COLUMN {name}")

    def create_conversation(self, created_by: str, title: str = "Нова розмова") -> Conversation:
        now = time.time()
        conv = Conversation(uuid.uuid4().hex, title, created_by, now, now, None)
        with self._lock:
            self._db.execute("INSERT INTO conversations (id, title, created_by, created_at, updated_at, "
                             "claude_session) VALUES (?,?,?,?,?,?)",
                             (conv.id, conv.title, conv.created_by, now, now, None))
        return conv

    def conversations(self) -> list[Conversation]:
        rows = self._db.execute("SELECT * FROM conversations ORDER BY updated_at DESC").fetchall()
        return [Conversation(**dict(r)) for r in rows]

    def conversation(self, conv_id: str) -> Conversation | None:
        row = self._db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        return Conversation(**dict(row)) if row else None

    def set_session(self, conv_id: str, session: str) -> None:
        with self._lock:
            self._db.execute("UPDATE conversations SET claude_session=? WHERE id=?", (session, conv_id))

    def rename(self, conv_id: str, title: str) -> None:
        with self._lock:
            self._db.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))

    def add_message(self, conv_id: str, role: str, text: str, author: str | None = None,
                    meta: dict | None = None) -> Message:
        now = time.time()
        meta = meta or {}
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages (conversation_id, role, author, text, meta, created_at) VALUES (?,?,?,?,?,?)",
                (conv_id, role, author, text, json.dumps(meta, ensure_ascii=False), now))
            self._db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        return Message(cur.lastrowid, conv_id, role, author, text, meta, now)

    def messages(self, conv_id: str) -> list[Message]:
        rows = self._db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,)).fetchall()
        by_message: dict[int, list[Attachment]] = {}
        for a in self._attachments("conversation_id=? AND message_id IS NOT NULL", (conv_id,)):
            by_message.setdefault(a.message_id, []).append(a)
        drafts: dict[int, list[Draft]] = {}
        for dr in self._drafts("conversation_id=? AND message_id IS NOT NULL", (conv_id,)):
            drafts.setdefault(dr.message_id, []).append(dr)
        found: dict[int, list[Investigation]] = {}
        for inv in self._investigations("conversation_id=? AND message_id IS NOT NULL", (conv_id,)):
            found.setdefault(inv.message_id, []).append(inv)
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d["meta"] or "{}")
            out.append(Message(**d, attachments=by_message.get(d["id"], []), drafts=drafts.get(d["id"], []),
                               investigations=found.get(d["id"], [])))
        return out

    # investigations ----------------------------------------------------------------------------------------------
    def _investigations(self, where: str, args: tuple) -> list[Investigation]:
        rows = self._db.execute(f"SELECT * FROM investigations WHERE {where} ORDER BY id", args).fetchall()
        return [Investigation(**dict(r)) for r in rows]

    def investigation(self, inv_id: int) -> Investigation | None:
        found = self._investigations("id=?", (inv_id,))
        return found[0] if found else None

    def add_investigation(self, conv_id: str, question: str, asked: tuple[int, ...] | list[int] = ()) -> Investigation:
        """`asked`: the people's messages the turn that started it answered; telling the finding answers them."""
        with self._lock:
            cur = self._db.execute("INSERT INTO investigations (conversation_id, question, asked, created_at) "
                                   "VALUES (?,?,?,?)", (conv_id, question, ",".join(str(i) for i in asked) or None,
                                                         time.time()))
        return self.investigation(cur.lastrowid)

    def attach_investigations(self, conv_id: str, message_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE investigations SET message_id=? WHERE conversation_id=? AND message_id IS NULL",
                             (message_id, conv_id))

    def investigations_in(self, state: str) -> list[Investigation]:
        return self._investigations("state=?", (state,))

    def investigation_started(self, inv_id: int, task: str) -> None:
        with self._lock:
            self._db.execute("UPDATE investigations SET prep_task=?, prep_started=? WHERE id=? AND state='running'",
                             (task, time.time(), inv_id))

    def investigation_found(self, inv_id: int, report: str, checked: str, notes: str) -> None:
        with self._lock:
            self._db.execute("UPDATE investigations SET state='found', report=?, checked=?, review_notes=? "
                             "WHERE id=? AND state='running'", (report, checked, notes, inv_id))

    def investigation_failed(self, inv_id: int, error: str) -> None:
        with self._lock:
            self._db.execute("UPDATE investigations SET state='failed', error=? WHERE id=? AND state='running'",
                             (error, inv_id))

    def investigation_told(self, inv_id: int, answer_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE investigations SET state='told', answer_id=?, tell_error=NULL WHERE id=?",
                             (answer_id, inv_id))

    def investigation_tell_failed(self, inv_id: int, error: str) -> None:
        """The finding stays (found or failed) and is not told again by itself: the person retries it."""
        with self._lock:
            self._db.execute("UPDATE investigations SET tell_error=? WHERE id=? AND state IN ('found','failed')",
                             (error, inv_id))

    def investigation_retell(self, inv_id: int) -> bool:
        with self._lock:
            n = self._db.execute("UPDATE investigations SET tell_error=NULL WHERE id=? AND state IN ('found','failed') "
                                 "AND tell_error IS NOT NULL", (inv_id,)).rowcount
        return n == 1

    def to_tell(self) -> list["Investigation"]:
        return self._investigations("state IN ('found','failed') AND tell_error IS NULL", ())

    def message(self, message_id: int) -> Message | None:
        row = self._db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["meta"] = json.loads(d["meta"] or "{}")
        return Message(**d, attachments=self._attachments("message_id=?", (message_id,)))

    # ticket drafts -----------------------------------------------------------------------------------------------
    def _drafts(self, where: str, args: tuple) -> list[Draft]:
        rows = self._db.execute(f"SELECT * FROM drafts WHERE {where} ORDER BY id", args).fetchall()
        return [Draft(**dict(r)) for r in rows]

    def add_draft(self, conv_id: str, kind: str, summary: str, description: str, preparing: bool = False,
                  target: dict | None = None) -> Draft:
        """Written by the tools process while a reply is still being written: it joins that reply when it is stored.
        `preparing`: the ticket is still to be written by Night Shift; this text is only her brief.
        `target`: an edit of an existing ticket ({key, summary, description} as it is now)."""
        t = target or {}
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO drafts (conversation_id, kind, summary, description, state, target_key, target_summary, "
                "target_before, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (conv_id, kind, summary, description, "preparing" if preparing else "draft", t.get("key"),
                 t.get("summary"), t.get("description"), time.time()))
        return self.draft(cur.lastrowid)

    def preparing(self) -> list[Draft]:
        return self._drafts("state='preparing'", ())

    def prep_started_as(self, draft_id: int, task: str) -> None:
        with self._lock:
            self._db.execute("UPDATE drafts SET prep_task=?, prep_started=? WHERE id=? AND state='preparing'",
                             (task, time.time(), draft_id))

    def prep_finished(self, draft_id: int, checked: str, notes: str, summary: str | None = None,
                      description: str | None = None) -> bool:
        """The ticket Night Shift wrote replaces her brief; without one, her brief stays, marked unchecked. A draft
        put aside meanwhile stays put aside."""
        with self._lock:
            n = self._db.execute("UPDATE drafts SET state='draft', checked=?, review_notes=?, "
                                 "summary=COALESCE(?, summary), description=COALESCE(?, description) "
                                 "WHERE id=? AND state='preparing'",
                                 (checked, notes, summary, description, draft_id)).rowcount
        return n == 1

    def draft(self, draft_id: int) -> Draft | None:
        found = self._drafts("id=?", (draft_id,))
        return found[0] if found else None

    def attach_drafts(self, conv_id: str, message_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE drafts SET message_id=? WHERE conversation_id=? AND message_id IS NULL",
                             (message_id, conv_id))

    def edit_draft(self, draft_id: int, kind: str, summary: str, description: str, who: str) -> bool:
        """A changed title or text is no longer what Codex reviewed: its check becomes stale (its notes stay, as
        notes on the earlier version)."""
        with self._lock:
            n = self._db.execute("UPDATE drafts SET checked=CASE WHEN checked IN ('passed','notes') "
                                 "AND (summary IS NOT ? OR description IS NOT ?) THEN 'stale' ELSE checked END, "
                                 "kind=?, summary=?, description=?, edited_by=?, error=NULL "
                                 "WHERE id=? AND state='draft'",
                                 (summary, description, kind, summary, description, who, draft_id)).rowcount
        return n == 1

    def claim_draft(self, draft_id: int) -> bool:
        """Only one press creates the ticket: the draft moves to `creating` once, atomically."""
        with self._lock:
            n = self._db.execute("UPDATE drafts SET state='creating', error=NULL WHERE id=? AND state='draft'",
                                 (draft_id,)).rowcount
        return n == 1

    def draft_failed(self, draft_id: int, error: str) -> None:
        with self._lock:
            self._db.execute("UPDATE drafts SET state='draft', error=? WHERE id=?", (error, draft_id))

    def draft_created(self, draft_id: int, key: str, url: str, tagged: str | None, who: str,
                      board: str | None = None) -> None:
        with self._lock:
            self._db.execute("UPDATE drafts SET state='created', issue_key=?, issue_url=?, tagged=?, board=?, "
                             "acted_by=?, acted_at=?, error=NULL WHERE id=?",
                             (key, url, tagged, board, who, time.time(), draft_id))

    def dismiss_draft(self, draft_id: int, who: str) -> bool:
        with self._lock:
            n = self._db.execute("UPDATE drafts SET state='dismissed', acted_by=?, acted_at=? "
                                 "WHERE id=? AND state IN ('draft','preparing')", (who, time.time(), draft_id)).rowcount
        return n == 1

    def reset_stuck_drafts(self) -> list[Draft]:
        """A restart in the middle of creating: the ticket may or may not exist. Back to a draft, saying so."""
        stuck = self._drafts("state='creating'", ())
        with self._lock:
            for d in stuck:
                self._db.execute("UPDATE drafts SET state='draft', error=? WHERE id=?", (
                    "Створення перервалося перезапуском асистента. Натисніть «Створити тікет» ще раз: якщо тікет "
                    "уже встиг створитися, асистент його знайде й другого не створить.", d.id))
        return stuck

    def unannounced(self, conv_id: str) -> list[Draft]:
        return self._drafts("conversation_id=? AND state IN ('created','dismissed') AND announced=0", (conv_id,))

    def mark_announced(self, ids: list[int]) -> None:
        with self._lock:
            for i in ids:
                self._db.execute("UPDATE drafts SET announced=1 WHERE id=?", (i,))

    # attachments -------------------------------------------------------------------------------------------------
    def _attachments(self, where: str, args: tuple) -> list[Attachment]:
        rows = self._db.execute(f"SELECT * FROM attachments WHERE {where} ORDER BY created_at, id", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["info"] = json.loads(d["info"] or "{}")
            out.append(Attachment(**d))
        return out

    def add_attachment(self, a: Attachment) -> None:
        with self._lock:
            self._db.execute("INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (a.id, a.conversation_id, a.message_id, a.name, a.mime, a.size, a.kind, a.path,
                              a.status, json.dumps(a.info, ensure_ascii=False), a.created_by, a.created_at))

    def attachment(self, aid: str) -> Attachment | None:
        found = self._attachments("id=?", (aid,))
        return found[0] if found else None

    def pending_attachments(self) -> list[Attachment]:
        return self._attachments("status='pending'", ())

    def finish_attachment(self, aid: str, status: str, kind: str, info: dict) -> None:
        with self._lock:
            self._db.execute("UPDATE attachments SET status=?, kind=?, info=? WHERE id=?",
                             (status, kind, json.dumps(info, ensure_ascii=False), aid))

    def delete_attachment(self, aid: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM attachments WHERE id=? AND message_id IS NULL", (aid,))

    def add_user_message(self, conv_id: str, text: str, author: str, attachment_ids: list[str],
                         meta: dict | None = None) -> Message:
        """The message and its attachments in one transaction: a file is never half-attached."""
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cur = self._db.execute(
                    "INSERT INTO messages (conversation_id, role, author, text, meta, created_at) VALUES (?,?,?,?,?,?)",
                    (conv_id, "user", author, text, json.dumps(meta or {}, ensure_ascii=False), now))
                mid = cur.lastrowid
                for aid in attachment_ids:
                    n = self._db.execute("UPDATE attachments SET message_id=? WHERE id=? AND conversation_id=? "
                                         "AND message_id IS NULL", (mid, aid, conv_id)).rowcount
                    if n != 1:
                        raise ValueError(aid)
                self._db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        msg = self.message(mid)
        assert msg is not None
        return msg

    # slack -------------------------------------------------------------------------------------------------------
    def slack_link(self, conv_id: str) -> SlackLink | None:
        row = self._db.execute("SELECT * FROM slack_links WHERE conversation_id=?", (conv_id,)).fetchone()
        return SlackLink(**dict(row)) if row else None

    def slack_links(self) -> list[SlackLink]:
        return [SlackLink(**dict(r)) for r in self._db.execute("SELECT * FROM slack_links").fetchall()]

    def slack_conversation(self, team: str, channel: str, thread_ts: str) -> str | None:
        row = self._db.execute("SELECT conversation_id FROM slack_links WHERE team=? AND channel=? AND thread_ts=?",
                               (team, channel, thread_ts)).fetchone()
        return row["conversation_id"] if row else None

    def link_slack(self, team: str, channel: str, thread_ts: str, owner: str | None, created_by: str,
                   title: str) -> str:
        """The conversation of a DM or a thread, made the first time: two events racing for it get the same one."""
        with self._lock:
            found = self.slack_conversation(team, channel, thread_ts)
            if found:
                return found
            conv = self.create_conversation(created_by, title)
            self._db.execute("INSERT INTO slack_links (conversation_id, team, channel, thread_ts, owner) "
                             "VALUES (?,?,?,?,?)", (conv.id, team, channel, thread_ts, owner))
            return conv.id

    def slack_posted(self, conv_id: str, message_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE slack_links SET posted_upto=MAX(posted_upto, ?), posted_parts=0 "
                             "WHERE conversation_id=?", (message_id, conv_id))

    def slack_posted_part(self, conv_id: str, message_id: int, parts: int) -> None:
        """`parts` pieces of message `message_id` are in Slack: a retry goes on from the next one."""
        with self._lock:
            self._db.execute("UPDATE slack_links SET posted_parts=? WHERE conversation_id=? AND posted_upto < ?",
                             (parts, conv_id, message_id))

    def slack_take(self, team: str, channel: str, ts: str, event: dict) -> bool:
        """False: this message was taken already."""
        with self._lock:
            n = self._db.execute("INSERT OR IGNORE INTO slack_inbox (team, channel, ts, event, created_at) "
                                 "VALUES (?,?,?,?,?)", (team, channel, ts, json.dumps(event, ensure_ascii=False),
                                                        time.time())).rowcount
        return n == 1

    def slack_received(self) -> list[dict]:
        """Taken but not yet stored as a message: a restart came in between."""
        rows = self._db.execute("SELECT * FROM slack_inbox WHERE state='received' ORDER BY created_at").fetchall()
        return [{**dict(r), "event": json.loads(r["event"])} for r in rows]

    def slack_settle(self, team: str, channel: str, ts: str, state: str, conv_id: str | None = None,
                     message_id: int | None = None) -> None:
        with self._lock:
            self._db.execute("UPDATE slack_inbox SET state=?, conversation_id=?, message_id=? "
                             "WHERE team=? AND channel=? AND ts=?", (state, conv_id, message_id, team, channel, ts))

    def slack_reacted(self, team: str, channel: str, ts: str, on: bool) -> None:
        with self._lock:
            self._db.execute("UPDATE slack_inbox SET reacted=? WHERE team=? AND channel=? AND ts=?",
                             (1 if on else 0, team, channel, ts))

    def slack_waiting_eyes(self, conv_id: str, answered: list[int]) -> list[dict]:
        """Messages with 👀 that her reply answered (exactly those: one sent while she was writing waits for the next)."""
        if not answered:
            return []
        rows = self._db.execute(f"SELECT team, channel, ts FROM slack_inbox WHERE conversation_id=? AND reacted=1 "
                                f"AND message_id IN ({','.join('?' * len(answered))})",
                                (conv_id, *answered)).fetchall()
        return [dict(r) for r in rows]

    def slack_card(self, draft_id: int) -> dict | None:
        row = self._db.execute("SELECT * FROM slack_cards WHERE draft_id=?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def slack_card_shown(self, draft_id: int, channel: str, ts: str, shown: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO slack_cards (draft_id, channel, ts, shown) VALUES (?,?,?,?) "
                             "ON CONFLICT(draft_id) DO UPDATE SET shown=excluded.shown", (draft_id, channel, ts, shown))

    def drafts_of(self, conv_id: str) -> list[Draft]:
        return self._drafts("conversation_id=?", (conv_id,))

    # escalations -------------------------------------------------------------------------------------------------
    def last_user_message(self, conv_id: str) -> Message | None:
        row = self._db.execute("SELECT id FROM messages WHERE conversation_id=? AND role='user' ORDER BY id DESC "
                               "LIMIT 1", (conv_id,)).fetchone()
        return self.message(row["id"]) if row else None

    def add_escalation(self, conv_id: str, reason: str, requester: str | None, asked: str | None) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO escalations (conversation_id, reason, requester, asked, created_at) "
                                   "VALUES (?,?,?,?,?)", (conv_id, reason, requester, asked, time.time()))
        return cur.lastrowid

    def escalations_new(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM escalations WHERE state='new' ORDER BY id")]

    def escalation_sent(self, esc_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE escalations SET state='sent' WHERE id=?", (esc_id,))

    def escalated(self, conv_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM escalations WHERE conversation_id=? LIMIT 1",
                                (conv_id,)).fetchone() is not None

    REMIND_AFTER_S = 7 * 86400   # a need still there a week after the expert was told: told again

    def add_capability_request(self, key: str, project: str, need: str, why: str, source: str, error: str,
                               requester: str | None, conv_id: str | None) -> tuple[int, bool]:
        """(id, whether the expert will hear of it now). The same need again only counts, unless he was told a week ago.
        One write transaction: the server and a turn's tools process may file the same need at once."""
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM capability_requests WHERE key=?", (key,)).fetchone()
                if row is None:
                    cur = self._db.execute(
                        "INSERT INTO capability_requests (key, project, need, why, source, error, requester, "
                        "conversation_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (key, project, need, why, source or None, error or None, requester, conv_id, now))
                    out = (cur.lastrowid, True)
                else:
                    again = row["state"] == "sent" and now - (row["sent_at"] or 0) > self.REMIND_AFTER_S
                    self._db.execute(
                        "UPDATE capability_requests SET times=times+1, why=?, source=COALESCE(?, source), "
                        "error=COALESCE(?, error), requester=?, state=CASE WHEN ? THEN 'new' ELSE state END "
                        "WHERE id=?", (why, source or None, error or None, requester, int(again), row["id"]))
                    out = (row["id"], again)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return out

    def capability_requests_new(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM capability_requests WHERE state='new' ORDER BY id")]

    def capability_request_sent(self, req_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE capability_requests SET state='sent', sent_at=? WHERE id=?", (time.time(), req_id))

    # ticket creators from Slack -----------------------------------------------------------------------------------
    def set_slack_creators(self, emails: set[str]) -> None:
        """The whole list as Slack gave it now: who left the channel loses the right with this."""
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM slack_creators")
                for e in sorted({e.lower() for e in emails}):
                    self._db.execute("INSERT INTO slack_creators (email, refreshed_at) VALUES (?, ?)", (e, now))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def slack_creators(self) -> set[str]:
        return {r["email"] for r in self._db.execute("SELECT email FROM slack_creators")}

    # memory ------------------------------------------------------------------------------------------------------
    def add_note(self, text: str, topic: str, audience: str, told_by: str | None, source: str | None,
                 channel: str | None = None) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO memory_notes (text, topic, audience, told_by, source, channel, "
                                   "created_at) VALUES (?,?,?,?,?,?,?)",
                                   (text, topic, audience, told_by, source, channel, time.time()))
        return cur.lastrowid

    def notes(self, audiences: list[str], query: str = "", limit: int = 30) -> list[dict]:
        """Active notes for these audiences, newest first; every word of `query` must appear (any case). A note that
        depends on a Slack channel only while Slack last said that channel is ours alone."""
        if not audiences:
            return []
        where = (f"state='active' AND audience IN ({','.join('?' * len(audiences))}) AND (channel IS NULL OR "
                 "channel IN (SELECT channel FROM channel_status WHERE shared=0))")
        args: list = list(audiences)
        for w in query.casefold().split()[:8]:
            where += " AND (ufold(text) LIKE ? OR ufold(topic) LIKE ?)"
            args += [f"%{w}%", f"%{w}%"]
        rows = self._db.execute(f"SELECT * FROM memory_notes WHERE {where} ORDER BY id DESC LIMIT ?",
                                (*args, limit)).fetchall()
        return [dict(r) for r in rows]

    def note(self, note_id: int) -> dict | None:
        row = self._db.execute("SELECT * FROM memory_notes WHERE id=?", (note_id,)).fetchone()
        return dict(row) if row else None

    def count_notes(self, audience: str) -> int:
        return self._db.execute("SELECT count(*) FROM memory_notes WHERE state='active' AND audience=?",
                                (audience,)).fetchone()[0]

    def _forget(self, where: str, args: tuple, who: str, why: str) -> int:
        """Take notes back, and make every conversation that had one of them in front of her start its session anew,
        without the replies she wrote with it (see exposed_turns): a resumed session would still remember it."""
        with self._lock:
            ids = [r["id"] for r in self._db.execute(f"SELECT id FROM memory_notes WHERE state='active' AND {where}",
                                                     args)]
            for nid in ids:
                self._db.execute("UPDATE memory_notes SET state='forgotten', forgotten_by=?, forgotten_why=?, "
                                 "forgotten_at=? WHERE id=?", (who, why, time.time(), nid))
                self._db.execute("UPDATE conversations SET rebuild=1 WHERE id IN (SELECT conversation_id FROM "
                                 "note_exposures WHERE note_id=?)", (nid,))
        return len(ids)

    def forget_note(self, note_id: int, who: str, why: str) -> bool:
        return self._forget("id=?", (note_id,), who, why) == 1

    def forget_notes_from(self, source: str, why: str) -> int:
        """A source changed or was deleted (a Slack message): what she wrote down from it goes too."""
        return self._forget("source=?", (source,), "slack", why)

    def forget_channel_notes(self, channel: str, why: str) -> int:
        """The channel is shared with another company now: nothing she took from it, or was told in it, stays."""
        return self._forget("channel=?", (channel,), "slack", why)

    def expose(self, note_ids: list[int], conv_id: str, turn_key: str) -> None:
        with self._lock:
            for nid in note_ids:
                self._db.execute("INSERT OR IGNORE INTO note_exposures VALUES (?,?,?)", (nid, conv_id, turn_key))

    def exposed_turns(self, conv_id: str) -> set[str]:
        """Turns of this conversation that had a note in front of her that has been taken back since."""
        rows = self._db.execute("SELECT e.turn_key FROM note_exposures e JOIN memory_notes n ON n.id=e.note_id "
                                "WHERE e.conversation_id=? AND n.state='forgotten'", (conv_id,)).fetchall()
        return {r["turn_key"] for r in rows}

    def rebuilt(self, conv_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE conversations SET rebuild=0 WHERE id=?", (conv_id,))

    # whether a channel is ours -----------------------------------------------------------------------------------
    def set_channel_status(self, channel: str, shared: bool | None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO channel_status VALUES (?,?,?) ON CONFLICT(channel) DO UPDATE SET "
                             "shared=excluded.shared, checked_at=excluded.checked_at",
                             (channel, None if shared is None else int(shared), time.time()))

    def channel_shared(self, channel: str) -> bool | None:
        row = self._db.execute("SELECT shared FROM channel_status WHERE channel=?", (channel,)).fetchone()
        return None if row is None or row["shared"] is None else bool(row["shared"])

    def channels_ours(self, channels: list[str] | tuple) -> list[str]:
        """Of these, the channels Slack last said are ours alone."""
        if not channels:
            return []
        rows = self._db.execute(f"SELECT channel FROM channel_status WHERE shared=0 AND channel IN "
                                f"({','.join('?' * len(channels))})", tuple(channels)).fetchall()
        return [r["channel"] for r in rows]

    # reading a channel's history ---------------------------------------------------------------------------------
    def backfill_state(self, channel: str) -> dict | None:
        row = self._db.execute("SELECT * FROM channel_backfill WHERE channel=?", (channel,)).fetchone()
        return dict(row) if row else None

    def backfill_start(self, channel: str, oldest: str, latest: str, covered_from: str | None = None) -> None:
        """A new window. `covered_from`: when this run reads the whole history it is to reach (else kept)."""
        with self._lock:
            self._db.execute("INSERT INTO channel_backfill (channel, oldest, latest, cursor, done, covered_from) "
                             "VALUES (?,?,?,NULL,0,?) ON CONFLICT(channel) DO UPDATE SET oldest=excluded.oldest, "
                             "latest=excluded.latest, cursor=NULL, done=0, "
                             "covered_from=COALESCE(excluded.covered_from, channel_backfill.covered_from)",
                             (channel, oldest, latest, covered_from))

    def backfill_page(self, channel: str, cursor: str | None) -> None:
        with self._lock:
            self._db.execute("UPDATE channel_backfill SET cursor=?, done=? WHERE channel=?",
                             (cursor, 0 if cursor else 1, channel))

    def forgotten_since(self, since: float, audiences: list[str]) -> list[dict]:
        if not audiences:
            return []
        rows = self._db.execute(f"SELECT * FROM memory_notes WHERE state='forgotten' AND forgotten_at>? AND audience "
                                f"IN ({','.join('?' * len(audiences))}) ORDER BY forgotten_at LIMIT 20",
                                (since, *audiences)).fetchall()
        return [dict(r) for r in rows]

    # channels she reads ------------------------------------------------------------------------------------------
    def log_message(self, team: str, channel: str, ts: str, thread_ts: str | None, user: str | None,
                    text: str) -> bool:
        """A message of a channel she reads. A deleted one stays deleted, whatever page of history brings it back."""
        with self._lock:
            n = self._db.execute(
                "INSERT INTO channel_log (team, channel, ts, thread_ts, user, text) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(team, channel, ts) DO UPDATE SET text=excluded.text WHERE channel_log.deleted=0 "
                "AND channel_log.text<>excluded.text AND channel_log.edited_at IS NULL",
                (team, channel, ts, thread_ts, user, text)).rowcount
        return n == 1

    def edit_logged(self, team: str, channel: str, ts: str, text: str) -> None:
        with self._lock:
            self._db.execute("UPDATE channel_log SET text=?, edited_at=? WHERE team=? AND channel=? AND ts=? "
                             "AND deleted=0", (text, time.time(), team, channel, ts))

    def retext_logged(self, team: str, channel: str, ts: str, raw_text: str, text: str) -> None:
        """The same message with names in place of Slack ids: only while it still holds the text it was made from
        (an edit or a deletion meanwhile wins)."""
        with self._lock:
            self._db.execute("UPDATE channel_log SET text=? WHERE team=? AND channel=? AND ts=? AND deleted=0 "
                             "AND text=?", (text, team, channel, ts, raw_text))

    def delete_logged(self, team: str, channel: str, ts: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO channel_log (team, channel, ts, text, deleted) VALUES (?,?,?,'',1) "
                             "ON CONFLICT(team, channel, ts) DO UPDATE SET text='', deleted=1", (team, channel, ts))

    def name_logged(self, user: str, name: str) -> None:
        with self._lock:
            self._db.execute("UPDATE channel_log SET author=? WHERE user=? AND (author IS NULL OR author<>?)",
                             (name, user, name))

    def purge_channel(self, channel: str) -> None:
        """The channel is no longer only ours (shared with a client): what she read there goes, and what she wrote
        down from it."""
        with self._lock:
            self._db.execute("DELETE FROM channel_log WHERE channel=?", (channel,))
            self._db.execute("DELETE FROM channel_backfill WHERE channel=?", (channel,))
        self.forget_channel_notes(channel, "канал став спільним з іншою компанією")

    def channel_messages(self, channels: list[str], query: str = "", since_ts: float = 0.0,
                         limit: int = 40, until_ts: float | None = None) -> list[dict]:
        if not channels:
            return []
        where = f"deleted=0 AND channel IN ({','.join('?' * len(channels))}) AND CAST(ts AS REAL)>=?"
        args: list = [*channels, since_ts]
        if until_ts is not None:
            where += " AND CAST(ts AS REAL)<?"
            args.append(until_ts)
        for w in query.casefold().split()[:8]:
            where += " AND ufold(text) LIKE ?"
            args.append(f"%{w}%")
        rows = self._db.execute(f"SELECT * FROM channel_log WHERE {where} ORDER BY CAST(ts AS REAL) DESC LIMIT ?",
                                (*args, limit)).fetchall()
        return [dict(r) for r in rows]

    def audiences(self, conv_id: str | None, project_channels: list[str] | tuple = ()) -> tuple[str, list[str]]:
        """Where a note written in this conversation goes, and which notes it may read.
        A DM: its person's own notes plus the project's. A thread in one of the project's channels, or the web chat
        (people of the project): the project's. A thread in any other channel: that channel's plus the project's —
        its notes never reach elsewhere."""
        link = self.slack_link(conv_id) if conv_id else None
        if link is None:
            return "project", ["project"]
        if not link.thread_ts:
            mine = f"dm:{(link.owner or '').lower()}"
            return mine, ["project", mine]
        if link.channel in project_channels:
            return "project", ["project"]
        mine = f"channel:{link.channel}"
        return mine, ["project", mine]

    # staying quiet ------------------------------------------------------------------------------------------------
    def set_quiet(self, conv_id: str, turn_key: str, reaction: str | None, why: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO turn_quiet VALUES (?,?,?,?,?) ON CONFLICT(conversation_id, turn_key) DO "
                             "UPDATE SET reaction=excluded.reaction, why=excluded.why",
                             (conv_id, turn_key, reaction, why, time.time()))

    def quiet(self, conv_id: str, turn_key: str) -> dict | None:
        row = self._db.execute("SELECT * FROM turn_quiet WHERE conversation_id=? AND turn_key=?",
                               (conv_id, turn_key)).fetchone()
        return dict(row) if row else None

    def slack_inbox_of(self, message_id: int) -> dict | None:
        row = self._db.execute("SELECT team, channel, ts FROM slack_inbox WHERE message_id=?", (message_id,)).fetchone()
        return dict(row) if row else None
