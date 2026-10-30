"""SQLite state of the runner, with numbered migrations from the first version."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Append only: never edit a migration that has shipped, add the next one.
MIGRATIONS: list[str] = [
    # 1: who triggered which issue, and what the runner decided about it.
    """
    CREATE TABLE trigger_decisions (
        issue_key        TEXT NOT NULL,
        tag_activity_id  TEXT NOT NULL,
        author           TEXT NOT NULL,
        accepted         INTEGER NOT NULL CHECK (accepted IN (0, 1)),
        tagged_at_ms     INTEGER NOT NULL,
        announced        INTEGER NOT NULL DEFAULT 0 CHECK (announced IN (0, 1)),
        decided_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        PRIMARY KEY (issue_key, tag_activity_id)
    );
    """,
    # 2: tasks the runner took, and a journal of its external actions.
    """
    CREATE TABLE tasks (
        task_id          TEXT PRIMARY KEY,
        issue_db_id      TEXT NOT NULL,
        state            TEXT NOT NULL,
        summary          TEXT NOT NULL DEFAULT '',
        tag_author       TEXT NOT NULL,
        repo             TEXT NOT NULL,
        repo_path        TEXT NOT NULL,
        branch           TEXT NOT NULL,
        base_branch      TEXT NOT NULL,
        question_id      TEXT,
        question_at_ms   INTEGER,
        reason           TEXT,
        mr_urls          TEXT NOT NULL DEFAULT '[]',
        attempt_started  TEXT,
        created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE INDEX tasks_state ON tasks (state);
    CREATE TABLE actions (
        idem_key         TEXT PRIMARY KEY,
        task_id          TEXT NOT NULL,
        kind             TEXT NOT NULL,
        status           TEXT NOT NULL CHECK (status IN ('intent', 'done')),
        result           TEXT,
        updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    """,
]

MIGRATIONS.append(
    # 3: the other repositories of the group a task's agent may change, next to the one it starts in.
    """
    ALTER TABLE tasks ADD COLUMN extra_repos TEXT NOT NULL DEFAULT '[]';
    """
)

MIGRATIONS.append(
    # 4: the review loop after the MR. Every MR of a task, every signal from it (a review note, a
    # failed pipeline, a conflict, a ticket comment) recorded once, and the batches sent to the agent.
    """
    CREATE TABLE task_mrs (
        task_id          TEXT NOT NULL,
        repo             TEXT NOT NULL,
        project_id       INTEGER NOT NULL,
        iid              INTEGER NOT NULL,
        url              TEXT NOT NULL,
        state            TEXT NOT NULL DEFAULT 'opened',
        published_sha    TEXT NOT NULL,
        handled_pipeline INTEGER,
        handled_conflict TEXT,
        PRIMARY KEY (task_id, repo)
    );
    CREATE TABLE signals (
        id               TEXT PRIMARY KEY,
        task_id          TEXT NOT NULL,
        kind             TEXT NOT NULL CHECK (kind IN ('review', 'ci', 'conflict', 'ticket', 'moved')),
        repo             TEXT,
        discussion_id    TEXT,
        author           TEXT NOT NULL,
        body             TEXT NOT NULL,
        seen_at          TEXT NOT NULL,
        batch_id         TEXT,
        replied          INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX signals_task ON signals (task_id, batch_id);
    CREATE TABLE batches (
        id               TEXT PRIMARY KEY,
        task_id          TEXT NOT NULL,
        source           TEXT NOT NULL,
        author           TEXT NOT NULL,
        text             TEXT NOT NULL,
        ci_only          INTEGER NOT NULL,
        created_at       TEXT NOT NULL,
        sent_at          TEXT
    );
    ALTER TABLE tasks ADD COLUMN review_rounds INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE tasks ADD COLUMN ci_attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE tasks ADD COLUMN published_at_ms INTEGER;
    """
)

# Runner task states. Active ones hold the agent; the rest wait on people or are over.
ACTIVE_STATES = ("claimed", "dispatched", "publishing")
# security_hold: a text for the agent waits for a security reviewer (screening.py); nothing runs meanwhile.
TASK_STATES = (*ACTIVE_STATES, "needs_input", "code_review", "security_hold", "failed", "done", "stopped")


class AlreadyRunning(Exception):
    pass


class InstanceLock:
    """One runner per state directory. Held for the whole life of the process."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.read(fd, 64).decode(errors="replace").strip()
            os.close(fd)
            raise AlreadyRunning(f"another runner holds {self.path} (pid {holder or '?'})") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


@dataclass(frozen=True)
class TriggerDecision:
    issue_key: str
    tag_activity_id: str
    author: str
    accepted: bool
    announced: bool


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        os.chmod(path, 0o600)
        self.migrate()

    @classmethod
    def read_only(cls, path: Path):
        """The runner's database for a command that only looks: no migration, no write, no file created."""
        if not path.exists():
            raise FileNotFoundError(f"{path}: no runner database yet")
        self = cls.__new__(cls)
        self.path = path
        self.conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        if self.version != len(MIGRATIONS):
            v = self.version
            self.conn.close()
            raise RuntimeError(f"{path} is schema v{v}, this runner knows v{len(MIGRATIONS)}: start this "
                               "version of the runner once first")
        return self

    def close(self) -> None:
        self.conn.close()

    @property
    def version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> None:
        current = self.version
        if current > len(MIGRATIONS):
            raise RuntimeError(f"{self.path} is schema v{current}, this runner knows only v{len(MIGRATIONS)}")
        for n in range(current, len(MIGRATIONS)):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                # executescript() would COMMIT first and break atomicity, so statements go one by one.
                for stmt in _split(MIGRATIONS[n]):
                    self.conn.execute(stmt)
                self.conn.execute(f"PRAGMA user_version = {n + 1}")
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def trigger_decision(self, issue_key: str, tag_activity_id: str) -> TriggerDecision | None:
        row = self.conn.execute(
            "SELECT * FROM trigger_decisions WHERE issue_key = ? AND tag_activity_id = ?",
            (issue_key, tag_activity_id),
        ).fetchone()
        if not row:
            return None
        return TriggerDecision(row["issue_key"], row["tag_activity_id"], row["author"], bool(row["accepted"]),
                               bool(row["announced"]))

    def record_trigger(self, issue_key: str, tag_activity_id: str, author: str, accepted: bool,
                       tagged_at_ms: int) -> TriggerDecision:
        """Store the decision once and return what is stored (the first decision wins)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO trigger_decisions (issue_key, tag_activity_id, author, accepted, tagged_at_ms)"
            " VALUES (?, ?, ?, ?, ?)",
            (issue_key, tag_activity_id, author, int(accepted), tagged_at_ms),
        )
        stored = self.trigger_decision(issue_key, tag_activity_id)
        assert stored is not None
        return stored

    def bind_integration(self, role: str, identity: str) -> str:
        """The integration this database belongs to for `role`: `identity` on the first call, the stored one after."""
        self.conn.execute("INSERT OR IGNORE INTO integrations (role, identity) VALUES (?, ?)", (role, identity))
        return self.conn.execute("SELECT identity FROM integrations WHERE role = ?", (role,)).fetchone()[0]

    def rebind_integration(self, role: str, identity: str) -> str | None:
        """Bind `role` to `identity` whatever it was; returns what it was."""
        row = self.conn.execute("SELECT identity FROM integrations WHERE role = ?", (role,)).fetchone()
        self.conn.execute("INSERT OR REPLACE INTO integrations (role, identity) VALUES (?, ?)", (role, identity))
        return row[0] if row else None

    def mark_announced(self, issue_key: str, tag_activity_id: str) -> None:
        """The event for this decision is in events.jsonl. Until then it is announced again on every poll."""
        self.conn.execute(
            "UPDATE trigger_decisions SET announced = 1 WHERE issue_key = ? AND tag_activity_id = ?",
            (issue_key, tag_activity_id),
        )


MIGRATIONS.append(
    # 5: a failed task can be picked up again. The moment it failed bounds the comments that count.
    """
    ALTER TABLE tasks ADD COLUMN failed_at_ms INTEGER;
    """
)

MIGRATIONS.append(
    # 6: review rounds the agent answered without changing code. They have a budget of their own,
    # so questions about the implementation do not use up the rounds that fix it.
    """
    ALTER TABLE tasks ADD COLUMN review_answers INTEGER NOT NULL DEFAULT 0;
    """
)

MIGRATIONS.append(
    # 7: how far the engine's step journal of a task's current attempt was written to events.jsonl
    # (progress.py), so a restart goes on from there instead of writing the attempt's steps again.
    """
    CREATE TABLE progress_cursors (
        task_id          TEXT PRIMARY KEY,
        dispatch_id      TEXT NOT NULL,
        last_ms          INTEGER NOT NULL,
        at_last_ms       INTEGER NOT NULL
    );
    """
)

MIGRATIONS.append(
    # 8: the security screen (screening.py). One verdict per exact text under the screen's policy
    # version (a rejection applies everywhere), and the holds a reviewer decides: an approval is for
    # that task only. Neither keeps the text itself; `author` of a hold is a JSON list of logins.
    # A review batch keeps everyone whose words it carries, earlier notes of its threads included.
    """
    CREATE TABLE screenings (
        digest           TEXT PRIMARY KEY,
        task_id          TEXT NOT NULL,
        kind             TEXT NOT NULL,
        verdict          TEXT NOT NULL CHECK (verdict IN ('allow', 'review', 'block', 'rejected')),
        categories       TEXT NOT NULL DEFAULT '[]',
        reason           TEXT NOT NULL DEFAULT '',
        model            TEXT NOT NULL DEFAULT '',
        policy           TEXT NOT NULL,
        screened_at      TEXT NOT NULL,
        decided_by       TEXT,
        decided_at       TEXT
    );
    CREATE TABLE security_holds (
        task_id          TEXT NOT NULL,
        digest           TEXT NOT NULL,
        kind             TEXT NOT NULL,
        author           TEXT NOT NULL,
        resume_state     TEXT NOT NULL,
        ref              TEXT,
        status           TEXT NOT NULL CHECK (status IN ('open', 'allowed', 'rejected')),
        held_at          TEXT NOT NULL,
        decided_by       TEXT,
        decided_at       TEXT,
        PRIMARY KEY (task_id, digest)
    );
    ALTER TABLE batches ADD COLUMN authors TEXT NOT NULL DEFAULT '[]';
    """
)

MIGRATIONS.append(
    # 9: a person closing one MR of the task is a signal for the agent (take that repository's changes
    # out), not the end of the task. SQLite cannot change a CHECK, so the table is rebuilt as it is.
    """
    CREATE TABLE signals_v9 (
        id               TEXT PRIMARY KEY,
        task_id          TEXT NOT NULL,
        kind             TEXT NOT NULL CHECK (kind IN ('review', 'ci', 'conflict', 'ticket', 'moved', 'closed')),
        repo             TEXT,
        discussion_id    TEXT,
        author           TEXT NOT NULL,
        body             TEXT NOT NULL,
        seen_at          TEXT NOT NULL,
        batch_id         TEXT,
        replied          INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO signals_v9 (id, task_id, kind, repo, discussion_id, author, body, seen_at, batch_id, replied)
        SELECT id, task_id, kind, repo, discussion_id, author, body, seen_at, batch_id, replied FROM signals;
    DROP TABLE signals;
    ALTER TABLE signals_v9 RENAME TO signals;
    CREATE INDEX signals_task ON signals (task_id, batch_id);
    """
)


MIGRATIONS.append(
    # 10: cleanup (cleanup.py). Each pass and what it did to each leftover; who held the shared workspace
    # when, so a container or a process can be put down to the task that made it.
    """
    CREATE TABLE cleanups (
        id               TEXT PRIMARY KEY,
        trigger          TEXT NOT NULL,
        scope            TEXT NOT NULL CHECK (scope IN ('full', 'independent')),
        mode             TEXT NOT NULL CHECK (mode IN ('report', 'act')),
        status           TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')),
        started_at       TEXT NOT NULL,
        finished_at      TEXT,
        summary          TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX cleanups_started ON cleanups (started_at);
    CREATE TABLE cleanup_items (
        run_id           TEXT NOT NULL,
        kind             TEXT NOT NULL,
        ref              TEXT NOT NULL,
        action           TEXT NOT NULL,
        result           TEXT NOT NULL,
        task_id          TEXT,
        detail           TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (run_id, kind, ref)
    );
    CREATE TABLE workspace_holds (
        task_id          TEXT NOT NULL,
        started_at       TEXT NOT NULL,
        ended_at         TEXT
    );
    CREATE INDEX workspace_holds_open ON workspace_holds (ended_at);
    CREATE TABLE cleanup_projects (
        name             TEXT PRIMARY KEY,
        task_id          TEXT,
        seen_at          TEXT NOT NULL
    );
    """
)

MIGRATIONS.append(
    # 11: a hold is known only up to the last tick that saw it. While the runner is down nobody records who
    # works in the workspace, so what is made then is put down to no task.
    """
    ALTER TABLE workspace_holds ADD COLUMN seen_at TEXT;
    """
)


MIGRATIONS.append(
    # 12: the issue tracker and the code host this database's tasks belong to (loop.bind_integrations).
    """
    CREATE TABLE integrations (
        role             TEXT PRIMARY KEY,
        identity         TEXT NOT NULL,
        bound_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    """
)


MIGRATIONS.append(
    # 13: a code host's ids are opaque (integrations.Id): "00042" or "1e3" must come back as written, not as
    # 42 or 1000. Columns with no declared type keep each value as given; GitLab's numbers stay numbers.
    """
    CREATE TABLE task_mrs_v13 (
        task_id          TEXT NOT NULL,
        repo             TEXT NOT NULL,
        project_id       NOT NULL,
        iid              NOT NULL,
        url              TEXT NOT NULL,
        state            TEXT NOT NULL DEFAULT 'opened',
        published_sha    TEXT NOT NULL,
        handled_pipeline,
        handled_conflict TEXT,
        PRIMARY KEY (task_id, repo)
    );
    INSERT INTO task_mrs_v13 (task_id, repo, project_id, iid, url, state, published_sha, handled_pipeline,
                              handled_conflict)
        SELECT task_id, repo, project_id, iid, url, state, published_sha, handled_pipeline, handled_conflict
        FROM task_mrs;
    DROP TABLE task_mrs;
    ALTER TABLE task_mrs_v13 RENAME TO task_mrs;
    """
)


MIGRATIONS.append(
    # 14: the review agent (code_review.py). One row per review request on a ticket, also the engine task that
    # does it (`id`). While it is preparing, running or restoring, it holds the shared workspace. `snapshot`
    # is what was reviewed and how to put the workspace back; `result` the runner's decision per MR, fixed
    # before anything is posted, so a retry after a crash posts the same.
    """
    CREATE TABLE review_requests (
        id               TEXT PRIMARY KEY,
        task_id          TEXT NOT NULL,
        n                INTEGER NOT NULL,
        comment_id       TEXT NOT NULL,
        author           TEXT NOT NULL,
        asked_at_ms      INTEGER NOT NULL,
        request          TEXT NOT NULL DEFAULT '',
        state            TEXT NOT NULL CHECK (state IN ('pending', 'preparing', 'running', 'restoring', 'publishing',
                                                        'done', 'refused')),
        snapshot         TEXT NOT NULL DEFAULT '{}',
        verdict          TEXT,
        result           TEXT,
        outcome          TEXT,
        reason           TEXT,
        started_at       TEXT,
        created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (task_id, comment_id)
    );
    CREATE INDEX review_requests_state ON review_requests (state);
    """
)


MIGRATIONS.append(
    # 15: what a code review found that has to change goes back to the author as a signal of its own
    # (code_review.py, review_agent.fix_rounds). The table is rebuilt as it is, as in 9, for the CHECK.
    """
    CREATE TABLE signals_v15 (
        id               TEXT PRIMARY KEY,
        task_id          TEXT NOT NULL,
        kind             TEXT NOT NULL CHECK (kind IN ('review', 'ci', 'conflict', 'ticket', 'moved', 'closed',
                                                       'code_review')),
        repo             TEXT,
        discussion_id    TEXT,
        author           TEXT NOT NULL,
        body             TEXT NOT NULL,
        seen_at          TEXT NOT NULL,
        batch_id         TEXT,
        replied          INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO signals_v15 (id, task_id, kind, repo, discussion_id, author, body, seen_at, batch_id, replied)
        SELECT id, task_id, kind, repo, discussion_id, author, body, seen_at, batch_id, replied FROM signals;
    DROP TABLE signals;
    ALTER TABLE signals_v15 RENAME TO signals;
    CREATE INDEX signals_task ON signals (task_id, batch_id);
    """
)


TASK_FIELDS = ("task_id", "issue_db_id", "state", "summary", "tag_author", "repo", "repo_path", "branch",
               "base_branch", "question_id", "question_at_ms", "reason", "mr_urls", "attempt_started",
               "extra_repos", "review_rounds", "ci_attempts", "published_at_ms", "failed_at_ms",
               "review_answers")
TASK_COLUMNS = (*TASK_FIELDS, "updated_at")  # read: the last change is set by the store itself, never written


@dataclass(frozen=True)
class Task:
    task_id: str
    issue_db_id: str
    state: str
    summary: str
    tag_author: str
    repo: str
    repo_path: str
    branch: str
    base_branch: str
    question_id: str | None
    question_at_ms: int | None
    reason: str | None
    mr_urls: str
    attempt_started: str | None
    extra_repos: str  # JSON list of paths below the group, e.g. ["acme-theme"]
    review_rounds: int = 0  # review rounds after the first MR that ended in published code
    ci_attempts: int = 0    # CI-only repairs in a row, reset when the pipeline passes
    published_at_ms: int | None = None  # the MR comment, or the last `/continue`: later comments are review
    failed_at_ms: int | None = None     # when the task failed: a later `/continue` from a person resumes it
    review_answers: int = 0  # review rounds answered without a code change
    updated_at: str = ""     # when the record last changed: for a task that is over, when it ended

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES


class TaskStore(Store):
    """Tasks and the action journal, on top of the trigger decisions."""

    @contextlib.contextmanager
    def transaction(self):
        """A state change and the intents it implies land together or not at all."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def task(self, task_id: str) -> Task | None:
        row = self.conn.execute(f"SELECT {', '.join(TASK_COLUMNS)} FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return Task(*row) if row else None

    def tasks(self, *states: str) -> list[Task]:
        q = f"SELECT {', '.join(TASK_COLUMNS)} FROM tasks"
        args: tuple = ()
        if states:
            q += f" WHERE state IN ({', '.join('?' * len(states))})"
            args = states
        return [Task(*r) for r in self.conn.execute(q + " ORDER BY created_at", args).fetchall()]

    def create_task(self, **fields) -> Task:
        unknown = set(fields) - set(TASK_FIELDS)
        if unknown:
            raise ValueError(f"unknown task fields {unknown}")
        cols = ", ".join(fields)
        self.conn.execute(f"INSERT OR IGNORE INTO tasks ({cols}) VALUES ({', '.join('?' * len(fields))})",
                          tuple(fields.values()))
        task = self.task(fields["task_id"])
        assert task is not None
        return task

    def update_task(self, task_id: str, **fields) -> Task:
        unknown = set(fields) - set(TASK_FIELDS)
        if unknown or "task_id" in fields:
            raise ValueError(f"cannot update {unknown or 'task_id'}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE tasks SET {sets}, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE task_id = ?",
            (*fields.values(), task_id),
        )
        task = self.task(task_id)
        assert task is not None
        return task

    def accepted_untaken(self) -> list[TriggerDecision]:
        """Accepted triggers whose issue the runner has not taken yet, oldest tag first. Only an issue's newest
        decision counts: a later tag by someone not on the list overrides an earlier approval."""
        rows = self.conn.execute(
            "SELECT d.* FROM trigger_decisions d LEFT JOIN tasks t ON t.task_id = d.issue_key"
            " WHERE d.accepted = 1 AND t.task_id IS NULL AND NOT EXISTS (SELECT 1 FROM trigger_decisions n"
            " WHERE n.issue_key = d.issue_key AND n.tagged_at_ms > d.tagged_at_ms) ORDER BY d.tagged_at_ms"
        ).fetchall()
        return [TriggerDecision(r["issue_key"], r["tag_activity_id"], r["author"], True, bool(r["announced"]))
                for r in rows]

    # -- how far a task's engine steps were written out (progress.py) --

    def progress_cursor(self, task_id: str) -> tuple[str, int, int] | None:
        row = self.conn.execute("SELECT dispatch_id, last_ms, at_last_ms FROM progress_cursors WHERE task_id = ?",
                                (task_id,)).fetchone()
        return (row["dispatch_id"], row["last_ms"], row["at_last_ms"]) if row else None

    def save_progress_cursor(self, task_id: str, dispatch_id: str, last_ms: int, at_last_ms: int) -> None:
        self.conn.execute(
            "INSERT INTO progress_cursors (task_id, dispatch_id, last_ms, at_last_ms) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (task_id) DO UPDATE SET dispatch_id = excluded.dispatch_id, last_ms = excluded.last_ms,"
            " at_last_ms = excluded.at_last_ms",
            (task_id, dispatch_id, last_ms, at_last_ms))

    # -- action journal: an external effect is marked before and after it happens --

    def action(self, idem_key: str) -> tuple[str, str | None] | None:
        row = self.conn.execute("SELECT status, result FROM actions WHERE idem_key = ?", (idem_key,)).fetchone()
        return (row["status"], row["result"]) if row else None

    def action_intent(self, idem_key: str, task_id: str, kind: str, payload: str | None = None) -> None:
        """Record what is about to happen; `payload` is what is needed to redo it (the comment text)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO actions (idem_key, task_id, kind, status, result) VALUES (?, ?, ?, 'intent', ?)",
            (idem_key, task_id, kind, payload),
        )

    def pending_continues(self) -> list[tuple[str, str, str | None]]:
        """`/continue` requests recorded but not yet taken by Bulava: (key, task id, payload)."""
        return [(k, t, p) for k, t, kind, p in self.pending_actions() if kind == "continue"]

    def count_actions(self, prefix: str) -> int:
        """How many actions have an idempotency key that starts with `prefix`, done or not."""
        return self.conn.execute("SELECT COUNT(*) FROM actions WHERE substr(idem_key, 1, ?) = ?",
                                 (len(prefix), prefix)).fetchone()[0]

    def pending_actions(self) -> list[tuple[str, str, str, str | None]]:
        rows = self.conn.execute(
            "SELECT idem_key, task_id, kind, result FROM actions WHERE status = 'intent' ORDER BY updated_at"
        ).fetchall()
        return [(r["idem_key"], r["task_id"], r["kind"], r["result"]) for r in rows]

    def action_done(self, idem_key: str, result: str | None = None) -> None:
        self.conn.execute(
            "UPDATE actions SET status = 'done', result = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            " WHERE idem_key = ?",
            (result, idem_key),
        )

    # -- the security screen (screening.py) --

    def screening(self, digest: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM screenings WHERE digest = ?", (digest,)).fetchone()

    def save_screening(self, digest: str, task_id: str, kind: str, verdict: str, categories: list[str], reason: str,
                       model: str, policy: str, at: str) -> sqlite3.Row:
        """The first verdict for a text wins: a second screening of it never replaces a rejection."""
        self.conn.execute(
            "INSERT OR IGNORE INTO screenings (digest, task_id, kind, verdict, categories, reason, model, policy,"
            " screened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (digest, task_id, kind, verdict, json.dumps(categories), reason, model, policy, at))
        row = self.screening(digest)
        assert row is not None
        return row

    def decide_screening(self, digest: str, verdict: str, by: str, at: str) -> None:
        self.conn.execute("UPDATE screenings SET verdict = ?, decided_by = ?, decided_at = ? WHERE digest = ?",
                          (verdict, by, at, digest))

    def open_hold(self, task_id: str, digest: str, kind: str, author: str, resume_state: str, ref: str | None,
                  at: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO security_holds (task_id, digest, kind, author, resume_state, ref, status, held_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'open', ?)", (task_id, digest, kind, author, resume_state, ref, at))

    def hold(self, task_id: str, digest: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM security_holds WHERE task_id = ? AND digest = ?",
                                 (task_id, digest)).fetchone()

    def open_hold_of(self, task_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM security_holds WHERE task_id = ? AND status = 'open'"
                                 " ORDER BY held_at DESC LIMIT 1", (task_id,)).fetchone()

    def close_hold(self, task_id: str, digest: str, status: str, by: str, at: str) -> None:
        self.conn.execute("UPDATE security_holds SET status = ?, decided_by = ?, decided_at = ?"
                          " WHERE task_id = ? AND digest = ?", (status, by, at, task_id, digest))

    # -- cleanup (cleanup.py) --

    def hold_workspace(self, task_id: str | None, at: str, began: str | None = None) -> None:
        """`task_id` works in the shared workspace (None: nobody). A change: the hold before it ends at `at`, the
        new one starts at `began` (`at` when not given); in between nobody is known to hold it. The same task
        again: its hold is seen up to `began`."""
        began = began or at
        with self.transaction():
            row = self.conn.execute("SELECT rowid, task_id FROM workspace_holds WHERE ended_at IS NULL"
                                    " ORDER BY started_at DESC LIMIT 1").fetchone()
            if row and row["task_id"] == task_id:
                self.conn.execute("UPDATE workspace_holds SET seen_at = ? WHERE rowid = ?", (began, row["rowid"]))
                return
            self.conn.execute("UPDATE workspace_holds SET ended_at = ? WHERE ended_at IS NULL", (at,))
            if task_id:
                self.conn.execute("INSERT INTO workspace_holds (task_id, started_at, seen_at) VALUES (?, ?, ?)",
                                  (task_id, began, began))

    def close_open_holds(self) -> None:
        """At start: a hold left open by a crash or a stop ended when a tick last saw it, not now."""
        self.conn.execute("UPDATE workspace_holds SET ended_at = COALESCE(seen_at, started_at) WHERE ended_at IS NULL")

    def workspace_holds(self) -> list[tuple[str, str, str]]:
        """(task id, from, to), oldest first. An open hold reaches as far as the last tick that saw it."""
        rows = self.conn.execute("SELECT task_id, started_at, COALESCE(ended_at, seen_at, started_at) AS until"
                                 " FROM workspace_holds ORDER BY started_at")
        return [(r["task_id"], r["started_at"], r["until"]) for r in rows.fetchall()]

    def cleanup_projects(self) -> dict[str, tuple[str | None, str]]:
        """Compose projects seen as the workspace's: the one task that labelled all their containers, and when
        they were last seen so."""
        return {r["name"]: (r["task_id"], r["seen_at"])
                for r in self.conn.execute("SELECT name, task_id, seen_at FROM cleanup_projects")}

    def remember_projects(self, projects: dict[str, str | None], at: str) -> None:
        """Kept after their containers are gone, so their networks stay tied to the workspace. Two tasks seen
        for one project: no single task owns it."""
        with self.transaction():
            for name, task_id in projects.items():
                self.conn.execute(
                    "INSERT INTO cleanup_projects (name, task_id, seen_at) VALUES (?, ?, ?) ON CONFLICT (name) DO UPDATE"
                    " SET task_id = CASE WHEN cleanup_projects.task_id IS excluded.task_id THEN excluded.task_id"
                    " ELSE NULL END, seen_at = excluded.seen_at", (name, task_id, at))

    def cleanup_started(self, run_id: str, trigger: str, scope: str, mode: str, at: str) -> None:
        self.conn.execute("INSERT INTO cleanups (id, trigger, scope, mode, status, started_at)"
                          " VALUES (?, ?, ?, ?, 'running', ?)", (run_id, trigger, scope, mode, at))

    def cleanup_item(self, run_id: str, kind: str, ref: str, action: str, result: str, task_id: str | None,
                     detail: dict) -> None:
        self.conn.execute(
            "INSERT INTO cleanup_items (run_id, kind, ref, action, result, task_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (run_id, kind, ref) DO UPDATE SET action = excluded.action, result = excluded.result,"
            " detail = excluded.detail", (run_id, kind, ref, action, result, task_id, json.dumps(detail, default=str)))

    def cleanup_finished(self, run_id: str, status: str, at: str, summary: dict) -> None:
        self.conn.execute("UPDATE cleanups SET status = ?, finished_at = ?, summary = ? WHERE id = ?",
                          (status, at, json.dumps(summary, default=str), run_id))

    def cleanup_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        """The latest passes first."""
        return self.conn.execute("SELECT * FROM cleanups ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()

    def cleanup_items(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM cleanup_items WHERE run_id = ? ORDER BY kind, ref", (run_id,)).fetchall()

    def forget_cleanups(self, before: str) -> None:
        with self.transaction():
            self.conn.execute("DELETE FROM cleanup_items WHERE run_id IN (SELECT id FROM cleanups WHERE started_at < ?)",
                              (before,))
            self.conn.execute("DELETE FROM cleanups WHERE started_at < ?", (before,))

    def interrupt_cleanups(self, at: str) -> list[str]:
        """Passes a crash or a stop cut short: never counted as done. Returns their ids."""
        ids = [r["id"] for r in self.conn.execute("SELECT id FROM cleanups WHERE status = 'running'").fetchall()]
        self.conn.execute("UPDATE cleanups SET status = 'interrupted', finished_at = ? WHERE status = 'running'", (at,))
        return ids


@dataclass(frozen=True)
class TaskMR:
    task_id: str
    repo: str
    project_id: int | str  # the code host's ids, opaque (integrations.Id): GitLab's are numbers
    iid: int | str
    url: str
    state: str
    published_sha: str
    handled_pipeline: int | str | None
    handled_conflict: str | None


@dataclass(frozen=True)
class Signal:
    id: str
    task_id: str
    kind: str
    repo: str | None
    discussion_id: str | None
    author: str
    body: str
    seen_at: str
    batch_id: str | None
    replied: bool


@dataclass(frozen=True)
class Batch:
    id: str
    task_id: str
    source: str
    author: str
    text: str
    ci_only: bool
    created_at: str
    sent_at: str | None
    authors: str = "[]"  # JSON: everyone whose words are in `text`, earlier notes of its threads included


# A review in these states has the shared workspace switched to what it reviews (code_review.py).
REVIEW_HOLDS = ("preparing", "running", "restoring")
REVIEW_FIELDS = ("state", "snapshot", "verdict", "result", "outcome", "reason", "started_at")


@dataclass(frozen=True)
class ReviewRequest:
    id: str              # also the engine task's id: <ticket key>.review-<n>
    task_id: str         # the ticket's task
    n: int
    comment_id: str
    author: str
    asked_at_ms: int
    request: str         # what the comment said after the command
    state: str
    snapshot: str        # JSON
    verdict: str | None  # JSON: what the reviewer wrote, as read
    result: str | None   # JSON: the runner's decision per MR
    outcome: str | None  # approved | ready | attention | changes | unverified | nothing | refused | …
    reason: str | None
    started_at: str | None
    created_at: str = ""
    updated_at: str = ""


class ReviewStore(TaskStore):
    """The review loop's records, next to the tasks."""

    # -- the review agent (code_review.py) --

    def add_review(self, task_id: str, comment_id: str, author: str, asked_at_ms: int, request: str,
                   state: str = "pending", outcome: str | None = None, reason: str | None = None) -> ReviewRequest | None:
        """A new request, numbered after the ticket's earlier ones. None when this comment was recorded before."""
        if self.review_of_comment(task_id, comment_id):
            return None
        n = self.conn.execute("SELECT COUNT(*) FROM review_requests WHERE task_id = ?", (task_id,)).fetchone()[0] + 1
        rid = f"{task_id}.review-{n}"
        self.conn.execute(
            "INSERT INTO review_requests (id, task_id, n, comment_id, author, asked_at_ms, request, state, outcome,"
            " reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, task_id, n, str(comment_id), author, asked_at_ms, request, state, outcome, reason))
        return self.review(rid)

    def review(self, review_id: str) -> ReviewRequest | None:
        r = self.conn.execute("SELECT * FROM review_requests WHERE id = ?", (review_id,)).fetchone()
        return ReviewRequest(**dict(r)) if r else None

    def review_of_comment(self, task_id: str, comment_id: str) -> ReviewRequest | None:
        r = self.conn.execute("SELECT id FROM review_requests WHERE task_id = ? AND comment_id = ?",
                              (task_id, str(comment_id))).fetchone()
        return self.review(r["id"]) if r else None

    def reviews(self, *states: str, task_id: str | None = None) -> list[ReviewRequest]:
        q, args = "SELECT * FROM review_requests WHERE 1 = 1", []
        if states:
            q += f" AND state IN ({', '.join('?' * len(states))})"
            args += states
        if task_id:
            q += " AND task_id = ?"
            args.append(task_id)
        return [ReviewRequest(**dict(r)) for r in self.conn.execute(q + " ORDER BY created_at, n", args).fetchall()]

    def update_review(self, review_id: str, **fields) -> ReviewRequest:
        unknown = set(fields) - set(REVIEW_FIELDS)
        if unknown:
            raise ValueError(f"cannot update {unknown}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE review_requests SET {sets}, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                          " WHERE id = ?", (*fields.values(), review_id))
        review = self.review(review_id)
        assert review is not None
        return review

    def review_holding(self) -> ReviewRequest | None:
        """The review that has the shared workspace now, if any."""
        found = self.reviews(*REVIEW_HOLDS)
        return found[0] if found else None

    def task_mrs(self, task_id: str) -> list[TaskMR]:
        rows = self.conn.execute("SELECT * FROM task_mrs WHERE task_id = ? ORDER BY repo", (task_id,)).fetchall()
        return [TaskMR(**dict(r)) for r in rows]

    def upsert_mr(self, task_id: str, repo: str, project_id: int | str, iid: int | str, url: str, sha: str) -> None:
        self.conn.execute(
            "INSERT INTO task_mrs (task_id, repo, project_id, iid, url, published_sha) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (task_id, repo) DO UPDATE SET iid = excluded.iid, url = excluded.url,"
            " published_sha = excluded.published_sha",
            (task_id, repo, project_id, iid, url, sha))

    def drop_mr(self, task_id: str, repo: str) -> None:
        self.conn.execute("DELETE FROM task_mrs WHERE task_id = ? AND repo = ?", (task_id, repo))

    def update_mr(self, task_id: str, repo: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE task_mrs SET {sets} WHERE task_id = ? AND repo = ?", (*fields.values(), task_id, repo))

    def add_signal(self, sig_id: str, task_id: str, kind: str, author: str, body: str, seen_at: str,
                   repo: str | None = None, discussion_id: str | None = None) -> bool:
        """Record a signal once. False if it was seen before (a repeated poll)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO signals (id, task_id, kind, repo, discussion_id, author, body, seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (sig_id, task_id, kind, repo, discussion_id, author, body, seen_at))
        return cur.rowcount == 1

    def signals(self, task_id: str, batch_id: str | None = "", unbatched: bool = False) -> list[Signal]:
        if unbatched:
            rows = self.conn.execute("SELECT * FROM signals WHERE task_id = ? AND batch_id IS NULL ORDER BY seen_at, id",
                                     (task_id,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM signals WHERE task_id = ? AND batch_id = ? ORDER BY seen_at, id",
                                     (task_id, batch_id)).fetchall()
        return [Signal(**{**dict(r), "replied": bool(r["replied"])}) for r in rows]

    def batch(self, batch_id: str) -> Batch | None:
        r = self.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        return Batch(**{**dict(r), "ci_only": bool(r["ci_only"])}) if r else None

    def open_batch(self, task_id: str) -> Batch | None:
        """A batch made but not yet accepted by the engine (busy, or a crash before the answer)."""
        r = self.conn.execute("SELECT id FROM batches WHERE task_id = ? AND sent_at IS NULL ORDER BY created_at",
                              (task_id,)).fetchone()
        return self.batch(r["id"]) if r else None

    def last_sent_batch(self, task_id: str) -> Batch | None:
        # A batch dropped before sending is marked `stale`, which is not a time and sorts after every one.
        r = self.conn.execute("SELECT id FROM batches WHERE task_id = ? AND sent_at IS NOT NULL AND sent_at != 'stale'"
                              " ORDER BY sent_at DESC LIMIT 1", (task_id,)).fetchone()
        return self.batch(r["id"]) if r else None

    def make_batch(self, batch_id: str, task_id: str, source: str, author: str, text: str, ci_only: bool,
                   created_at: str, signal_ids: list[str], authors: list[str] = ()) -> Batch:
        with self.transaction():
            self.conn.execute("INSERT INTO batches (id, task_id, source, author, text, ci_only, created_at, authors)"
                              " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (batch_id, task_id, source, author, text,
                                                                   int(ci_only), created_at, json.dumps(list(authors))))
            for sid in signal_ids:
                self.conn.execute("UPDATE signals SET batch_id = ? WHERE id = ?", (batch_id, sid))
        return self.batch(batch_id)

    def set_batch_text(self, batch_id: str, text: str) -> None:
        self.conn.execute("UPDATE batches SET text = ? WHERE id = ?", (text, batch_id))

    def batch_sent(self, batch_id: str, at: str) -> None:
        self.conn.execute("UPDATE batches SET sent_at = ? WHERE id = ?", (at, batch_id))

    def consume_signal(self, sig_id: str, task_id: str, kind: str, author: str, body: str, at: str,
                       how: str) -> None:
        """A comment already used another way (an answer to the agent's question): never a review."""
        self.add_signal(sig_id, task_id, kind, author, body, at)
        self.conn.execute("UPDATE signals SET batch_id = ? WHERE id = ? AND batch_id IS NULL", (how, sig_id))

    def signals_of_kind(self, task_id: str, kind: str) -> list[Signal]:
        rows = self.conn.execute("SELECT * FROM signals WHERE task_id = ? AND kind = ? ORDER BY seen_at, id",
                                 (task_id, kind)).fetchall()
        return [Signal(**{**dict(r), "replied": bool(r["replied"])}) for r in rows]

    def signal(self, sig_id: str) -> Signal | None:
        r = self.conn.execute("SELECT * FROM signals WHERE id = ?", (sig_id,)).fetchone()
        return Signal(**{**dict(r), "replied": bool(r["replied"])}) if r else None

    def withdraw_signal(self, sig_id: str) -> None:
        """A signal that no longer holds, before it reached the agent: as if it was never seen.

        A batch that holds it and is not sent yet is dropped; its other signals wait for the next one.
        """
        with self.transaction():
            row = self.conn.execute("SELECT s.batch_id FROM signals s LEFT JOIN batches b ON b.id = s.batch_id"
                                    " WHERE s.id = ? AND (s.batch_id IS NULL OR (b.id IS NOT NULL AND b.sent_at IS NULL))",
                                    (sig_id,)).fetchone()
            if row is None:
                return  # unknown, or the agent has it already
            if row["batch_id"]:
                self.conn.execute("UPDATE batches SET sent_at = 'stale' WHERE id = ?", (row["batch_id"],))
                self.conn.execute("UPDATE signals SET batch_id = NULL WHERE batch_id = ? AND id != ?",
                                  (row["batch_id"], sig_id))
            self.conn.execute("DELETE FROM signals WHERE id = ?", (sig_id,))

    def drop_signals(self, ids: list[str], why: str) -> None:
        for sid in ids:
            self.conn.execute("UPDATE signals SET batch_id = ? WHERE id = ? AND batch_id IS NULL", (why, sid))

    def mark_replied(self, sig_id: str) -> None:
        self.conn.execute("UPDATE signals SET replied = 1 WHERE id = ?", (sig_id,))

    def has_pending_review(self) -> bool:
        """Anything from an open MR waiting for the agent: it goes before new tasks."""
        return self.conn.execute(
            "SELECT 1 FROM signals s JOIN tasks t ON t.task_id = s.task_id"
            " WHERE t.state = 'code_review' AND s.batch_id IS NULL"
            " UNION SELECT 1 FROM batches b JOIN tasks t ON t.task_id = b.task_id"
            " WHERE t.state = 'code_review' AND b.sent_at IS NULL LIMIT 1").fetchone() is not None


def _split(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
