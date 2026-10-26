"""Which models and depth the agent's tasks actually ran on, from the CLIs' own transcripts.

    /opt/arceus-runner/venv/bin/python -I model-history.py [--since 2026-10-01] [--sessions]

The runner's `task_dispatched` events say what config.yaml asked for; `auto` there means the CLI
chose. This reads what was used: Claude writes `message.model` and `effort` on every answer in
~/.claude/projects/*/*.jsonl, Codex writes `model` and `effort` in each `turn_context` of
~/.codex/sessions/**/rollout-*.jsonl. A session belongs to the task the runner last dispatched or
continued before it started (the runner takes one task at a time). An older CLI that wrote no depth
shows `?`: unknown, not the default. Read-only. Exit 0 when it read anything, 1 when it found nothing.
"""

import argparse
import json
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from arceus_runner.config import load_config

CONFIG = Path("~/arceus/data/config.yaml").expanduser()
STARTS = ("task_dispatched", "task_continued")


def ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def records(path: Path, marker: str):
    """The JSON lines of a transcript that contain marker; a damaged line is skipped, not fatal."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if marker in line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
    except OSError:
        return


def ours(cwd: str, roots: list[str]) -> bool:
    return any(cwd == r or cwd.startswith(r + "/") for r in roots)


def claude_sessions(home: Path, roots: list[str]) -> list[dict]:
    out = []
    for path in sorted(home.glob(".claude/projects/*/*.jsonl")):
        start, cwd, used = None, None, Counter()
        for r in records(path, '"type":"assistant"'):
            if r.get("type") != "assistant" or r.get("isSidechain"):
                continue
            model = (r.get("message") or {}).get("model")
            if not model or model.startswith("<"):
                continue
            start = start or ts(r.get("timestamp", ""))
            cwd = cwd or r.get("cwd", "")
            used[(model, r.get("effort") or "?")] += 1
        if used and start and ours(cwd or "", roots):
            out.append({"cli": "claude", "start": start, "cwd": cwd, "used": used, "file": str(path)})
    return out


def codex_sessions(home: Path, roots: list[str]) -> list[dict]:
    out = []
    for path in sorted(home.glob(".codex/sessions/**/rollout-*.jsonl")):
        start, cwd, used = None, None, Counter()
        for r in records(path, '"type":"'):
            p = r.get("payload") or {}
            if r.get("type") == "session_meta":
                start = ts(p.get("timestamp") or r.get("timestamp", ""))
                cwd = p.get("cwd", "")
            elif r.get("type") == "turn_context":
                start = start or ts(r.get("timestamp", ""))
                cwd = cwd or p.get("cwd", "")
                used[(p.get("model") or "?", p.get("effort") or "?")] += 1
        if used and start and ours(cwd or "", roots):
            out.append({"cli": "codex", "start": start, "cwd": cwd, "used": used, "file": str(path)})
    return out


def task_starts(events: Path) -> list[tuple[datetime, str, dict]]:
    out = []
    for r in records(events, '"event":"task_'):
        if r.get("event") in STARTS and (t := ts(r.get("ts", ""))):
            out.append((t, r.get("task_id", "?"), r))
    return sorted(out, key=lambda e: e[0])


def fmt(used: Counter) -> str:
    total = sum(used.values())
    return ", ".join(f"{m}·{e} {round(100 * n / total)}%" for (m, e), n in used.most_common())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--since", help="only sessions that started on or after this date (YYYY-MM-DD)")
    ap.add_argument("--sessions", action="store_true", help="also list every session with its file")
    args = ap.parse_args()

    cfg = load_config(Path(args.config).expanduser())
    home = Path.home()
    # The worker runs in the workspace; the engine's own Codex calls (preflight, peer, report) in its state dir.
    roots = [str(cfg.paths.workspace), str(cfg.paths.bulava_state)]
    sessions = sorted(claude_sessions(home, roots) + codex_sessions(home, roots), key=lambda s: s["start"])
    if args.since:
        since = datetime.fromisoformat(args.since).date()
        sessions = [s for s in sessions if s["start"].date() >= since]
    if not sessions:
        print(f"no Claude or Codex sessions under {', '.join(roots)}")
        return 1

    starts = task_starts(cfg.paths.events)
    times = [t for t, _, _ in starts]
    per_task: dict[str, dict] = defaultdict(lambda: {"claude": Counter(), "codex": Counter(), "n": 0, "asked": None})
    for s in sessions:
        i = bisect_right(times, s["start"]) - 1
        task = starts[i][1] if i >= 0 else "(before the first task in events.jsonl)"
        row = per_task[task]
        row[s["cli"]].update(s["used"])
        row["n"] += 1
        if i >= 0 and starts[i][2].get("claude_model"):
            e = starts[i][2]
            row["asked"] = (f"Claude {e['claude_model']}·{e['claude_effort']}, "
                            f"Codex {e['codex_model']}·{e['codex_effort']}")
        s["task"] = task

    print("Share of answers per model·depth; ? = the CLI did not record it.\n")
    for task, row in per_task.items():
        print(f"{task}  ({row['n']} sessions)")
        if row["asked"]:
            print(f"  asked:  {row['asked']}")
        print(f"  Claude: {fmt(row['claude']) or '-'}")
        print(f"  Codex:  {fmt(row['codex']) or '-'}")
    if args.sessions:
        print()
        for s in sessions:
            print(f"{s['start']:%Y-%m-%d %H:%M}  {s['task']:<12} {s['cli']:<6} {fmt(s['used'])}  {s['file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
