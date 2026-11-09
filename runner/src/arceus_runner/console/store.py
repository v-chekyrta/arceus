"""console.db: the console's own state. People and their roles, sessions, one-time links, failed sign-ins, and the
journal of what people did. Numbered migrations, append only, the way runner.db has them.

Nothing here imports the web stack: the server's CLI (`arceus-runner console invite`) uses it from the runner's own
virtualenv, which has no web packages.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROLES = ("qa", "architect", "operator", "admin")
STATUSES = ("pending", "active", "disabled")
# scrypt, from the standard library: about 50 ms and 32 MB per check, so a stolen database is slow to guess at.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD, MAX_PASSWORD = 10, 256
# Failed sign-ins: this many for one address in the window, or this many from one network address, and the next
# attempt waits until the oldest one leaves the window.
FAIL_WINDOW = timedelta(minutes=15)
# Per address from one network address (a person mistyping), per network address (someone trying many accounts),
# and per address from anywhere, much higher: so a stranger's wrong passwords cannot keep its owner out.
FAILS_PER_EMAIL_IP, FAILS_PER_IP, FAILS_PER_EMAIL = 5, 30, 100
# New accounts asked for from one network address in an hour: more is someone filling the admins' list.
REGISTRATIONS_PER_IP_HOUR = 5
LINK_TTL = timedelta(hours=48)

MIGRATIONS: list[str] = [
    # 1: people, their sessions, one-time links (an invitation or a password reset), failed sign-ins, the journal.
    """
    CREATE TABLE users (
        id               INTEGER PRIMARY KEY,
        email            TEXT NOT NULL UNIQUE,
        name             TEXT NOT NULL,
        password         TEXT NOT NULL,
        status           TEXT NOT NULL CHECK (status IN ('pending', 'active', 'disabled')),
        roles            TEXT NOT NULL DEFAULT '',
        note             TEXT NOT NULL DEFAULT '',
        approved_by      INTEGER REFERENCES users (id),
        approved_at      TEXT,
        last_login_at    TEXT,
        created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE TABLE sessions (
        id_hash          TEXT PRIMARY KEY,
        user_id          INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        csrf             TEXT NOT NULL,
        ip               TEXT NOT NULL DEFAULT '',
        user_agent       TEXT NOT NULL DEFAULT '',
        created_at       TEXT NOT NULL,
        last_seen_at     TEXT NOT NULL,
        expires_at       TEXT NOT NULL
    );
    CREATE INDEX sessions_user ON sessions (user_id);
    CREATE TABLE links (
        id_hash          TEXT PRIMARY KEY,
        kind             TEXT NOT NULL CHECK (kind IN ('invite', 'reset')),
        email            TEXT NOT NULL,
        roles            TEXT NOT NULL DEFAULT '',
        user_id          INTEGER REFERENCES users (id) ON DELETE CASCADE,
        created_by       INTEGER REFERENCES users (id),
        created_label    TEXT NOT NULL DEFAULT '',
        created_at       TEXT NOT NULL,
        expires_at       TEXT NOT NULL,
        used_at          TEXT
    );
    CREATE TABLE login_failures (
        id               INTEGER PRIMARY KEY,
        email            TEXT NOT NULL,
        ip               TEXT NOT NULL,
        at               TEXT NOT NULL
    );
    CREATE INDEX login_failures_email ON login_failures (email, at);
    CREATE INDEX login_failures_ip ON login_failures (ip, at);
    CREATE TABLE audit (
        id               INTEGER PRIMARY KEY,
        at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        actor_id         INTEGER REFERENCES users (id),
        actor            TEXT NOT NULL,
        action           TEXT NOT NULL,
        target           TEXT NOT NULL DEFAULT '',
        detail           TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX audit_at ON audit (at);
    """,
    # 2: every version of a file saved from the console (instructions, the project's notes, config.yaml): what was
    # there before, what was saved, who saved it. A version is a step back the page can offer.
    """
    CREATE TABLE file_versions (
        id               INTEGER PRIMARY KEY,
        kind             TEXT NOT NULL,
        path             TEXT NOT NULL,
        before_sha       TEXT NOT NULL,
        before_text      TEXT,
        after_sha        TEXT NOT NULL,
        after_text       TEXT NOT NULL,
        actor_id         INTEGER REFERENCES users (id),
        actor            TEXT NOT NULL,
        note             TEXT NOT NULL DEFAULT '',
        at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE INDEX file_versions_kind ON file_versions (kind, id);
    """,
    # 3: who asked for an account from where, so one network address cannot fill the list of requests.
    """
    CREATE TABLE registrations (
        id               INTEGER PRIMARY KEY,
        ip               TEXT NOT NULL,
        at               TEXT NOT NULL
    );
    CREATE INDEX registrations_ip ON registrations (ip, at);
    """,
]


class ConsoleError(Exception):
    """Something a person asked for that cannot be done, in words for them."""


@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: str
    status: str
    roles: frozenset[str]
    note: str
    approved_by: int | None
    approved_at: str | None
    last_login_at: str | None
    created_at: str

    @property
    def active(self) -> bool:
        return self.status == "active"

    def has(self, role: str) -> bool:
        return role in self.roles

    @property
    def label(self) -> str:
        return f"{self.name} <{self.email}>"


@dataclass(frozen=True)
class Session:
    user: User
    csrf: str
    expires_at: datetime


@dataclass(frozen=True)
class Link:
    kind: str
    email: str
    roles: frozenset[str]
    user_id: int | None
    expires_at: str


def now() -> datetime:
    return datetime.now(UTC)


def iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normal_email(email: str) -> str:
    return email.strip().lower()


_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")


def valid_email(email: str) -> bool:
    """An address of printable ASCII only: it goes into headers (Grafana's user) and nothing in it can split one."""
    return len(email) <= 254 and bool(_EMAIL.match(email))


def roles_text(roles) -> str:
    unknown = set(roles) - set(ROLES)
    if unknown:
        raise ConsoleError(f"no such role: {', '.join(sorted(unknown))}")
    return " ".join(r for r in ROLES if r in set(roles))


def clean_name(name: str) -> str:
    """A person's name as kept: printable characters only, spaces collapsed."""
    return " ".join("".join(ch for ch in name if ch.isprintable() or ch.isspace()).split())


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"The password needs at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return f"The password can have at most {MAX_PASSWORD} characters."
    if password.strip() != password or not password.strip():
        return "The password cannot start or end with a space."
    return None


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=SCRYPT_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


# A hash checked when the address is unknown, so a wrong address takes as long as a wrong password.
_DUMMY = hash_password(secrets.token_urlsafe(16))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _split(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


class ConsoleStore:
    """console.db. A connection per instance: the web server opens one per request (its threads), the CLI one."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        if fresh:
            os.chmod(path, 0o600)
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> ConsoleStore:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> None:
        current = self.version
        if current > len(MIGRATIONS):
            raise RuntimeError(f"{self.path} is schema v{current}, this console knows only v{len(MIGRATIONS)}")
        for n in range(current, len(MIGRATIONS)):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if self.version == n:  # another process may have run it while this one waited for the lock
                    for stmt in _split(MIGRATIONS[n]):
                        self.conn.execute(stmt)
                    self.conn.execute(f"PRAGMA user_version = {n + 1}")
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    @contextlib.contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # -- people ---------------------------------------------------------------------------------------------------

    @staticmethod
    def _user(row: sqlite3.Row | None) -> User | None:
        if row is None:
            return None
        return User(id=row["id"], email=row["email"], name=row["name"], status=row["status"],
                    roles=frozenset(row["roles"].split()), note=row["note"], approved_by=row["approved_by"],
                    approved_at=row["approved_at"], last_login_at=row["last_login_at"], created_at=row["created_at"])

    def user(self, user_id: int) -> User | None:
        return self._user(self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    def user_by_email(self, email: str) -> User | None:
        return self._user(self.conn.execute("SELECT * FROM users WHERE email = ?",
                                            (normal_email(email),)).fetchone())

    def users(self) -> list[User]:
        rows = self.conn.execute("SELECT * FROM users ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'active' "
                                 "THEN 1 ELSE 2 END, lower(name), id").fetchall()
        return [u for u in (self._user(r) for r in rows) if u]

    def pending_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM users WHERE status = 'pending'").fetchone()[0]

    def active_admins(self) -> list[User]:
        return [u for u in self.users() if u.active and u.has("admin")]

    def register(self, email: str, name: str, password: str, note: str = "", ip: str = "") -> User | None:
        """A new account waiting for an admin. None when the address has an account already: the caller says the
        same thing either way, so the form does not tell strangers who has one."""
        if ip:
            hour_ago = iso(now() - timedelta(hours=1))
            self.conn.execute("DELETE FROM registrations WHERE at < ?", (iso(now() - timedelta(days=1)),))
            asked = self.conn.execute("SELECT COUNT(*) FROM registrations WHERE ip = ? AND at >= ?",
                                      (ip, hour_ago)).fetchone()[0]
            if asked >= REGISTRATIONS_PER_IP_HOUR:
                raise ConsoleError("Too many requests for accounts from here. Try again in an hour, or ask an admin "
                                   "for an invitation.")
            self.conn.execute("INSERT INTO registrations (ip, at) VALUES (?, ?)", (ip, iso(now())))
        email, name = normal_email(email), clean_name(name)
        if not valid_email(email):
            raise ConsoleError("That does not look like an email address.")
        if not name or len(name) > 120:
            raise ConsoleError("Say your name, up to 120 characters.")
        problem = password_problem(password)
        if problem:
            raise ConsoleError(problem)
        hashed = hash_password(password)
        with self.transaction():
            if self.user_by_email(email):
                return None
            cur = self.conn.execute("INSERT INTO users (email, name, password, status, note) VALUES (?, ?, ?, ?, ?)",
                                    (email, name, hashed, "pending", note.strip()[:500]))
            self.log(None, f"{name} <{email}>", "registered", f"user:{cur.lastrowid}")
        return self.user(cur.lastrowid)

    def authenticate(self, email: str, password: str, ip: str) -> User | None:
        """The account these credentials open, active or not, or None. Raises when this address or this network
        address has failed too often lately."""
        email = normal_email(email)
        wait = self.throttled(email, ip)
        if wait:
            minutes = max(1, round(wait.total_seconds() / 60))
            raise ConsoleError(f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.")
        row = self.conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            check_password(password, _DUMMY)
            ok = False
        else:
            ok = check_password(password, row["password"])
        if not ok:
            self.conn.execute("INSERT INTO login_failures (email, ip, at) VALUES (?, ?, ?)", (email, ip, iso(now())))
            return None
        self.conn.execute("DELETE FROM login_failures WHERE email = ?", (email,))
        return self._user(row)

    def throttled(self, email: str, ip: str) -> timedelta | None:
        """How long the next attempt has to wait, or None."""
        since = iso(now() - FAIL_WINDOW)
        self.conn.execute("DELETE FROM login_failures WHERE at < ?", (since,))
        waits = []
        for where, args, limit in (("email = ? AND ip = ?", (email, ip), FAILS_PER_EMAIL_IP),
                                   ("ip = ?", (ip,), FAILS_PER_IP), ("email = ?", (email,), FAILS_PER_EMAIL)):
            if not all(args):
                continue
            rows = self.conn.execute(f"SELECT at FROM login_failures WHERE {where} AND at >= ? ORDER BY at DESC",
                                     (*args, since)).fetchall()
            if len(rows) >= limit:
                oldest_counted = parse_iso(rows[limit - 1]["at"])
                waits.append(oldest_counted + FAIL_WINDOW - now())
        wait = max(waits, default=None)
        return wait if wait and wait > timedelta(0) else None

    def _ensure_admin_stays(self, user: User, roles: frozenset[str], status: str) -> None:
        stays_admin = status == "active" and "admin" in roles
        if user.active and user.has("admin") and not stays_admin:
            others = [a for a in self.active_admins() if a.id != user.id]
            if not others:
                raise ConsoleError("This is the last active admin. Make someone else an admin first.")

    def approve(self, user_id: int, roles, by: User) -> User:
        with self.transaction():
            user = self.user(user_id)
            if user is None:
                raise ConsoleError("No such account.")
            if user.status != "pending":
                raise ConsoleError("This account is not waiting for approval.")
            text = roles_text(roles)
            self.conn.execute("UPDATE users SET status = 'active', roles = ?, approved_by = ?, approved_at = ?, "
                              "updated_at = ? WHERE id = ?", (text, by.id, iso(now()), iso(now()), user_id))
            self.log(by, by.label, "approved", f"user:{user_id}", {"email": user.email, "roles": text.split()})
        found = self.user(user_id)
        assert found is not None
        return found

    def set_roles(self, user_id: int, roles, by: User | None, actor: str = "") -> User:
        with self.transaction():
            user = self.user(user_id)
            if user is None:
                raise ConsoleError("No such account.")
            text = roles_text(roles)
            self._ensure_admin_stays(user, frozenset(text.split()), user.status)
            if text != " ".join(r for r in ROLES if r in user.roles):
                self.conn.execute("UPDATE users SET roles = ?, updated_at = ? WHERE id = ?", (text, iso(now()), user_id))
                self.log(by, actor or (by.label if by else "server"), "roles_changed", f"user:{user_id}",
                         {"email": user.email, "from": sorted(user.roles), "to": text.split()})
        found = self.user(user_id)
        assert found is not None
        return found

    def set_status(self, user_id: int, status: str, by: User | None, actor: str = "") -> User:
        """Disable an account (its sessions end at once) or enable it again. Approving is `approve`."""
        if status not in ("active", "disabled"):
            raise ConsoleError("No such status.")
        with self.transaction():
            user = self.user(user_id)
            if user is None:
                raise ConsoleError("No such account.")
            if user.status == "pending":
                raise ConsoleError("Approve or decline the account first.")
            self._ensure_admin_stays(user, user.roles, status)
            if status != user.status:
                self.conn.execute("UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
                                  (status, iso(now()), user_id))
                if status == "disabled":
                    self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                    self.conn.execute("UPDATE links SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                                      (iso(now()), user_id))
                self.log(by, actor or (by.label if by else "server"),
                         "disabled" if status == "disabled" else "enabled", f"user:{user_id}", {"email": user.email})
        found = self.user(user_id)
        assert found is not None
        return found

    def decline(self, user_id: int, by: User) -> None:
        """A registration nobody approves: gone, so the address can register again."""
        with self.transaction():
            user = self.user(user_id)
            if user is None or user.status != "pending":
                raise ConsoleError("This account is not waiting for approval.")
            self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self.log(by, by.label, "declined", f"user:{user_id}", {"email": user.email, "name": user.name})

    def set_password(self, user_id: int, password: str, by: User | None, actor: str = "",
                     keep_session: str | None = None) -> None:
        """A new password. Every other session of the account ends."""
        problem = password_problem(password)
        if problem:
            raise ConsoleError(problem)
        hashed = hash_password(password)
        with self.transaction():
            user = self.user(user_id)
            if user is None:
                raise ConsoleError("No such account.")
            self.conn.execute("UPDATE users SET password = ?, updated_at = ? WHERE id = ?", (hashed, iso(now()), user_id))
            self.conn.execute("DELETE FROM sessions WHERE user_id = ? AND id_hash != ?",
                              (user_id, _token_hash(keep_session) if keep_session else ""))
            self.log(by, actor or (by.label if by else user.label), "password_changed", f"user:{user_id}",
                     {"email": user.email})

    # -- sessions -------------------------------------------------------------------------------------------------

    def start_session(self, user: User, ip: str, user_agent: str, idle: timedelta, longest: timedelta) -> str:
        """A new session; returns its token, which only the cookie holds (the database keeps its hash)."""
        token = secrets.token_urlsafe(32)
        at = now()
        with self.transaction():
            self.conn.execute("INSERT INTO sessions (id_hash, user_id, csrf, ip, user_agent, created_at, "
                              "last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (_token_hash(token), user.id, secrets.token_urlsafe(24), ip[:64], user_agent[:300],
                               iso(at), iso(at), iso(at + min(idle, longest))))
            self.conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (iso(at), user.id))
            self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(at),))
            self.log(user, user.label, "signed_in", f"user:{user.id}", {"ip": ip})
        return token

    def session(self, token: str, idle: timedelta, longest: timedelta) -> Session | None:
        """The session this cookie is, still valid and of an active account, its idle expiry moved on. None for
        anything else."""
        if not token or len(token) > 100:
            return None
        h = _token_hash(token)
        row = self.conn.execute("SELECT * FROM sessions WHERE id_hash = ?", (h,)).fetchone()
        if row is None:
            return None
        at = now()
        if parse_iso(row["expires_at"]) <= at:
            self.conn.execute("DELETE FROM sessions WHERE id_hash = ?", (h,))
            return None
        user = self.user(row["user_id"])
        if user is None or not user.active:
            return None
        ends = min(at + idle, parse_iso(row["created_at"]) + longest)
        if (at - parse_iso(row["last_seen_at"])) > timedelta(minutes=1):
            self.conn.execute("UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id_hash = ?",
                              (iso(at), iso(ends), h))
        return Session(user=user, csrf=row["csrf"], expires_at=ends)

    def end_session(self, token: str) -> None:
        self.conn.execute("DELETE FROM sessions WHERE id_hash = ?", (_token_hash(token),))

    def end_sessions(self, user_id: int, keep: str | None = None) -> int:
        cur = self.conn.execute("DELETE FROM sessions WHERE user_id = ? AND id_hash != ?",
                                (user_id, _token_hash(keep) if keep else ""))
        return cur.rowcount

    def session_count(self, user_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ? AND expires_at > ?",
                                 (user_id, iso(now()))).fetchone()[0]

    # -- one-time links -------------------------------------------------------------------------------------------

    def make_link(self, kind: str, email: str, roles=(), by: User | None = None, actor: str = "") -> str:
        """An invitation (a new account, active at once, with these roles) or a password reset of an existing
        account. Returns the token for the link; a newer link of the same kind for the address replaces older ones."""
        email = normal_email(email)
        if not valid_email(email):
            raise ConsoleError("That does not look like an email address.")
        token = secrets.token_urlsafe(32)
        at = now()
        with self.transaction():
            user = self.user_by_email(email)
            if kind == "invite":
                if user is not None:
                    raise ConsoleError(f"{email} has an account already: change its roles, or make a reset link.")
                text = roles_text(roles)
                user_id = None
            elif kind == "reset":
                if user is None:
                    raise ConsoleError(f"No account for {email}.")
                if user.status == "disabled":
                    raise ConsoleError(f"{email} is disabled: enable it first.")
                text, user_id = "", user.id
            else:
                raise ValueError(kind)
            self.conn.execute("UPDATE links SET used_at = ? WHERE kind = ? AND email = ? AND used_at IS NULL",
                              (iso(at), kind, email))
            self.conn.execute("INSERT INTO links (id_hash, kind, email, roles, user_id, created_by, created_label, "
                              "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (_token_hash(token), kind, email, text, user_id, by.id if by else None,
                               actor or (by.label if by else "server"), iso(at), iso(at + LINK_TTL)))
            self.log(by, actor or (by.label if by else "server"), f"{kind}_link_made",
                     f"user:{user_id}" if user_id else email, {"email": email, "roles": text.split()})
        return token

    def link(self, token: str) -> Link | None:
        if not token or len(token) > 100:
            return None
        row = self.conn.execute("SELECT * FROM links WHERE id_hash = ?", (_token_hash(token),)).fetchone()
        if row is None or row["used_at"] or parse_iso(row["expires_at"]) <= now():
            return None
        return Link(kind=row["kind"], email=row["email"], roles=frozenset(row["roles"].split()),
                    user_id=row["user_id"], expires_at=row["expires_at"])

    def use_link(self, token: str, password: str, name: str = "") -> User:
        """Spend a link: an invitation makes its account (active, with its roles), a reset sets the password."""
        problem = password_problem(password)
        if problem:
            raise ConsoleError(problem)
        hashed = hash_password(password)
        h = _token_hash(token)
        with self.transaction():
            link = self.link(token)
            if link is None:
                raise ConsoleError("This link has expired or was used already. Ask an admin for a new one.")
            self.conn.execute("UPDATE links SET used_at = ? WHERE id_hash = ?", (iso(now()), h))
            if link.kind == "invite":
                name = clean_name(name)
                if not name or len(name) > 120:
                    raise ConsoleError("Say your name, up to 120 characters.")
                if self.user_by_email(link.email):
                    raise ConsoleError(f"{link.email} has an account already. Sign in, or ask for a reset link.")
                row = self.conn.execute("SELECT created_by FROM links WHERE id_hash = ?", (h,)).fetchone()
                cur = self.conn.execute(
                    "INSERT INTO users (email, name, password, status, roles, approved_by, approved_at) "
                    "VALUES (?, ?, ?, 'active', ?, ?, ?)",
                    (link.email, name, hashed, " ".join(r for r in ROLES if r in link.roles), row["created_by"],
                     iso(now())))
                user_id = cur.lastrowid
                self.log(None, f"{name} <{link.email}>", "invitation_used", f"user:{user_id}",
                         {"roles": sorted(link.roles)})
            else:
                user = self.user(link.user_id or 0)
                if user is None or user.status == "disabled":
                    raise ConsoleError("This account cannot sign in. Ask an admin.")
                user_id = user.id
                self.conn.execute("UPDATE users SET password = ?, updated_at = ? WHERE id = ?",
                                  (hashed, iso(now()), user_id))
                self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                self.conn.execute("DELETE FROM login_failures WHERE email = ?", (user.email,))
                self.log(None, user.label, "password_reset", f"user:{user_id}")
        found = self.user(user_id)
        assert found is not None
        return found

    # -- the journal ----------------------------------------------------------------------------------------------

    def log(self, by: User | None, actor: str, action: str, target: str = "", detail: dict | None = None) -> None:
        self.conn.execute("INSERT INTO audit (actor_id, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                          (by.id if by else None, actor, action, target,
                           json.dumps(detail or {}, ensure_ascii=False, default=str)))

    def add_version(self, kind: str, path: Path, before_sha: str, before_text: str | None, after_sha: str,
                    after_text: str, by: User, note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO file_versions (kind, path, before_sha, before_text, after_sha, after_text, actor_id, actor, "
            "note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, str(path), before_sha, before_text, after_sha, after_text, by.id, by.label, note[:300]))
        return cur.lastrowid

    def versions(self, kind: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM file_versions WHERE kind = ? ORDER BY id DESC LIMIT ?",
                                 (kind, limit)).fetchall()
        return [dict(r) for r in rows]

    def version_of(self, kind: str, version_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM file_versions WHERE kind = ? AND id = ?", (kind, version_id)).fetchone()
        return dict(row) if row else None

    def journal(self, limit: int = 200, target: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM audit", []
        if target:
            q += " WHERE target = ?"
            args.append(target)
        rows = self.conn.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]
