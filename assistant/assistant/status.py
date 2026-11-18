"""What the console's Assistant page shows of the running assistant, written by the assistant itself:
`assistant-status.json` beside config.yaml (the data folder, which the console reads; no secret goes in it).

- `config_sha256`: the config.yaml it started with. The page compares it with the file now: saved is not applied
  until the assistant is restarted.
- `slack`: off | connecting | connected (the workspace and the bot as Slack's auth.test named them) | failed (why,
  with anything that looks like a token masked).
- `claude`: what writes her replies (provider: claude | codex), who pays (auth) and how the last reply went: ok | no_key | bad_key | error.
- `at`: refreshed every 30 seconds while it runs (with the Slack connection checked anew), so a page can tell a status that is no longer being kept.

Written whole, by a rename, so the console never reads half a file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

NAME = "assistant-status.json"
TOKENISH = re.compile(r"(xox[abposre]-|xapp-|sk-ant-|perm[:-])[\w.:-]+")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def masked(text: str, limit: int = 300) -> str:
    return TOKENISH.sub(lambda m: m.group(1) + "…", " ".join(str(text).split()))[:limit]


class Status:
    def __init__(self, config_path: Path | None, auth: str, web: bool, slack_on: bool, port: int | None = None,
                 provider: str = "claude"):
        self.path = config_path.parent / NAME if config_path else None
        sha = ""
        if config_path:
            try:
                sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
            except OSError:
                sha = ""
        self.data = {"schema": 1, "pid": os.getpid(), "started_at": now(), "at": now(), "config_sha256": sha,
                     "web": web, "port": port, "slack": {"state": "connecting" if slack_on else "off", "at": now()},
                     "claude": {"provider": provider, "auth": auth, "last": None, "at": None}}
        self.write()

    def slack(self, state: str, team: str = "", bot: str = "", error: str = "") -> None:
        self.data["slack"] = {"state": state, "team": team, "bot": bot, "error": masked(error) if error else "",
                              "at": now()}
        self.write()

    def claude(self, last: str) -> None:
        self.data["claude"].update(last=last, at=now())
        self.write()

    def beat(self) -> None:
        self.write()

    def write(self) -> None:
        if self.path is None:
            return
        self.data["at"] = now()
        tmp = self.path.with_name(f".{NAME}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(self.data, ensure_ascii=False))
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
        except OSError:
            pass      # the page then says the status is not being kept; the assistant goes on


def outcome(kind: str, text: str) -> str:
    """A turn's end, in the page's words."""
    from assistant.brain import BAD_KEY, NO_KEY
    if kind == "done":
        return "ok"
    return {NO_KEY: "no_key", BAD_KEY: "bad_key"}.get(text, "error")
