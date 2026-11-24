"""The tickets' work in the console: every task the runner took or has queued (/tasks/), and one task's page
(/tasks/<KEY>/): where it is, why it waits, the question it waits on, what it has used of its limits, its merge
requests with the commit Codex passed, and the code reviews asked for on it.

Development, research and code review of people's merge requests are kinds of task, so they share this page; a test
run of the platform, an architecture review and a triage run have theirs under /qa/, /architecture/ and /secops/
(runs.status_page).

The page reads runner.db read-only and the runner's metrics (execution.py). Reloading it repeats nothing: every action
on a task (an answer, `/continue`, a security decision) stays on the ticket."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from arceus_runner.config import Config
from arceus_runner.console import execution as ex
from arceus_runner.console import files, links, status
from arceus_runner.console.store import ConsoleError
from arceus_runner.console.web import Ctx, Gone, page
from arceus_runner.db import REVIEW_UNDER_WAY, ReviewStore

SECTIONS = ("/tasks/",)
LIST_LIMIT = 200
RUNNING_REVIEW = ("preparing", "running", "restoring")


def _cfg(ctx: Ctx) -> Config:
    cfg, problems = ctx.config()
    if cfg is None:
        raise ConsoleError("config.yaml has errors, so the runner's database cannot be found: " + "; ".join(problems[:3]))
    return cfg


def _store(cfg: Config) -> ReviewStore:
    try:
        return ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        raise ConsoleError(str(e)) from None


def _key(ctx: Ctx) -> str:
    key = ctx.param("key")
    if not ex.KEY.match(key):
        raise Gone()
    return key


def _limits(cfg: Config, task, now: float, config_note: str) -> dict:
    """The task's spend against config.yaml's `limits`. Budgets of rounds count per task; `/continue` sets them back
    to zero, and the CI repairs start again once a pipeline passes."""
    lim = cfg.limits
    rows = []
    if task.kind == "code":
        rows += [
            {"name": "Review rounds that changed code", "used": task.review_rounds, "max": lim.max_review_rounds,
             "note": "max_review_rounds; a /continue starts the count again"},
            {"name": "Review rounds answered without a change", "used": task.review_answers,
             "max": lim.max_review_answers, "note": "max_review_answers"},
            {"name": "CI repairs in a row", "used": task.ci_attempts, "max": lim.max_ci_fix_attempts,
             "note": "max_ci_fix_attempts; starts again once a pipeline passes"},
        ]
    for r in rows:
        r["pct"] = min(100, round(100 * r["used"] / r["max"])) if r["max"] else 0
        r["level"] = "bad" if r["used"] >= r["max"] else "warn" if r["used"] >= r["max"] - 1 else "ok"
    answer = None
    if task.no_change_asked:
        answer = {"no_deadline": True}
    elif task.state == "needs_input":
        fail_after = lim.needs_input_fail_after.total_seconds()
        if task.question_at_ms:
            asked = task.question_at_ms / 1000
            left = asked + fail_after - now
            answer = {"known": True, "asked": asked, "deadline": asked + fail_after, "left": left,
                      "waited": now - asked, "of": ex.duration(fail_after),
                      "level": "bad" if left < 86400 else "warn" if left < fail_after / 2 else "ok"}
        else:
            answer = {"known": False, "of": ex.duration(fail_after)}
    return {"rows": rows, "answer": answer, "config_note": config_note}


def _config_note(cfg_path, store: ReviewStore) -> str:
    """Whether the limits shown are the ones the runner counts with: it reads config.yaml when it starts."""
    found = store.state("config")
    value = found[0] if found else None
    if not isinstance(value, dict) or not value.get("sha256"):
        return "The runner has not recorded which config.yaml it started with: these are the file's limits now."
    current = files.read(cfg_path)
    if current.problem or current.sha != value["sha256"]:
        return ("config.yaml has changed since the runner started. It counts with the limits it started with until it "
                "is restarted; these are the file's.")
    return ""


def _mrs(store: ReviewStore, task) -> list[dict]:
    rows = [{"repo": m.repo, "url": m.url, "state": m.state or "opened", "sha": m.published_sha,
             "iid": m.iid} for m in store.task_mrs(task.task_id)]
    if not rows:  # a task from before the MRs were kept per repository, or one whose records were dropped
        try:
            urls = json.loads(task.mr_urls or "[]")
        except ValueError:
            urls = []
        urls = [u for u in urls if isinstance(u, str)] if isinstance(urls, list) else []
        rows = [{"repo": "", "url": u, "state": "", "sha": "", "iid": ""} for u in urls]
    return rows


def _hold(store: ReviewStore, task_id: str) -> dict | None:
    """The open security hold, as the ticket's comment tells it: what kind of text, its code and the categories found.
    Never the text, never the screen's reason (docs/security.md)."""
    row = store.open_hold_of(task_id)
    if row is None:
        return None
    screening = store.screening(row["digest"])
    try:
        categories = json.loads(screening["categories"]) if screening else []
    except ValueError:
        categories = []
    categories = [c for c in categories if isinstance(c, str)] if isinstance(categories, list) else []
    return {"kind": row["kind"], "code": row["digest"][:12], "held_at": row["held_at"], "categories": categories,
            "verdict": screening["verdict"] if screening else ""}


