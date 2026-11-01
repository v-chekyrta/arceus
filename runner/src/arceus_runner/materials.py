"""Files from the ticket for the agent: the mockups, specifications and logs people attach to an issue and its comments.

The agent has no credentials for the tracker, so the runner fetches the files for it, in three steps:

1. **Fetch** into the runner's memory, never to a place the agent can see: each file the tracker lists for the
   delivery, within the limits below. A file of a kind the security screen cannot read (an archive, a Word or
   Figma file, a video) is not downloaded at all. A delivery that is tried again (a crash, a released hold)
   fetches again: the same bytes are the same screened content.
2. **Screen** with the text it goes with: images and PDFs as the classifier sees them, text files as text
   (screening.Gate.clear with `files`). A held text holds its files with it.
3. **Publish** only after the clearance: the screened bytes are copied into the workspace, `.arceus/ticket/<KEY>/`,
   next to `materials.json`, and the text tells the agent where they are and what each one is.

Every file the tracker listed is in that text, given to the agent or not: one that is too large, of a kind the
screen cannot read, or that the bot may not read is named with the reason, so the agent can ask for it instead
of working as if it were not there. Nothing here holds a download address: the tracker's may carry a signed token.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from arceus_runner.http import AuthError, NotFound, ServiceError, TooLarge
from arceus_runner.integrations import Attachment, IssueTracker, key_part
from arceus_runner.screening import CALL_FILE_BYTES, MAX_CHARS, ScreenFile

TICKET_DIR = Path(".arceus") / "ticket"
MANIFEST = "materials.json"
MAX_FILES = 20                       # given to the agent in one delivery; the rest are named only
MAX_TOTAL_BYTES = 48 * 1024 * 1024   # one delivery, all files; the screen takes them over several calls
# Bytes a file. A PDF or image goes to the screen whole in one call, so none is larger than a call may carry.
LIMITS = {"image": 12 * 1024 * 1024, "pdf": CALL_FILE_BYTES, "text": 512 * 1024}
KINDS = {".png": ("image", "image/png"), ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
         ".gif": ("image", "image/gif"), ".webp": ("image", "image/webp"), ".pdf": ("pdf", "application/pdf")}
TEXT = {".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".yaml", ".yml", ".xml", ".log", ".html", ".htm",
        ".svg", ".diff", ".patch", ".sql", ".toml", ".ini"}
KIND_WORDS = {"image": "image", "pdf": "PDF", "text": "text"}
SECTION_HEAD = "## Files from the ticket"
PDF_WHOLE_BYTES = 8 * 1024 * 1024  # PDFs in one delivery past which the agent is told to read them by pages


def _signature_ok(kind: str, mime: str, head: bytes) -> bool:
    if kind == "pdf":
        return head.startswith(b"%PDF-")
    if mime == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return head[:6] in (b"GIF87a", b"GIF89a")
    if mime == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


def kind_of(name: str) -> tuple[str, str] | None:
    """(kind, MIME type) by the file's extension, or None for one the screen cannot read."""
    ext = Path(name).suffix.lower()
    if ext in KINDS:
        return KINDS[ext]
    if ext in TEXT:
        return "text", "text/plain"
    return None


def local_name(a: Attachment) -> str:
    """The file's name on disk: the tracker's id first, so two files with one name stay two, then the name as people
    wrote it with anything unsafe for a path replaced."""
    stem, ext = Path(a.name).stem, Path(a.name).suffix.lower()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "file"
    ext = ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else ""
    return f"{key_part(a.id)}-{safe[:60]}{ext}"


