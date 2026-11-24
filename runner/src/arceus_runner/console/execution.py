"""One piece of agent work as the console shows it: a ticket's task (code, research, people's merge requests the agent
only reviews), a code review asked for on a ticket, and a background agent's run (a test run of the platform, an
architecture review).

Every page of one shares a header: the state the runner's database has it in, the engine's step and since when, when
the agent last wrote anything and how long it has been quiet, and whether those live figures can be trusted now.
Three things go stale on their own, so they are kept apart:
- the runner's heartbeat (its last finished pass, status.runner_status): every live figure is as old as that;
- the runner's last read of this work's engine files (runner_engine_observed_seconds);
- the agent's quiet time, which means something only while the step is the agent's own.

Why the work waits is one of a fixed set of reasons (`reason()`), each with its own words: an answer on the ticket is
asked for only when an answer is what it waits for, and a used-up subscription is said to be everyone's.

Everything here reads: runner.db through a read-only connection, and the runner's metrics. Nothing a page shows starts,
repeats or stops anything; `/continue` and every answer stay on the ticket.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from arceus_runner.config import Config
from arceus_runner.console import status
from arceus_runner.console.status import Metrics
from arceus_runner.db import ACTIVE_STATES, ReviewStore
from arceus_runner.progress import NOT_AGENT_STAGES, WAITING_STATES

# A ticket's key as it appears in an address: letters, digits and `-_.`, never a path. Anything else is not a task.
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
KIND_WORDS = {"code": "Development", "research": "Research", "review": "Code review of people's merge requests"}
REVIEW_STATE_WORDS = {"pending": "Queued", "preparing": "Preparing the workspace", "running": "Reviewing",
                      "restoring": "Putting the workspace back", "publishing": "Posting the result", "done": "Done",
                      "refused": "Refused"}
RUN_STATE_WORDS = {"pending": "Queued", "preparing": "Preparing the workspace", "running": "Agent working",
                   "reviewing": "Codex checks the findings", "restoring": "Putting the workspace back",
                   "publishing": "Writing the report", "done": "Finished"}
WINDOW_WORDS = {"5h": "5-hour", "7d": "7-day"}
PROVIDER_WORDS = {"claude": "Claude", "codex": "Codex"}
# The question as the runner kept it before it kept it whole (texts.short_text): cut at 1500 with an ellipsis.
OLD_QUESTION_CUT = 1500


def observed_stale_after(cfg: Config) -> float:
    """How long the runner may go without reading a running task's engine files before the page says so: three of its
    progress polls, and never under five minutes (a pass can take that long)."""
    return max(300.0, 3 * cfg.schedule.poll_progress.total_seconds())


def live(m: Metrics, exec_id: str, now: float, stale_after: float) -> dict:
    """What the runner's metrics say about one engine task now: its step, the agent's activity and quiet time, and
    when the runner last read its files. `turn` is whose the step is: agent, checks (the gate, the verifier or Codex)
    or paused (the engine waits)."""
    out: dict = {"step": None, "turn": "", "active_at": m.active.get(exec_id), "quiet": m.quiet.get(exec_id),
                 "observed_at": m.observed.get(exec_id)}
    step = m.steps.get(exec_id)
    if step:
        stage, state, since = step
        out["step"] = {"stage": stage, "state": state, "since": since or None}
        out["turn"] = "paused" if state in WAITING_STATES else "checks" if stage.startswith(NOT_AGENT_STAGES) \
            else "agent"
    observed = out["observed_at"]
    out["observed_stale"] = observed is None or now - observed > stale_after
    return out


def used_up(m: Metrics, tz) -> list[str]:
    """The subscription windows the runner reads as used up, each with when it resets: the runner pauses every
    attempt while any is (delivery._limits_exhausted). They are the whole factory's, never one task's."""
    out = []
    for (provider, window), pct in sorted(m.usage.items()):
        if pct >= 100:
            resets = m.resets.get((provider, window))
            at = f", resets {datetime.fromtimestamp(resets, tz):%a %H:%M}" if resets else ""
            out.append(f"{PROVIDER_WORDS.get(provider, provider)}'s {WINDOW_WORDS.get(window, window)} window{at}")
    return out


@dataclass
class Facts:
    """What decides a reason. `state` is the task's state, a code review's (`review:<state>`) or a run's
    (`run:<state>`), or `queued` for a ticket accepted and not taken yet."""
    state: str
    kind: str = "code"
    blocked: bool = False            # the question says the agent cannot go on at all without an answer
    no_change: bool = False          # it waits on the agent's conclusion that nothing needs changing
    turn: str = ""                   # live()["turn"]
    used_up: list[str] = field(default_factory=list)
    limit_paused: bool = False       # the runner holds the attempt's clock: the engine records it waiting for a window
    holder: str = ""                 # what has the shared workspace now, in words; "" when it is free
    can_take: bool | None = None     # health lets new work in (None: not known)
    mode: str = "live"
    drain_by: str = ""               # someone asked from the console to restart the engine
    review: tuple[int, str] | None = None  # a code review of this ticket under way: (n, its state)
    fixes_waiting: bool = False      # people's comments on the MRs not sent to the agent yet
    ahead: str = ""                  # work the runner starts before a queued ticket, in words
    reason: str = ""                 # the runner's own reason (a failure, a stop)
    stop_asked: bool = False         # a run someone asked to stop
    continue_until: str = ""         # a failed task a `/continue` on the ticket still resumes until then


def reason(f: Facts) -> dict:
    """Why the work is where it is: {code, tone, title, detail}. Tones as on Home: good, busy, wait, bad, quiet."""
    def r(code, tone, title, detail=""):
        return {"code": code, "tone": tone, "title": title, "detail": detail}

    state = f.state
    if state in ("queued", "run:pending", "review:pending"):
        what = "It" if state == "queued" else "The run" if state.startswith("run:") else "The review"
        # The runner's own gates, in its order (delivery.maybe_start, code_review, workspace_run.start).
        if f.mode != "live":
            return r("observe", "wait", "Queued, but the runner takes no work",
                     f"The runner is in mode: {f.mode}. {what} starts once it is live.")
        if f.drain_by:
            return r("drain", "wait", "Queued: new work is held for an engine restart",
                     f"{f.drain_by} asked from the console to restart the engine once its tasks end. {what} starts "
                     "after that.")
        if f.can_take is False:
            return r("health", "wait", "Queued, held by a failing health check",
                     "The runner starts no new work while a check fails or a service does not answer. Home says "
                     "which one and why.")
        if f.holder:
            return r("workspace", "wait", "Queued for the workspace",
                     f"{f.holder} has the shared workspace. {what} starts once it is free.")
        if f.ahead:
            return r("behind", "wait", "Queued behind other work",
                     f"{f.ahead} goes first. {what} starts after it.")
        return r("next_pass", "busy", "Queued", f"{what} starts on the runner's next pass.")

    if state in ("dispatched", "review:running", "run:running"):
        if f.limit_paused and state == "dispatched":
            # The same signal the attempt's clock stops on (delivery._check_timeout, the task's attempt_pause): the
            # engine records this attempt waiting for a window. A used-up limit alone does not make it wait.
            return r("subscription", "wait", "Paused: a subscription limit is used up",
                     f"Used up: {'; '.join(f.used_up) or 'a subscription window'}. The limit is shared by all the "
                     "factory's work, not spent by this one alone. It goes on by itself when the window resets, and "
                     "the attempt's clock stands still meanwhile. Nothing to answer.")
        if f.used_up and f.turn == "paused" and state != "dispatched":
            # A code review or a run has no such pause of its own: only the engine's step says it waits. A development
            # attempt the engine has not recorded as waiting for a window (it waits on background agents, say) is
            # paused by the engine, not for a subscription, and its clock runs: the next reason says so.
            return r("subscription", "wait", "Paused: a subscription limit is used up",
                     f"The engine waits, and {'; '.join(f.used_up)} is used up: shared by all the factory's work, "
                     "not spent by this one alone. It goes on by itself; its time limit keeps running. Nothing to "
                     "answer.")
        if f.turn == "paused":
            return r("engine_wait", "wait", "Paused by the engine",
                     "The engine holds this step and goes on by itself. Nothing to answer.")
        if f.turn == "checks":
            return r("checks", "busy", "Checks running",
                     "The gate, the verifier or Codex has the turn; the agent is expected to be quiet meanwhile.")
        words = {"dispatched": "Agent working", "review:running": "The review agent is reading the code",
                 "run:running": "Agent working"}[state]
        return r("working", "busy", words)

    if state == "claimed":
        return r("starting", "busy", "Taken: starting", "The runner prepares the branch and sends the brief to the "
                                                          "engine.")
    if state == "publishing":
        return r("publishing", "busy", "Opening merge requests",
                 "Codex passed the work; the runner pushes exactly that commit and opens the merge requests.")
    if state == "needs_input" and f.no_change:
        return r("person", "wait", "Nothing to change: waiting for your decision",
                 "The agent concluded that nothing needs changing, and no review checks that. Closing the ticket "
                 "accepts it and ends the task without a failure; a comment saying what still needs doing sends the "
                 "agent back to work. It has no deadline.")
    if state == "needs_input":
        title = "Waiting for a decision on the ticket" if f.blocked else "Waiting for an answer on the ticket"
        return r("person", "wait", title, "The agent asked the question below. A comment on the ticket from someone "
                                          "allowed to tag work is the answer; the runner sends it on its next pass.")
    if state == "security_hold":
        return r("security", "wait", "Held by the security screen",
                 "A text for the agent was held. A security reviewer answers on the ticket with /security-allow or "
                 "/security-reject and the code the ticket's comment gives. The held text is not shown here.")
    if state == "code_review":
        if f.review:
            n, rstate = f.review
            if rstate == "pending":
                inner = reason(Facts(state="review:pending", holder=f.holder, drain_by=f.drain_by, mode=f.mode,
                                     can_take=f.can_take))
                return {**inner, "title": f"Code review {n}: {inner['title'].lower()}"}
            if rstate == "running":
                inner = reason(Facts(state="review:running", turn=f.turn, used_up=f.used_up))
                return {**inner, "title": f"Code review {n}: {inner['title'][0].lower()}{inner['title'][1:]}"}
            return r("review_agent", "busy", f"Code review {n}: {REVIEW_STATE_WORDS.get(rstate, rstate).lower()}")
        if f.fixes_waiting:
            return r("review_fixes", "busy", "Review comments go to the agent",
                     "People commented on the merge requests. The runner sends their comments to the agent before it "
                     "takes new work.")
        if f.kind == "review":
            return r("review_idle", "quiet", "Waiting for the next code review request",
                     "People wrote this code. The agent reviews it again when someone asks for a review on the ticket.")
        return r("mr_review", "wait", "Waiting for people to review and merge",
                 "The merge requests are out. Comments on them go back to the agent; a merge finishes the task.")
    if state == "failed":
        detail = f.reason or "The runner gave no reason."
        if f.continue_until:
            detail += f" A /continue comment on the ticket resumes it until {f.continue_until}."
        return r("failed", "bad", "Failed", detail)
    if state == "stopped":
        return r("stopped", "quiet", "Stopped", f.reason or "")
    if state == "done":
        return r("done", "good", "Done", f.reason or "")
    if state.startswith(("run:", "review:")):
        kind, _, rstate = state.partition(":")
        words = (RUN_STATE_WORDS if kind == "run" else REVIEW_STATE_WORDS).get(rstate, rstate)
        tone = "good" if rstate == "done" else "quiet" if rstate == "refused" else "busy"
        detail = f.reason or ""
        if f.stop_asked and rstate != "done":
            detail = ("Someone asked to stop it; the runner stops it on its next pass. " + detail).strip()
        return r(rstate, tone, words, detail)
    return r("unknown", "quiet", state)


def freshness(runner: dict, running: bool, view: dict | None, now: float) -> dict | None:
    """The marker over live figures that may not be current, or None when they are (or nothing live is shown).
    Over work never goes stale: what the database says about it is final."""
    if not running:
        return None
    if not runner["fresh"]:
        if runner["words"] == "Not answering":
            return {"tone": "bad", "words": "Live figures unknown: the runner's metrics do not answer.",
                    "detail": runner["detail"]}
        if runner["starting"]:
            return {"tone": "unknown", "words": "Live figures unknown until the runner's first pass ends.",
                    "detail": ""}
        return {"tone": "bad", "words": f"Out of date: the runner's {runner['detail']}.",
                "detail": "Everything below is as the runner last saw it, not necessarily what happens now."}
    if view is not None and view["observed_stale"]:
        if view["observed_at"] is None:
            return {"tone": "unknown", "words": "The runner has not read this work's engine files yet.",
                    "detail": "The step and the agent's activity show once it has."}
        return {"tone": "wait", "words": f"The runner last read the engine's files {status.ago(view['observed_at'], now)}.",
                "detail": "The step and the agent's activity are as they were then."}
    return None


def holder_words(store: ReviewStore) -> str:
    """What has the shared workspace now, in words, or ''."""
    active = store.tasks(*ACTIVE_STATES)
    if active:
        return f"{active[0].task_id}"
    review = store.review_holding()
    if review:
        return f"Code review {review.n} of {review.task_id}"
    run = store.run_holding()
    if run:
        return f"{status.RUN_WORDS[run.kind][0]}, run {run.n}"
    return ""


def drain_by(store: ReviewStore) -> str:
    found = store.state("drain")
    value = found[0] if found else None
    return (value.get("by") or "Someone") if isinstance(value, dict) and value else ""


def fixes_waiting(store: ReviewStore, task_id: str) -> bool:
    """People's comments on the task's MRs not sent to the agent yet (store.has_pending_review, for one task). Counted,
    never read: the comments' words are not the page's to show."""
    return store.conn.execute(
        "SELECT 1 FROM signals WHERE task_id = ? AND batch_id IS NULL"
        " UNION SELECT 1 FROM batches WHERE task_id = ? AND sent_at IS NULL LIMIT 1",
        (task_id, task_id)).fetchone() is not None


def question(task) -> dict | None:
    """The question the task waits on: {text, options, blocked, no_change, cut}. None unless it waits on one, and never an earlier
    question's words: the record keeps the last question asked, which a later one replaces."""
    if task.state != "needs_input" or not task.question:
        return None
    try:
        q = json.loads(task.question)
    except ValueError:
        return {"text": "", "options": [], "blocked": False, "no_change": False, "cut": False, "unreadable": True}
    if not isinstance(q, dict) or q.get("id") != task.question_id:
        return None
    text = q.get("text") if isinstance(q.get("text"), str) else ""
    options = [o for o in q.get("options") or [] if isinstance(o, str)] if isinstance(q.get("options"), list) else []
    return {"text": text, "options": options, "blocked": bool(q.get("blocked")), "no_change": bool(q.get("no_change")),
            "unreadable": False,
            # Kept before questions were kept whole: the ticket has the rest.
            "cut": len(text) == OLD_QUESTION_CUT + 1 and text.endswith("…")}


def duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60:02d} min"
    days, hours = s // 86400, s % 86400 // 3600
    return f"{days} d {hours} h" if hours else f"{days} d"


def limit_paused(task) -> bool:
    """Whether the runner holds the task's attempt clock for a usage window now (delivery._pause: a pause running),
    for the attempt now running: a pause that began before this attempt started is an earlier attempt's."""
    try:
        since = (json.loads(task.attempt_pause or "{}") or {}).get("since")
        started = task.attempt_started
        return bool(since) and (not started or datetime.fromisoformat(since) >= datetime.fromisoformat(started))
    except (ValueError, AttributeError, TypeError):
        return False
