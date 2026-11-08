"""What the home page shows: the factory now, from what the runner leaves behind.

Two sources, each of which can be missing, and each missing one is said, never shown as fine:
- runner.db, read-only: the tickets in work, waiting on people, the queue, the background runs, the findings, and
  the health checks with their detail as the runner last wrote them (runner_state `health`);
- the runner's metrics on localhost (metrics.py): whether it answers, when it last finished a pass, the checks'
  status now, subscription usage. Until its first pass ends, a runner that answers knows none of these yet.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from prometheus_client.parser import text_string_to_metric_families

from arceus_runner.config import Config
from arceus_runner.db import ACTIVE_STATES, RUN_HOLDS, RUN_UNDER_WAY, ReviewStore

# A pass ends at least every poll interval; this long without one means the loop is stuck in a call.
STUCK_AFTER = 15 * 60
RUN_WORDS = {"qa": ("Platform test", "Testing the platform"), "arch": ("Architecture review", "Reviewing the architecture")}
STATE_WORDS = {"claimed": "Starting", "dispatched": "Agent working", "publishing": "Opening merge requests",
               "needs_input": "Question for people", "security_hold": "Held by the security screen",
               "code_review": "In code review", "failed": "Failed", "done": "Done", "stopped": "Stopped"}


@dataclass
class Metrics:
    answered: bool = False
    error: str = ""
    last_tick: float | None = None
    mode: str = ""
    version: str = ""
    can_take: bool | None = None
    health: dict[str, int] = field(default_factory=dict)        # check -> 0 ok, 1 warn, 2 fail
    usage: dict[tuple[str, str], float] = field(default_factory=dict)   # (provider, window) -> percent
    resets: dict[tuple[str, str], float] = field(default_factory=dict)  # (provider, window) -> epoch


def read_metrics(cfg: Config, timeout: float = 2.0) -> Metrics:
    m = Metrics()
    if not cfg.metrics.enabled:
        m.error = "metrics are off in config.yaml (metrics.enabled)"
        return m
    host = cfg.metrics.address if cfg.metrics.address not in ("0.0.0.0", "::") else "127.0.0.1"
    try:
        r = httpx.get(f"http://{host}:{cfg.metrics.port}/metrics", timeout=timeout)
        r.raise_for_status()
    except httpx.HTTPError as e:
        m.error = f"nothing answers on {host}:{cfg.metrics.port} ({type(e).__name__})"
        return m
    m.answered = True
    for family in text_string_to_metric_families(r.text):
        for s in family.samples:
            name, labels, value = s.name, s.labels, s.value
            if name == "runner_last_tick_seconds":
                m.last_tick = value or None
            elif name == "runner_info" and value:
                m.mode, m.version = labels.get("mode", ""), labels.get("version", "")
            elif name == "runner_can_take":
                m.can_take = bool(value)
            elif name == "runner_health":
                m.health[labels.get("check", "?")] = int(value)
            elif name == "runner_usage_percent":
                m.usage[(labels.get("provider", ""), labels.get("window", ""))] = value
            elif name == "runner_usage_resets_at_seconds":
                m.resets[(labels.get("provider", ""), labels.get("window", ""))] = value
    return m



def _task(t, issue_url) -> dict:
    return {"key": t.task_id, "summary": t.summary, "state": t.state, "words": STATE_WORDS.get(t.state, t.state),
            "url": issue_url(t.task_id) if issue_url else "", "since": t.updated_at, "author": t.tag_author,
            "question": getattr(t, "question", None) or "", "reason": t.reason or ""}


def read_store(cfg: Config, issue_url=None) -> dict:
    """Everything the page shows from runner.db, or why it could not be read."""
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except FileNotFoundError:
        return {"error": f"no runner database at {cfg.paths.db} yet: the runner has not run with this config"}
    except RuntimeError as e:
        return {"error": str(e)}
    try:
        active = store.tasks(*ACTIVE_STATES)
        waiting = store.tasks("needs_input", "security_hold")
        review = store.tasks("code_review")
        day_ago = datetime.fromtimestamp(time.time() - 86400, UTC).isoformat()
        failed = [t for t in store.tasks("failed") if (t.updated_at or "") >= day_ago[:19]]
        queue = [d.issue_key for d in store.accepted_untaken()]
        under_way = store.runs(None, *RUN_UNDER_WAY)
        latest = {}
        for kind in ("qa", "arch"):
            done = store.runs(kind, "done", limit=1)
            if done:
                r = done[-1]
                result = json.loads(r.result or "{}")
                latest[kind] = {"n": r.n, "outcome": r.outcome, "finished_at": r.finished_at,
                                "findings": len(result.get("findings") or []), "summary": result.get("summary") or ""}
        findings = store.findings("open")
        health = store.state("health")
        return {
            "health": health[0] if health else {},
            "active": [_task(t, issue_url) for t in active],
            "waiting": [_task(t, issue_url) for t in waiting],
            "review": [_task(t, issue_url) for t in review],
            "failed": [_task(t, issue_url) for t in failed],
            "queue": queue,
            "runs": [{"kind": r.kind, "n": r.n, "state": r.state, "holds": r.state in RUN_HOLDS,
                      "label": RUN_WORDS[r.kind][0], "doing": RUN_WORDS[r.kind][1], "since": r.started_at or r.created_at,
                      "stop": bool(r.stop_requested), "by": r.requested_by} for r in under_way],
            "latest": latest,
            "findings_open": sum(1 for f in findings if not f.decision),
            "findings_decided": sum(1 for f in findings if f.decision),
        }
    finally:
        store.close()


def ago(epoch: float | None, now: float | None = None) -> str:
    if not epoch:
        return ""
    s = max(0, int((now or time.time()) - epoch))
    if s < 60:
        return f"{s} s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60:02d} min ago"
    return f"{s // 86400} d ago"


def snapshot(cfg: Config, issue_url=None, now: float | None = None) -> dict:
    """The home page's facts and its one-line headline: (tone, words). Tones: good, busy, wait, bad, unknown."""
    now = now or time.time()
    m = read_metrics(cfg)
    db = read_store(cfg, issue_url)
    stuck = bool(m.answered and m.last_tick and now - m.last_tick > STUCK_AFTER)
    starting = m.answered and not m.last_tick
    recorded = db.get("health") or {}
    failing = [{"check": k, "status": "fail" if v == 2 else "warn", "detail": (recorded.get(k) or {}).get("detail", ""),
                "since": (recorded.get(k) or {}).get("since")} for k, v in sorted(m.health.items()) if v]
    failing.sort(key=lambda c: c["status"] != "fail")
    if not m.answered:
        runner = {"tone": "bad", "words": "Not answering", "detail": m.error}
    elif starting:
        runner = {"tone": "unknown", "words": "Starting", "detail": "its first pass has not finished yet"}
    elif stuck:
        runner = {"tone": "bad", "words": "Stuck", "detail": f"its last pass ended {ago(m.last_tick, now)}"}
    else:
        runner = {"tone": "good", "words": "Running", "detail": f"last pass {ago(m.last_tick, now)}"}
    mode = m.mode or cfg.mode

    tone, words = "good", "Idle: nothing is queued"
    if db.get("error"):
        tone, words = "unknown", "The runner's state cannot be read"
    elif db["active"]:
        t = db["active"][0]
        tone, words = "busy", f"Working on {t['key']}: {t['summary']}" if t["summary"] else f"Working on {t['key']}"
    elif any(r["holds"] for r in db["runs"]):
        r = next(r for r in db["runs"] if r["holds"])
        tone, words = "busy", f"{r['doing']}, run {r['n']}"
    elif db["queue"] and m.can_take is False:
        tone, words = "wait", f"{len(db['queue'])} queued, but new work is held while a health check fails"
    elif db["queue"]:
        tone, words = "busy", f"{len(db['queue'])} queued: the next one starts on the runner's next pass"
    elif db["waiting"]:
        n = len(db["waiting"])
        tone, words = "wait", f"Waiting on people: {n} ticket{'s' if n != 1 else ''}"
    if mode == "observe" and not db.get("error"):
        tone, words = "wait", "Observing: the runner takes no work (mode: observe)"
    if runner["tone"] == "bad":
        tone, words = "bad", "The runner is not answering" if not m.answered else "The runner is stuck"
    elif starting:
        tone, words = "unknown", "The runner is starting: its health and usage are not known until its first pass ends"

    usage = []
    for provider, title in (("claude", "Claude"), ("codex", "Codex")):
        for window, wtitle in (("5h", "5 hours"), ("7d", "7 days")):
            pct = m.usage.get((provider, window))
            resets = m.resets.get((provider, window))
            usage.append({"name": f"{title}, {wtitle}", "pct": pct,
                          "level": None if pct is None else "bad" if pct >= 90 else "warn" if pct >= 70 else "ok",
                          "resets": datetime.fromtimestamp(resets, cfg.schedule.tz).strftime("%a %H:%M")
                          if resets else ""})
    # Health and usage are known once the runner answers and has finished a pass; before that they are not.
    known = m.answered and not starting
    return {"tone": tone, "words": words, "runner": runner, "mode": mode, "version": m.version, "known": known,
            "metrics": m, "failing": failing, "db": db, "usage": usage, "can_take": m.can_take}