def _when(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M UTC") if ms else "unknown time"


@dataclass(frozen=True)
class Material:
    """One file the tracker listed, and what became of it."""
    attachment: Attachment
    where: str           # "the ticket", "a comment by j.doe on 2026-10-30 14:02 UTC"
    file: str            # local name; "" when it was not fetched
    kind: str            # image | pdf | text | "" (a kind the screen cannot read)
    mime: str
    status: str          # ok | skipped | unavailable
    reason: str = ""     # why not ok, in words for the agent and people
    data: bytes = b""

    @property
    def attached(self) -> str:
        """"attached to the ticket by j.doe"; a file in someone's comment by that same person is not said twice."""
        author = self.attachment.author or "someone unknown"
        if self.where.startswith(f"a comment by {author} ") or self.where == f"a comment by {author}":
            return f"attached to {self.where}"
        return f"attached to {self.where} by {author}"

    def screen_file(self) -> ScreenFile:
        return ScreenFile(self.file, self.kind, self.mime, self.data, self.attached)

    def entry(self) -> dict:
        a = self.attachment
        return {"file": self.file or None, "name": a.name, "kind": self.kind or None, "status": self.status,
                "reason": self.reason or None, "attached_to": self.where, "author": a.author, "created": a.created,
                "id": a.id, "comment_id": a.comment_id, "size": len(self.data) if self.data else a.size}


def fetch(tracker: IssueTracker, attachments: list[Attachment], where: dict[str | None, str]) -> list[Material]:
    """Download the files a delivery gives the agent, within the limits; every one listed comes back with its status.

    `where` names what each file is attached to, by comment id (None: the ticket). A transient failure of the
    tracker raises: the delivery is tried again on a later tick, never sent with a file missing for no reason.
    """
    out: list[Material] = []
    given = total = 0
    for a in attachments:
        place = where.get(a.comment_id, "the ticket")
        known = kind_of(a.name)
        if known is None:
            out.append(Material(a, place, "", "", a.mime, "skipped",
                                f"{Path(a.name).suffix or 'files without an extension'} files cannot be read by the "
                                "security screen, so they are not given to the agent"))
            continue
        kind, mime = known
        limit = min(LIMITS[kind], MAX_TOTAL_BYTES - total)
        if given >= MAX_FILES:
            out.append(Material(a, place, "", kind, mime, "skipped", f"more than {MAX_FILES} files at once"))
            continue
        if a.size > limit:
            out.append(Material(a, place, "", kind, mime, "skipped", _too_large(kind, a.size, total)))
            continue
        try:
            data = tracker.download(a, limit)
        except TooLarge:
            out.append(Material(a, place, "", kind, mime, "skipped", _too_large(kind, limit + 1, total)))
            continue
        except AuthError:
            out.append(Material(a, place, "", kind, mime, "unavailable", "the runner's account may not read it"))
            continue
        except NotFound:
            out.append(Material(a, place, "", kind, mime, "unavailable", "it is gone from the tracker"))
            continue
        except ValueError as e:
            out.append(Material(a, place, "", kind, mime, "unavailable", str(e)))
            continue
        except ServiceError as e:
            if e.status is not None and 400 <= e.status < 500:  # refused for good (BadRequest): not a passing outage
                out.append(Material(a, place, "", kind, mime, "unavailable", f"the tracker refused it (HTTP {e.status})"))
                continue
            raise
        if kind == "text":
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                out.append(Material(a, place, "", kind, mime, "skipped", "not UTF-8 text"))
                continue
        elif not _signature_ok(kind, mime, data[:16]):
            out.append(Material(a, place, "", kind, mime, "skipped", f"its content is not {Path(a.name).suffix[1:]}"))
            continue
        given += 1
        total += len(data)
        out.append(Material(a, place, local_name(a), kind, mime, "ok", data=data))
    return out


def _too_large(kind: str, size: int, total: int) -> str:
    mb = 1024 * 1024
    if size > LIMITS[kind]:
        return f"larger than {LIMITS[kind] // mb} MB, the limit for a {KIND_WORDS[kind]} file"
    return f"the files before it already take {total // mb} of the {MAX_TOTAL_BYTES // mb} MB one delivery may carry"


def screen_files(items: list[Material]) -> tuple[ScreenFile, ...]:
    return tuple(m.screen_file() for m in items if m.status == "ok")


def folder(workspace: Path, task_id: str) -> Path:
    return workspace / TICKET_DIR / task_id


def publish(items: list[Material], workspace: Path, task_id: str, fresh: bool = False) -> None:
    """Put the screened files where the agent reads them, and add them to the folder's manifest. `fresh`: the task
    starts, so whatever an earlier task of the same key left there goes first.

    The folder is in the agent's workspace, so the agent may have put a symbolic link anywhere on the way to it or in
    it. Nothing here follows one: each folder is opened without following links (a link or a file in its place is
    removed and a real folder made), every file is written under a fresh name that cannot exist yet and renamed over
    the old one, which replaces a link rather than writing through it.
    """
    fd = _open_folder(workspace, [*TICKET_DIR.parts, task_id])
    try:
        if fresh:
            for name in os.listdir(fd):
                if not stat.S_ISDIR(os.lstat(name, dir_fd=fd).st_mode):
                    os.unlink(name, dir_fd=fd)
        for m in items:
            if m.status == "ok":
                _write(fd, m.file, m.data)
        entries: dict[str, dict] = {}
        try:
            old = os.open(MANIFEST, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(old, "rb") as f:
                entries = {e["id"]: e for e in json.loads(f.read(1024 * 1024)).get("items", [])}
        except (OSError, ValueError, AttributeError, KeyError, TypeError):
            entries = {}
        for m in items:
            entries[m.attachment.id] = m.entry()
        _write(fd, MANIFEST, (json.dumps({"items": list(entries.values())}, indent=2, ensure_ascii=False) + "\n").encode())
    finally:
        os.close(fd)


def _open_folder(root: Path, parts: list[str]) -> int:
    """A descriptor of root/parts..., each part a real folder: whatever else stands in its place is removed."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as e:
                if e.errno not in (errno.ELOOP, errno.ENOTDIR):
                    raise
                os.unlink(part, dir_fd=fd)  # a link or a file where the folder belongs: never followed
                os.mkdir(part, 0o755, dir_fd=fd)
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write(fd: int, name: str, data: bytes) -> None:
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
    try:
        with os.fdopen(out, "wb") as f:
            f.write(data)
        os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=fd)
        except FileNotFoundError:
            pass
        raise


# `![](name)` or `[text](name)`: the name in <angle brackets> (it may hold spaces), or bare with balanced
# parentheses in it ("Screenshot (2).png"); an optional "title" after it.
_REF = re.compile(r"!?\[[^\]]*\]\(\s*(?:<([^<>\n]+)>|((?:[^()\s<>]|\([^()\s]*\))+))(?:\s+\"[^\"]*\")?\s*\)")

Text = tuple[str, str | None, str]  # (what it is, for the agent: "the description"; the comment's id or None; its text)


def references(texts: list[Text], items: list[Material], target: Path) -> list[str]:
    """Which file each in-place reference in the text means (`![](mockup.png)`). The tracker shows a file named in a
    comment from that comment's own files, and one named in the description from the ticket's; a name only found
    elsewhere is taken from there when exactly one file has it. Two files that both fit are said to be ambiguous,
    never resolved by a guess."""
    lines = []
    for where, owner, text in texts:
        for name in dict.fromkeys(unquote(m.group(1) or m.group(2)) for m in _REF.finditer(text or "")):
            named = [m for m in items if m.attachment.name == name]
            if not named or "://" in name:
                continue
            own = [m for m in named if m.attachment.comment_id == owner]
            fit = own or named
            if len(fit) == 1:
                lines.append(f"- `{name}` in {where}: {_describe(fit[0], target)}")
            else:
                lines.append(f"- `{name}` in {where} is ambiguous: {len(fit)} files have that name "
                             f"({'; '.join(_describe(m, target) for m in fit)}). Do not pick one: if it matters, "
                             "say which you used and why, or ask which is meant.")
    return lines


def _describe(m: Material, target: Path) -> str:
    if m.status == "ok":
        return f"`{target / m.file}`, {m.attached}"
    return f"the file {m.attached}, not given to you"


def _chars(m: Material) -> int:
    return len(m.data.decode("utf-8", "replace")) if m.kind == "text" and m.status == "ok" else 0


def with_section(text: str, items: list[Material], target: Path, texts: list[Text] = (),
                 after: str = "") -> tuple[str, list[Material]]:
    """`text` with the files section after it, then `after` (what goes to the agent below the files), and the files
    as given. The whole message counts: the screen reads at most MAX_CHARS of a text
    and its text files together and gives anything longer to a person whole, so while they would go past that,
    the longest text file is left out, with the reason, instead of the whole delivery waiting for a person. A text
    too long by itself is left as it is: that is for a person."""
    items = list(items)
    while True:
        out = text + ("\n\n" + section(items, target, texts) if items else "") + (f"\n\n{after}" if after else "")
        used = len(out) + sum(_chars(m) for m in items)
        longest = max(range(len(items)), key=lambda i: _chars(items[i]), default=None)
        if used <= MAX_CHARS or longest is None or not _chars(items[longest]):
            return out, items
        m = items[longest]
        items[longest] = dataclasses.replace(
            m, file="", status="skipped", data=b"",
            reason=f"{_chars(m)} characters, more than the security screen reads together with the rest of this "
                   f"text ({MAX_CHARS} in all); paste the part that matters into a comment")


def given_in(text: str, items: list[Material], target: Path) -> list[Material]:
    """The files `text` gives the agent, out of `items` fetched again for it: one the text does not name as given
    (attached or left out since the text was written) is not published or screened with it."""
    return [m for m in items if m.status == "ok" and f": `{target / m.file}` (" in text]


def section(items: list[Material], target: Path, texts: list[Text] = ()) -> str:
    """What the text for the agent says about the files: where the given ones are, which file each in-place reference
    in `texts` means, and why the others are not there."""
    if not items:
        return ""
    lines = [SECTION_HEAD, "",
             f"The runner fetched the files people attached and screened them with this text. They are in `{target}` "
             f"(listed in `{MANIFEST}` there). Open the ones the task needs: images and PDFs with your Read tool.", ""]
    if sum(len(m.data) for m in items if m.kind == "pdf") > PDF_WHOLE_BYTES:
        lines += ["Read the PDFs a few pages at a time (Read's `pages`, as \"1-5\"), not whole: a conversation holds "
                  "about 18 MB of PDFs read whole, and past that the earliest are dropped from it without a word.", ""]
    for m in items:
        a = m.attachment
        about = f"`{a.name}`, {m.attached}"
        if m.status == "ok":
            lines.append(f"- {about}: `{target / m.file}` ({KIND_WORDS[m.kind]})")
        else:
            lines.append(f"- {about}: not given to you, {m.reason}")
    shown = references(list(texts), items, target)
    if shown:
        lines += ["", "Files the text shows in place by name, and which file each one is:", *shown]
    if any(m.status != "ok" for m in items):
        lines += ["", "If the task depends on a file that was not given to you, say so or ask for it in a form you can "
                      "get (a PNG or PDF export, the text pasted into a comment). Never guess what it contains."]
    return "\n".join(lines)