def _continue_until(cfg: Config, task) -> str:
    since = task.published_at_ms if task.state == "done" and task.research else \
        task.failed_at_ms if task.state in ("failed", "stopped") else None
    if not since or not task.branch:
        return ""
    until = since / 1000 + cfg.limits.continue_window.total_seconds()
    if until <= time.time():
        return ""
    return f"{datetime.fromtimestamp(until, cfg.schedule.tz):%d %b %Y, %H:%M}"


def _ahead(store: ReviewStore, cfg: Config) -> str:
    """What the runner starts before a queued ticket (delivery.maybe_start): people's comments for an open MR, a
    `/continue`, a code review or a test run or architecture review waiting. Not a test run that will work in a folder
    of its own (qa.workspace): it starts beside the ticket."""
    if store.has_pending_review():
        return "Review comments for an open merge request"
    if store.pending_continues():
        return "A /continue on another ticket"
    review = next(iter(store.reviews("pending")), None)
    if review:
        return f"Code review {review.n} of {review.task_id}"
    run = next((r for r in store.runs(None, "pending") if not (r.kind == "qa" and cfg.qa.workspace is not None)),
               None)
    if run:
        return f"{status.RUN_WORDS[run.kind][0]}, run {run.n}"
    return ""


def gather(cfg: Config, key: str, config_path, now: float | None = None) -> dict | None:
    """Everything one task's page shows, or None when the runner has neither taken nor queued this ticket."""
    now = now or time.time()
    store = _store(cfg)
    try:
        task = store.task(key)
        queued = any(d.issue_key == key for d in store.accepted_untaken())
        if task is None and not queued:
            return None
        m = status.read_metrics(cfg)
        runner = status.runner_status(m, now)
        stale_after = ex.observed_stale_after(cfg)
        holder = ex.holder_words(store)
        drain = ex.drain_by(store)
        mode = m.mode or cfg.mode
        used_up = ex.used_up(m, cfg.schedule.tz)
        reviews = store.reviews(task_id=key)
        under_way = next((r for r in reviews if r.state in REVIEW_UNDER_WAY), None)

        # A ticket whose merge requests the runner only reviewed is queued for the agent once tagged: it is not taken.
        # The runner takes it over only once no review of it runs or waits and nothing holds it (delivery.start);
        # until then it is where its review has it.
        taken = task is not None and not (queued and task.kind == "review" and task.state != "security_hold"
                                          and under_way is None)
        state = task.state if taken else "queued"
        running = state == "dispatched"
        view = ex.live(m, key, now, stale_after) if running else None
        review_view = None
        if taken and state == "code_review" and under_way and under_way.state in RUNNING_REVIEW:
            review_view = ex.live(m, under_way.id, now, stale_after)
        q = ex.question(task) if taken else None
        facts = ex.Facts(
            state=state, kind=task.kind if task else "code", blocked=bool(q and q["blocked"]),
            no_change=bool(q and q["no_change"]),
            turn=(view or review_view or {}).get("turn", ""),
            used_up=used_up if (running or (review_view and under_way.state == "running")) else [],
            limit_paused=running and ex.limit_paused(task),
            holder="" if holder == key else holder, can_take=m.can_take, mode=mode, drain_by=drain,
            review=(under_way.n, under_way.state) if under_way and state == "code_review" else None,
            fixes_waiting=taken and state == "code_review" and ex.fixes_waiting(store, key),
            ahead=_ahead(store, cfg) if state == "queued" else "",
            reason=(task.reason or "") if taken else "",
            continue_until=_continue_until(cfg, task) if taken else "")
        why = ex.reason(facts)
        issue = links.issue_url(cfg)(key)

        out = {
            "key": key, "issue_url": issue, "taken": taken, "state": state, "why": why, "runner": runner,
            "summary": task.summary if task else "", "kind": task.kind if task else "",
            "kind_words": ex.KIND_WORDS.get(task.kind, task.kind) if task else "",
            "state_words": status.STATE_WORDS.get(state, state) if taken else "Queued",
            "author": task.tag_author if task else "",
            "updated_at": task.updated_at if task else "",
            "live": view, "fresh": ex.freshness(runner, running, view, now),
            "question": q, "hold": _hold(store, key) if taken and state == "security_hold" else None,
            "limits": _limits(cfg, task, now, _config_note(config_path, store))
            if taken else None,
            "branch": task.branch if task and task.kind != "review" else "",
            "base_branch": task.base_branch if task else "",
            "mrs": _mrs(store, task) if taken else [], "research": bool(task and task.research),
            "reviews": [], "now": now,
        }
        for r in reversed(reviews):
            rv = ex.live(m, r.id, now, stale_after) if r.state in RUNNING_REVIEW else None
            out["reviews"].append({
                "n": r.n, "id": r.id, "state": r.state, "words": ex.REVIEW_STATE_WORDS.get(r.state, r.state),
                "outcome": r.outcome or "", "author": r.author, "reason": r.reason or "",
                "asked_at": datetime.fromtimestamp(r.asked_at_ms / 1000, UTC).isoformat() if r.asked_at_ms else "",
                "started_at": r.started_at or "", "updated_at": r.updated_at, "live": rv,
                "fresh": ex.freshness(runner, r.state in RUNNING_REVIEW, rv, now)})
        return out
    finally:
        store.close()


