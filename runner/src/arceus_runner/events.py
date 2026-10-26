"""events.jsonl: one JSON object per line, for Loki."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1


class EventLog:
    def __init__(self, path: Path, project: str, redact: list[str] | None = None):
        self.path = path
        self.project = project
        self._redact = [s for s in (redact or []) if len(s) >= 8]
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        self._f = os.fdopen(fd, "a", encoding="utf-8")

    def close(self) -> None:
        self._f.close()

    def _clean(self, text: str) -> str:
        for s in self._redact:
            text = text.replace(s, "<redacted>")
        return text

    def redact(self, text: str) -> str:
        """The runner's own tokens taken out of a text it is about to show anywhere."""
        return self._clean(text)

    def emit(self, event: str, **fields: object) -> dict:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "v": SCHEMA_VERSION,
            "project": self.project,
            "source": "runner",
            "event": event,
            **{k: v for k, v in fields.items() if v is not None},
        }
        line = self._clean(json.dumps(record, ensure_ascii=False, default=str))
        self._f.write(line + "\n")
        self._f.flush()
        return record
