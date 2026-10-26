"""A stand-in for the Bulava engine's command line, following engine/SERVER.md.

Installed as bin/dispatch.sh, bin/worker-send.sh and bin/night-shift.sh. Keeps its
state in $SUPERVISOR_STATE_DIR/fake-engine.json and logs every call with the
environment it saw, so tests can check what the runner handed to the engine.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

STATE = Path(os.environ["SUPERVISOR_STATE_DIR"]) / "fake-engine.json"


def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"tasks": {}, "calls": [], "next_rc": {}}


def save(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))


def now() -> str:
    return datetime.now(UTC).isoformat()


def flags(argv: list[str]) -> tuple[dict, list[str]]:
    out, rest, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            out[a[2:]] = argv[i + 1]
            i += 2
        else:
            rest.append(a)
            i += 1
    return out, rest


def emit(tier: str, data: dict, rc: int = 0) -> int:
    print(f"TIER={tier}")
    print(json.dumps(data))
    return rc


def main() -> int:
    cmd, argv = sys.argv[1], sys.argv[2:]
    s = load()
    s["calls"].append({"cmd": cmd, "argv": argv, "env": {k: v for k, v in os.environ.items()
                                                       if k.startswith(("SUPERVISOR_", "CLAUDE_", "NIGHT_SHIFT", "GIT", "YOUTRACK",
                                                                        "SLACK"))}})
    sub = f"{cmd} {argv[0]}" if argv else cmd
    forced = s["next_rc"].pop(sub, None)
    if forced is None:
        forced = s["next_rc"].pop(cmd, None)
    if forced is not None:
        save(s)
        print("❌ forced by the test", file=sys.stderr)
        return forced

    if cmd == "dispatch":
        f, (repo, text) = flags(argv)
        tid = f["task-id"]
        t = s["tasks"].get(tid)
        if t:
            if f.get("message-id") in t["messages"]:
                save(s)
                return emit("duplicate", {"task_id": tid, "attempt": 1})
            save(s)
            print(f"❌ задача {tid} уже є", file=sys.stderr)
            return 2
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-B", f["branch"], f["base"]], check=True)
        base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        # Like the engine: every repository of the task with its own base, the project first.
        repos = [{"path": repo, "base_sha": base}]
        extra_file = os.environ.get("SUPERVISOR_EXTRA_DIRS_FILE")
        if extra_file and os.path.exists(extra_file):
            for line in open(extra_file).read().splitlines():
                if line.strip():
                    repos.append({"path": line, "base_sha": subprocess.run(
                        ["git", "-C", line, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()})
        s["tasks"][tid] = {
            "schema": 1, "task_id": tid, "state": "running", "state_reason": "", "attempt_live": True,
            "project": repo, "branch": f["branch"], "base_ref": f["base"], "base_sha": base, "objective": text,
            "author": f.get("author"), "repos": repos, "question": None, "receipt": None, "outcome": None,
            "attempts": [{"n": 1, "kind": "initial", "started_at": now(), "ended_at": None}],
            "messages": {f.get("message-id"): text},
        }
        save(s)
        return emit("dispatched", {"task_id": tid, "dispatch_id": "d-1", "attempt": 1, "message_id": f.get("message-id")})

    if cmd == "worker-send":
        f, rest = flags(argv)
        tid = f["task-id"]
        text = rest[-1]
        t = s["tasks"].get(tid)
        if not t:
            save(s)
            return 2
        mid = f.get("message-id")
        if mid in t["messages"]:
            save(s)
            return emit("duplicate", {"task_id": tid}) if t["messages"][mid] == text else 4
        if t["state"] == "running" and t["attempt_live"]:
            save(s)
            print("TIER=busy")
            return 6
        if "answers" in f and (not t["question"] or t["question"].get("id") != f["answers"]):
            save(s)
            print("TIER=conflict")
            return 4
        t["messages"][mid] = text
        t["attempts"].append({"n": len(t["attempts"]) + 1, "kind": "continue", "started_at": now(), "ended_at": None})
        t.update(state="running", attempt_live=True, question=None)
        save(s)
        return emit("dispatched", {"task_id": tid, "attempt": len(t["attempts"])})

    if cmd in ("claude-usage", "codex-usage"):
        save(s)
        return 0

    if cmd == "night-shift":
        if argv[:2] == ["task", "show"]:
            t = s["tasks"].get(argv[3])
            save(s)
            if not t:
                print(f"❌ задачі {argv[3]} немає", file=sys.stderr)
                return 1
            print(json.dumps({k: v for k, v in t.items() if k != "messages"}))
            return 0
        if argv[:1] == ["doctor"]:
            checks = s.get("doctor") or [{"name": "auth.claude", "status": "ok", "detail": "signed in"}]
            save(s)
            print(json.dumps({"ok": all(c["status"] != "fail" for c in checks), "schema": 1, "checks": checks}))
            return 1 if any(c["status"] == "fail" for c in checks) else 0
        save(s)
        return 0  # trust, mcp-decide, stop
    return 2


if __name__ == "__main__":
    sys.exit(main())