def _gather(ctx: Ctx) -> dict:
    key = _key(ctx)
    t = gather(_cfg(ctx), key, ctx.settings.config_path)
    if t is None:
        raise Gone()
    return t


def task(ctx: Ctx) -> Response:
    t = _gather(ctx)
    return ctx.render("task.html", title=t["key"] + (f": {t['summary']}" if t["summary"] else ""), t=t,
                      ago=status.ago, duration=ex.duration)


def task_live(ctx: Ctx) -> Response:
    """Every part of the task's page below its title, as it is now: the page puts in those that changed."""
    t = _gather(ctx)
    html = ctx.app.templates.get_template("task_parts.html").render(ctx=ctx, user=ctx.user, t=t, ago=status.ago,
                                                                   duration=ex.duration)
    return HTMLResponse(html)


def index(ctx: Ctx) -> Response:
    cfg = _cfg(ctx)
    store = _store(cfg)
    try:
        issue = links.issue_url(cfg)
        queue = [d.issue_key for d in store.accepted_untaken()]
        rows = sorted(store.tasks(), key=lambda t: t.updated_at or "", reverse=True)
        total = len(rows)
        tasks = [{"key": t.task_id, "summary": t.summary, "state": t.state,
                  "words": status.STATE_WORDS.get(t.state, t.state), "kind": ex.KIND_WORDS.get(t.kind, t.kind),
                  "kind_code": t.kind, "updated_at": t.updated_at, "url": issue(t.task_id),
                  "queued": t.task_id in queue} for t in rows[:LIST_LIMIT]]
    finally:
        store.close()
    known = {t["key"] for t in tasks}
    return ctx.render("tasks.html", title="Tasks", tasks=tasks, total=total, limit=LIST_LIMIT,
                      queue=[{"key": k, "url": issue(k), "known": k in known} for k in queue])


def routes() -> list[Route]:
    return [Route("/tasks/", page(index)),
            Route("/tasks/{key}/", page(task)),
            Route("/tasks/{key}/live", page(task_live, api=True))]
