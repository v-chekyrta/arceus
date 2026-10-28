"""The agent's answers to the MR threads of a review round, one per thread.

The agent has no code host credentials, so it cannot post in a thread itself. The follow-up names
each thread addressed to it (`T1`, `T2`, …) and the file to answer in,
`.arceus/replies/<ticket key>/<batch id>.json` in the workspace:

    {"replies": [{"thread": "T1", "changed": true, "text": "Moved the timeout into settings."},
                 {"thread": "T2", "changed": false, "text": "Retries live in the client because …"}]}

`changed` says whether the thread's answer is a code change (the runner then adds the commit) or
only an answer. The folder is written by the agent, so the file is read defensively: only threads
of this round, only plain text, and nothing that could pass for the runner's own reply marker.
The workspace's own `.gitignore` (`/*`) keeps the folder out of every commit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


# "Codex: tests are missing", "- **Claude Code:** …": who said a line, at its start. The public text speaks as
# Arceus, so the label goes and the line stays; the same words inside the text are content and are kept.
_ATTRIBUTION = re.compile(r"(?im)^([ \t]*(?:[-*•][ \t]+|\d+[.)][ \t]+)?)(?:\*\*|\[)?"
                          r"(?:codex|claude(?:[ \t]+code)?|bulava)(?:[ \t]*(?::\*\*|\*\*:|\]:?|:))[ \t]*")


def unattributed(text: str) -> str:
    """The text without the model or engine labels that start its lines (_ATTRIBUTION)."""
    return _ATTRIBUTION.sub(r"\1", text or "")


REPLIES_DIR = Path(".arceus") / "replies"
NOTES_DIR = Path(".arceus") / "notes"
MAX_FILE_BYTES = 256 * 1024
MAX_TEXT = 6000
_MARKER = re.compile(r"<!--\s*arceus", re.IGNORECASE)


@dataclass(frozen=True)
class Reply:
    text: str
    changed: bool


def replies_file(workspace: Path, task_id: str, batch_id: str) -> Path:
    return workspace / REPLIES_DIR / task_id / f"{batch_id}.json"


def decisions_file(workspace: Path, task_id: str) -> Path:
    """Where the agent writes down what it chose and why while it works, for the questions that come later."""
    return workspace / NOTES_DIR / task_id / "decisions.md"


def load(path: Path, threads: set[str]) -> tuple[dict[str, Reply], list[str]]:
    """The valid answer for each thread of the round, and one line for each entry left out."""
    if not path.is_file() or path.is_symlink():
        return {}, []
    if path.stat().st_size > MAX_FILE_BYTES:
        return {}, [f"{path.name}: larger than {MAX_FILE_BYTES} bytes"]
    try:
        entries = json.loads(path.read_text()).get("replies")
    except (OSError, ValueError, AttributeError) as e:
        return {}, [f"{path.name}: unreadable ({type(e).__name__})"]
    if not isinstance(entries, list):
        return {}, [f"{path.name}: \"replies\" is not a list"]
    out: dict[str, Reply] = {}
    problems: list[str] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            problems.append(f"entry {i}: not an object")
            continue
        thread, text = e.get("thread"), e.get("text")
        if not isinstance(thread, str) or thread not in threads:
            problems.append(f"entry {i}: {thread!r} is not a thread of this round")
            continue
        if thread in out:
            problems.append(f"entry {i}: a second answer for {thread}")
            continue
        if not isinstance(text, str) or not text.strip():
            problems.append(f"entry {i}: no text")
            continue
        text = _MARKER.sub("<!-- ", text.strip())
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT].rstrip() + " …"
        out[thread] = Reply(text, e.get("changed") is True)
    return out, problems
