"""Attachments: what a file is, and what of it the assistant can read.

Every upload ends up as one of these for Claude:
- an image file it opens with Read (Claude sees images);
- text extracted beside the file (`<name>.txt`): documents, sheets, slides, mail, archives' listings;
- a transcript of the speech in an audio or video file, plus a few frames of a video;
- or only its name, type and size, said plainly, when nothing in it can be read.

Extraction is synchronous and runs in a worker thread (server.py). Nothing here executes the file.
"""

from __future__ import annotations

import csv
import email
import email.policy
import html.parser
import io
import json
import mimetypes
import re
import subprocess
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_FILES_PER_MESSAGE = 10
INLINE_TEXT_CHARS = 6000      # extracted text up to this goes into the prompt itself; longer is read from the file
EXTRACT_CAP_CHARS = 400_000   # never write more than this of extracted text
SHEET_ROWS_CAP = 2000

IMAGE_FOR_CLAUDE = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
IMAGE_CONVERT = {".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif", ".ico"}
AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac", ".amr", ".wma", ".weba"}
VIDEO = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".3gp", ".wmv"}
TEXT = {".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".conf", ".toml",
        ".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".scss", ".sql", ".sh", ".java", ".kt", ".swift", ".go",
        ".rb", ".php", ".c", ".h", ".cpp", ".rs", ".srt", ".vtt", ".ics", ".svg", ".rtf"}
TABLE_TEXT = {".csv", ".tsv"}
HTML = {".html", ".htm"}

# Served inline (viewable in the browser) only for these: anything else is a download, so an uploaded HTML or SVG
# never runs as a page on the console's origin.
INLINE_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf",
               "audio/mpeg", "audio/mp4", "audio/aac", "audio/wav", "audio/x-wav", "audio/ogg", "audio/opus",
               "audio/webm", "audio/flac", "video/mp4", "video/webm", "video/quicktime", "video/ogg"}

KIND_WORDS = {"image": "зображення", "audio": "аудіо", "video": "відео", "pdf": "PDF", "document": "документ Word",
              "sheet": "таблиця", "slides": "презентація", "text": "текст", "mail": "лист", "archive": "архів",
              "other": "файл"}


@dataclass
class Result:
    kind: str
    text_path: str | None = None       # extracted text beside the file
    text_chars: int = 0
    transcript: str | None = None
    frames: list[str] = field(default_factory=list)
    view_path: str | None = None       # the image Claude opens (the file itself, or a PNG made from it)
    duration_s: float | None = None
    note: str = ""                     # what could not be read, in plain words


def safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name.strip() or "файл"
    name = re.sub(r"[\x00-\x1f/]", "_", name)
    if name.startswith("."):
        name = "_" + name
    return name[:150]


def guess_mime(name: str, declared: str | None) -> str:
    guessed = mimetypes.guess_type(name)[0]
    declared = (declared or "").split(";")[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return declared
    return guessed or "application/octet-stream"


def kind_of(name: str, mime: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_FOR_CLAUDE or ext in IMAGE_CONVERT:
        return "image"
    if ext in AUDIO:
        return "audio"
    if ext in VIDEO:
        return "audio" if mime.startswith("audio/") else "video"   # a voice note recorded in a browser is .webm
    if ext == ".pdf":
        return "pdf"
    if ext in (".docx", ".docm"):
        return "document"
    if ext in (".xlsx", ".xlsm"):
        return "sheet"
    if ext == ".pptx":
        return "slides"
    if ext == ".eml":
        return "mail"
    if ext == ".zip":
        return "archive"
    if ext in TEXT or ext in TABLE_TEXT or ext in HTML:
        return "text"
    top = mime.split("/")[0]
    if top == "image" and mime != "image/svg+xml":
        return "image"
    if top in ("audio", "video"):
        return top
    if top == "text":
        return "text"
    return "other"


def inline_mime(mime: str) -> bool:
    return mime in INLINE_MIME


# --------------------------------------------------------------------------------------------------------- extract
def process(path: Path, mime: str, transcriber: "Transcriber | None") -> Result:
    """Make what Claude needs from the file at `path`. Never raises for a bad file: says so in `note`."""
    kind = kind_of(path.name, mime)
    try:
        return _PROCESSORS.get(kind, _other)(path, kind, transcriber)
    except Exception as e:  # noqa: BLE001 - a broken file must not break the conversation
        return Result(kind=kind, note=f"Файл не вдалося прочитати ({type(e).__name__}).")


def _write_text(path: Path, text: str, res: Result) -> Result:
    text = text.strip()
    if not text:
        return res
    cut = len(text) > EXTRACT_CAP_CHARS
    text = text[:EXTRACT_CAP_CHARS]
    out = path.with_name(path.name + ".txt")
    out.write_text(text + ("\n\n[… текст обрізано]" if cut else ""), encoding="utf-8")
    res.text_path, res.text_chars = str(out), len(text)
    return res


def _image(path: Path, kind: str, _t) -> Result:
    if path.suffix.lower() in IMAGE_FOR_CLAUDE:
        return Result(kind=kind, view_path=str(path))
    png = path.with_name(path.name + ".png")
    run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(path), "-frames:v", "1", str(png)], 60)
    if png.exists() and png.stat().st_size:
        return Result(kind=kind, view_path=str(png))
    return Result(kind=kind, note="Цей формат зображення не вдалося відкрити.")


