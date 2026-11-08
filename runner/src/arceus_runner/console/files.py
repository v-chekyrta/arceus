"""Editing a text file of the factory from the console: the test instructions, the architecture instructions, the
project's notes. The same rules for each:

- the page carries the hash of the file it showed; a save over a file that changed since (another person, an edit
  over SSH) is refused, and the page shows what is there now against what was sent;
- the write is atomic (a temporary file beside it, flushed, renamed over), keeps the file's mode, and never leaves
  a half-written file for the runner to read;
- every saved version is kept in console.db with who saved it, so any earlier one can be brought back.
"""

from __future__ import annotations

import difflib
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from arceus_runner.console.store import ConsoleStore, User

MAX_FILE = 1024 * 1024
MISSING = "missing"


@dataclass(frozen=True)
class Current:
    text: str
    sha: str           # sha256 of the bytes, or "missing"
    exists: bool
    problem: str = ""  # why it cannot be read or written


class Conflict(Exception):
    def __init__(self, current: Current, diff: str):
        self.current, self.diff = current, diff
        super().__init__("changed since the page was opened")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(path: Path) -> Current:
    try:
        if path.is_symlink():
            return Current("", MISSING, False, f"{path} is a symbolic link: edit it on the server")
        data = path.read_bytes()
    except FileNotFoundError:
        return Current("", MISSING, False)
    except OSError as e:
        return Current("", MISSING, False, f"{path} cannot be read ({e.strerror or e})")
    if len(data) > MAX_FILE:
        return Current("", digest(data), True, f"{path} is larger than {MAX_FILE // 1024} KB: edit it on the server")
    try:
        return Current(data.decode("utf-8"), digest(data), True)
    except UnicodeDecodeError:
        return Current("", digest(data), True, f"{path} is not UTF-8 text: edit it on the server")


def normal(text: str) -> str:
    """What a browser's textarea sends, as the file keeps it: \\n line ends, one at the end."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n" if text.strip() else ""


def diff(before: str, after: str, a: str = "on the server now", b: str = "yours") -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), a, b))


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.console-{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def save(store: ConsoleStore, kind: str, path: Path, text: str, expected_sha: str, by: User, note: str = "") -> bool:
    """Write `text` if the file is still what the page showed (`expected_sha`). Returns False when nothing changed.
    Raises Conflict when it changed meanwhile, OSError when it cannot be written."""
    current = read(path)
    if current.problem:
        raise OSError(current.problem)
    text = normal(text)
    if current.sha != expected_sha:
        raise Conflict(current, diff(current.text, text))
    if current.exists and text == current.text:
        return False
    data = text.encode("utf-8")
    if len(data) > MAX_FILE:
        raise OSError(f"the text is larger than {MAX_FILE // 1024} KB")
    mode = (path.stat().st_mode & 0o777) if current.exists else 0o600
    write_atomic(path, data, mode)
    store.add_version(kind, path, current.sha, current.text if current.exists else None, digest(data), text, by, note)
    store.log(by, by.label, "file_saved", kind, {"path": str(path), "sha": digest(data)[:12],
                                                  "lines": text.count("\n")})
    return True
