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
import struct
from dataclasses import dataclass, field
from pathlib import Path

MEDIA_DIR = Path(".arceus") / "media"
MANIFEST = "media.json"
MAX_ITEMS = 12
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_CAPTION = 200
# The box a screenshot is shown in, in a comment or an MR description: small enough that a report of several
# screenshots reads in a short scroll. The file itself is the original; a click on the preview opens it.
PREVIEW_BOX = (480, 320)

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

    @property
    def preview_width(self) -> int | None:
        """The width to show an image at so it fits PREVIEW_BOX, its proportions kept and never enlarged. Only
        the width is given: both trackers then scale the height with it. None for a video, and for an image whose
        header does not give its size: no width can bound that one's height, so it is not shown in place at all."""
        size = image_size(self.content) if self.kind == "image" else None
        if not size:
            return None
        w, h = size
        return max(1, min(w, PREVIEW_BOX[0], w * PREVIEW_BOX[1] // h))

    @property
    def shown_as(self) -> str:
        """How a report shows it: "player" (a video), "preview" (an image, at preview_width) or "link" (an image
        of unknown size: a link to the original, so it can neither fill the page nor be blown up)."""
        if self.kind != "image":
            return "player"
        return "preview" if self.preview_width else "link"


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) in pixels from a PNG, JPEG, GIF or WebP header; None when the header does not say."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
            size = struct.unpack(">II", data[16:24])
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            size = struct.unpack("<HH", data[6:10])
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            size = _webp_size(data)
        elif data.startswith(b"\xff\xd8"):
            size = _jpeg_size(data)
        else:
            size = None
    except struct.error:
        return None
    return size if size and size[0] > 0 and size[1] > 0 else None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L" and data[20:21] == b"\x2f" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(data) >= 30:
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    return None


# JPEG start-of-frame markers: every SOFn but DHT (C4), JPG (C8) and DAC (CC), which share the range.
_SOF = {*range(0xC0, 0xD0)} - {0xC4, 0xC8, 0xCC}


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0x01, *range(0xD0, 0xDA)):  # markers without a length
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in _SOF:
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if length < 2:
            return None
        i += 2 + length
    return None


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