def _media(path: Path, kind: str, transcriber) -> Result:
    res = Result(kind=kind, duration_s=probe_duration(path))
    if kind == "video":
        res.frames = video_frames(path, res.duration_s)
    if transcriber is None:
        res.note = "Розшифровка голосу на цьому сервері не налаштована."
        return res
    pcm = decode_pcm(path)
    if pcm is None:
        res.note = "У файлі немає звукової доріжки, яку можна розшифрувати." if kind == "video" else \
            "Звук у файлі не вдалося декодувати."
        return res
    res.transcript = transcriber.transcribe(pcm)
    if not res.transcript:
        res.note = "Мови в записі не розпізнано."
    return res


def _pdf(path: Path, kind: str, _t) -> Result:
    res = Result(kind=kind)
    out = run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"], 120)
    text = out.stdout.decode("utf-8", "replace") if out else ""
    if text.strip():
        return _write_text(path, text, res)
    res.note = "Тексту в PDF немає (схоже, скан): відкрий сам файл через Read, щоб побачити сторінки."
    return res


def _docx(path: Path, kind: str, _t) -> Result:
    import docx  # python-docx
    d = docx.Document(str(path))
    parts: list[str] = []
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            t = "".join(n.text or "" for n in child.iter() if n.tag.endswith("}t"))
            parts.append(t)
        elif tag == "tbl":
            for row in child.iter():
                if row.tag.endswith("}tr"):
                    cells = []
                    for cell in row.iterchildren():
                        if cell.tag.endswith("}tc"):
                            cells.append(" ".join("".join(n.text or "" for n in p.iter() if n.tag.endswith("}t"))
                                                  for p in cell.iter() if p.tag.endswith("}p")).strip())
                    parts.append(" | ".join(cells))
            parts.append("")
    return _write_text(path, "\n".join(parts), Result(kind=kind))


def _xlsx(path: Path, kind: str, _t) -> Result:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    try:
        for ws in wb.worksheets:
            buf.write(f"\n## Аркуш «{ws.title}»\n")
            n = 0
            for row in ws.iter_rows(values_only=True):
                if all(v is None for v in row):
                    continue
                w.writerow(["" if v is None else v for v in row])
                n += 1
                if n >= SHEET_ROWS_CAP:
                    buf.write(f"[… далі рядки не показано, аркуш довший за {SHEET_ROWS_CAP}]\n")
                    break
    finally:
        wb.close()
    return _write_text(path, buf.getvalue(), Result(kind=kind))


def _pptx(path: Path, kind: str, _t) -> Result:
    import pptx
    prs = pptx.Presentation(str(path))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        parts.append(f"\n## Слайд {i}")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            parts.append("Нотатки: " + slide.notes_slide.notes_text_frame.text.strip())
    return _write_text(path, "\n".join(parts), Result(kind=kind))


def _mail(path: Path, kind: str, _t) -> Result:
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    head = "\n".join(f"{h}: {msg[h]}" for h in ("From", "To", "Cc", "Date", "Subject") if msg[h])
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        text = body.get_content()
        if body.get_content_type() == "text/html":
            text = html_to_text(text)
    names = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    tail = ("\n\nВкладення листа (вміст не розкрито): " + ", ".join(names)) if names else ""
    return _write_text(path, f"{head}\n\n{text}{tail}", Result(kind=kind))


