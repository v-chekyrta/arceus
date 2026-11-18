"""Reading the code and this conversation's files, for a model that has no file tools of its own (Codex without its
shell, codex_brain.py). The roots come from the assistant's server for each turn (ASSISTANT_READ_ROOTS: the code
mirror, then this conversation's upload folder), never from the model: every path is resolved, links included, and
refused unless it lies inside one of them. Relative paths are read against the first root (the code).
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

MAX_READ = 200_000          # characters of one read
MAX_LINES = 2000
MAX_LIST = 500
MAX_HITS = 200
MAX_FILE = 2_000_000        # a file larger than this is not searched
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", ".next", ".gradle"}

TOOLS = [
    {"name": "read_file",
     "description": "Читає текстовий файл коду проєкту чи файлу з цієї розмови. path — відносно кореня коду (як "
                    "показує list_dir) або повний шлях до файлу розмови. offset — номер першого рядка (з 1), limit — "
                    "скільки рядків (до 2000).",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"},
                                                      "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "list_dir",
     "description": "Показує вміст теки коду проєкту (path відносно кореня коду; порожній — корінь) або теки файлів "
                    "цієї розмови. Теки позначено «/».",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    {"name": "search_code",
     "description": "Шукає регулярний вираз у коді проєкту (як grep): повертає файл:рядок: текст. path — тека чи файл "
                    "для пошуку (порожній — увесь код), glob — фільтр імен файлів, наприклад *.py.",
     "inputSchema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"},
                                                      "glob": {"type": "string"}}, "required": ["pattern"]}},
]
TOOLS.append(
    {"name": "view_image",
     "description": "Показує картинку з коду проєкту чи з цієї розмови (png, jpg, gif, webp), щоб ти її побачила. "
                    "path — як дає list_dir.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}})
NAMES = {t["name"] for t in TOOLS}
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_IMAGE = 8_000_000


class Refused(Exception):
    """A path outside the roots, or one that is not there: said to the model in words."""


class FileTools:
    def __init__(self, roots: list[Path]):
        self.roots = [r.resolve() for r in roots if r.is_dir()]

    def resolve(self, path: str) -> Path:
        if not self.roots:
            raise Refused("Код зараз недоступний.")
        raw = Path(path or ".")
        p = (raw if raw.is_absolute() else self.roots[0] / raw).resolve()
        if not any(p == r or r in p.parents for r in self.roots):
            raise Refused("Цей шлях поза кодом проєкту й файлами розмови: його не читаю.")
        if not p.exists():
            raise Refused(f"Немає такого: {path}")
        return p

    def shown(self, p: Path) -> str:
        """A path as the model should give it back: relative to the code, or full for a conversation's file."""
        code = self.roots[0]
        return str(p.relative_to(code)) if p == code or code in p.parents else str(p)

    def call(self, name: str, args: dict) -> str:
        if name == "read_file":
            return self.read(str(args.get("path") or ""), int(args.get("offset") or 1), int(args.get("limit") or 0))
        if name == "list_dir":
            return self.list(str(args.get("path") or ""))
        if name == "view_image":
            return self.image(str(args.get("path") or ""))
        if name == "search_code":
            return self.search(str(args.get("pattern") or ""), str(args.get("path") or ""),
                               str(args.get("glob") or ""))
        raise Refused(f"Немає інструмента {name}")

    def read(self, path: str, offset: int, limit: int) -> str:
        p = self.resolve(path)
        if p.is_dir():
            raise Refused(f"{path} — тека: подивись її через list_dir.")
        data = p.read_bytes()[:MAX_FILE]
        if b"\0" in data[:4096]:
            raise Refused(f"{path} — не текст ({p.suffix or 'двійковий'} файл).")
        lines = data.decode("utf-8", "replace").splitlines()
        start = max(offset, 1)
        count = min(limit or MAX_LINES, MAX_LINES)
        chunk = lines[start - 1:start - 1 + count]
        body = "\n".join(f"{start + i}\t{line}" for i, line in enumerate(chunk))[:MAX_READ]
        more = len(lines) - (start - 1 + len(chunk))
        return body + (f"\n… ще {more} рядків (offset={start + len(chunk)})" if more > 0 else "")

    def image(self, path: str) -> dict:
        """{"image": base64, "mime": …}: shown to the model as the picture itself."""
        import base64
        p = self.resolve(path)
        mime = IMAGES.get(p.suffix.lower())
        if p.is_dir() or mime is None:
            raise Refused(f"{path} — не картинка (png, jpg, gif, webp).")
        if p.stat().st_size > MAX_IMAGE:
            raise Refused(f"{path} — завелика картинка.")
        return {"image": base64.b64encode(p.read_bytes()).decode("ascii"), "mime": mime}

    def list(self, path: str) -> str:
        p = self.resolve(path)
        if not p.is_dir():
            raise Refused(f"{path} — файл: прочитай його через read_file.")
        names = sorted(os.listdir(p))[:MAX_LIST]
        out = [n + ("/" if (p / n).is_dir() else "") for n in names if n not in (".git",)]
        return f"{self.shown(p) or '.'}:\n" + "\n".join(out) if out else f"{self.shown(p)}: порожньо"

    def search(self, pattern: str, path: str, glob: str) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise Refused(f"Неправильний вираз: {e}") from None
        base = self.resolve(path)
        files = [base] if base.is_file() else (
            Path(d) / f for d, dirs, fs in os.walk(base)
            for _ in [dirs.__setitem__(slice(None), [x for x in dirs if x not in SKIP_DIRS])] for f in fs)
        hits: list[str] = []
        for f in files:
            if glob and not fnmatch.fnmatch(f.name, glob):
                continue
            try:
                real = f.resolve()
                if not any(real == r or r in real.parents for r in self.roots) or real.stat().st_size > MAX_FILE:
                    continue
                data = real.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:4096]:
                continue
            for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{self.shown(f)}:{n}: {line.strip()[:300]}")
                    if len(hits) >= MAX_HITS:
                        return "\n".join(hits) + f"\n… понад {MAX_HITS} збігів: звузь path чи glob"
        return "\n".join(hits) or "Нічого не знайдено."
