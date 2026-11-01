"""Visual proof of the agent's work: screenshots and recordings it leaves for the people who review it.

The agent has no credentials for the code host or the tracker, so it cannot upload anything itself. It saves the
files into the task's media folder in the workspace, `.arceus/media/<ticket key>/`, next to a
`media.json` that says what each file shows:

    {"items": [{"file": "404-desktop.png", "caption": "404 page, desktop", "repo": "web"},
               {"file": "retry.webm", "caption": "503, then Retry recovers"}]}

When the runner publishes, it uploads them to each MR (`repo` names the repository a file
belongs to; without it the file goes to every MR of the task) and attaches them to its comment
on the ticket. Only what the manifest lists is taken, and only real images and videos: the
folder is written by the agent, so every entry is checked before anything leaves the server.
The workspace's own `.gitignore` (`/*`) keeps the folder out of every commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

MEDIA_DIR = Path(".arceus") / "media"
MANIFEST = "media.json"
MAX_ITEMS = 12
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_CAPTION = 200

# Extension -> (MIME type, kind). The signature check below keeps a renamed text file from passing.
TYPES = {
    ".png": ("image/png", "image"),
    ".jpg": ("image/jpeg", "image"),
    ".jpeg": ("image/jpeg", "image"),
    ".gif": ("image/gif", "image"),
    ".webp": ("image/webp", "image"),
    ".webm": ("video/webm", "video"),
    ".mp4": ("video/mp4", "video"),
}


def _signature_ok(ext: str, head: bytes) -> bool:
    if ext == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == ".gif":
        return head[:6] in (b"GIF87a", b"GIF89a")
    if ext == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == ".webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    if ext == ".mp4":
        return head[4:8] == b"ftyp"
    return False


@dataclass(frozen=True)
class MediaItem:
    path: Path
    caption: str
    repo: str | None
    sha256: str
    mime: str
    kind: str          # image | video
    # The bytes that were checked. The folder is the agent's: read again at upload, the path could by then be
    # another file or a link to anything on the server.
    content: bytes = field(default=b"", repr=False, compare=False)

    @property
    def upload_name(self) -> str:
        """Unique per content, so a new screenshot with an old name is never mistaken for the old one."""
        return f"{self.sha256[:8]}-{self.path.name}"

    def data(self) -> bytes:
        return self.content


def media_dir(workspace: Path, task_id: str) -> Path:
    return workspace / MEDIA_DIR / task_id


def _caption(value: object, fallback: str) -> str:
    text = " ".join(str(value or "").split()) or fallback
    for ch in "[]()<>`*_|\\":
        text = text.replace(ch, " ")
    return " ".join(text.split())[:MAX_CAPTION]


def _read_plain(path: Path, limit: int) -> bytes:
    """The file's bytes, at most `limit` + 1 of them, refusing a link or anything but a regular file at the moment
    of reading: the checks before it ran on a path the agent may have changed since."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path.name}: not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read(limit + 1)
    finally:
        os.close(fd)


def load(folder: Path) -> tuple[list[MediaItem], list[str]]:
    """The manifest's valid items, and one line for each entry left out (or for a broken manifest)."""
    manifest = folder / MANIFEST
    if not manifest.is_file() or manifest.is_symlink():
        return [], []
    try:
        entries = json.loads(manifest.read_text()).get("items")
    except (OSError, ValueError, AttributeError) as e:
        return [], [f"{MANIFEST}: unreadable ({type(e).__name__})"]
    if not isinstance(entries, list):
        return [], [f"{MANIFEST}: \"items\" is not a list"]
    items: list[MediaItem] = []
    problems: list[str] = []
    seen: set[str] = set()
    total = 0
    root = folder.resolve()
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append(f"{entry!r}: not an object")
            continue
        name = str(entry.get("file") or "")
        if len(items) >= MAX_ITEMS:
            problems.append(f"{name}: more than {MAX_ITEMS} files")
            continue
        path = folder / name
        if not name or Path(name).name != name or name.startswith("."):
            problems.append(f"{name!r}: not a plain file name in the media folder")
            continue
        ext = path.suffix.lower()
        if ext not in TYPES:
            problems.append(f"{name}: only {', '.join(sorted(TYPES))} are uploaded")
            continue
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            problems.append(f"{name}: no such regular file")
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            problems.append(f"{name}: too large ({size // (1024 * 1024)} MB; {MAX_FILE_BYTES // (1024 * 1024)} MB a "
                            f"file, {MAX_TOTAL_BYTES // (1024 * 1024)} MB in all)")
            continue
        try:
            data = _read_plain(path, MAX_FILE_BYTES)
        except OSError:
            problems.append(f"{name}: no such regular file")
            continue
        if len(data) != size:  # it changed after it was measured: it is not the file that was checked
            problems.append(f"{name}: changed while it was read")
            continue
        if not _signature_ok(ext, data[:16]):
            problems.append(f"{name}: the content is not {ext[1:]}")
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        total += size
        mime, kind = TYPES[ext]
        repo = entry.get("repo")
        items.append(MediaItem(path, _caption(entry.get("caption"), path.stem), str(repo) if repo else None,
                               digest, mime, kind, data))
    return items, problems


def for_repo(items: list[MediaItem], repo: str) -> list[MediaItem]:
    return [m for m in items if m.repo in (None, repo)]