def _zip(path: Path, kind: str, _t) -> Result:
    with zipfile.ZipFile(path) as z:
        rows = [f"{i.filename}\t{human_size(i.file_size)}" for i in z.infolist() if not i.is_dir()]
    text = f"Архів містить {len(rows)} файлів (вміст не розпаковано):\n" + "\n".join(rows[:2000])
    return _write_text(path, text, Result(kind=kind, note="Файли всередині архіву не розпаковано, видно лише їх список."))


def _text(path: Path, kind: str, _t) -> Result:
    raw = path.read_bytes()
    text = decode_text(raw)
    if text is None:
        return Result(kind="other", note="Файл не текстовий, і його вміст прочитати не вдалося.")
    if path.suffix.lower() in HTML:
        text = html_to_text(text)
    return _write_text(path, text, Result(kind=kind))


def _other(path: Path, kind: str, _t) -> Result:
    with path.open("rb") as f:
        head = f.read(64 * 1024)
    if decode_text(head) is not None and path.stat().st_size <= 5 * 1024 * 1024:
        res = _text(path, "text", None)
        if res.text_path:
            return res
    return Result(kind="other", note="Вміст цього формату асистент прочитати не може: відомі лише назва, тип і розмір.")


_PROCESSORS = {"image": _image, "audio": _media, "video": _media, "pdf": _pdf, "document": _docx, "sheet": _xlsx,
               "slides": _pptx, "mail": _mail, "archive": _zip, "text": _text, "other": _other}


# --------------------------------------------------------------------------------------------------------- helpers
def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None


def decode_text(raw: bytes) -> str | None:
    if b"\x00" in raw[:8192]:
        return None
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


class _Strip(html.parser.HTMLParser):
    SKIP = {"script", "style", "head", "noscript"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article"}

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(src: str) -> str:
    p = _Strip()
    p.feed(src)
    return re.sub(r"\n\s*\n+", "\n\n", "".join(p.out)).strip()


def human_size(n: int) -> str:
    for unit, step in (("Б", 1), ("КБ", 1024), ("МБ", 1024 ** 2), ("ГБ", 1024 ** 3)):
        if n < step * 1024 or unit == "ГБ":
            return f"{n / step:.0f} {unit}" if unit in ("Б", "КБ") else f"{n / step:.1f} {unit}"
    return f"{n} Б"


def probe_duration(path: Path) -> float | None:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], 30)
    try:
        return round(float(json.loads(out.stdout)["format"]["duration"]), 1) if out else None
    except (ValueError, KeyError, TypeError):
        return None


def decode_pcm(path: Path):
    """16 kHz mono float32 samples of the file's audio, or None when it has none."""
    import numpy as np
    out = run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-vn", "-f", "s16le", "-ac", "1",
               "-ar", "16000", "-"], 900)
    if not out or out.returncode != 0 or len(out.stdout) < 3200:
        return None
    return np.frombuffer(out.stdout, np.int16).astype(np.float32) / 32768.0


def video_frames(path: Path, duration: float | None, count: int = 4) -> list[str]:
    frames = []
    d = duration or 0
    for i in range(count):
        at = d * (i + 0.5) / count if d else 0
        out = path.with_name(f"{path.name}.frame{i + 1}.jpg")
        run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", str(path), "-frames:v", "1",
             "-vf", "scale='min(1280,iw)':-2", str(out)], 60)
        if out.exists() and out.stat().st_size:
            frames.append(str(out))
        if not d:
            break
    return frames


class Transcriber:
    """faster-whisper, loaded on first use and shared; one transcription at a time (the VM has 4 cores)."""

    def __init__(self, model: str = "medium", models_dir: Path | None = None, threads: int = 4):
        self.model_name, self.models_dir, self.threads = model, models_dir, threads
        self._model = None
        self._lock = threading.Lock()

    def transcribe(self, pcm) -> str:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                           cpu_threads=self.threads,
                                           download_root=str(self.models_dir) if self.models_dir else None)
            segments, _info = self._model.transcribe(
                pcm, vad_filter=True, beam_size=5,
                initial_prompt="Розмова про проєкт: курси, сторінки, кнопки, тікети, клієнт, баг.")
            return " ".join(s.text.strip() for s in segments).strip()
