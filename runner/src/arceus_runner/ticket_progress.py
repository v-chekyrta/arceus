"""The ticket says what the agent is doing, in the comment that started the attempt now running.

People follow a ticket in the tracker, not in Slack or Grafana: once the runner takes a ticket, people can wait for hours
without a sign of what the agent was doing. So the comment that started the attempt grows a Progress section that
the runner edits in place while the task runs: what happens now, when the attempt started, the agent's latest
actions, the steps so far (the brief delivered, the build, Codex's review and its verdict, a question asked), and the
merge requests once they are open. One comment per attempt, never a new one per step.

The first attempt writes in the `claim` comment (delivery.claim_on_ticket). A `/continue` writes in the runner's own
"picking this up again" comment, which is the newest on the ticket: left in the claim comment, a third attempt's progress would go on
a whole discussion above where people are reading, where nobody looks for it. When
the section moves on, the comment it leaves is put back as the runner first posted it.

    what changes                                   when the comment is edited
    the task's state, or the engine's step         at the next look
    only the agent's latest actions                at most every schedule.progress_comment_every
    nothing                                        never: an edit is not a heartbeat

The bot's edits are not silent (the tracker may tell the ticket's watchers), so routine activity is held to the
interval and an unchanged text is never written again.

What it reads: runner.db (the task, its merge requests), the claim comment once, and the progress watcher
(progress.py), which tells each new engine step and keeps the agent's latest actions. The agent's actions are the
watcher's safe words (its own one-line descriptions of commands, paths in the workspace), redacted like the
events and shown as code, so nothing in them can mention anyone or render as markup. What it keeps: one runner_state
value, `ticket_progress`, per task followed: the comment, the text it took over, the steps, what was last written.

A task is followed from the moment it runs (claimed, dispatched, publishing) until the comment has said how it ended;
a `/continue` later follows it again, in that attempt's own comment. A tracker without edit_comment, or `tracker.progress_comment: false`, leaves the
claim comment as it was posted. Telling must never stop the work: every failure here is an event, and the next look
tries again.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime

from arceus_runner.db import ACTIVE_STATES, Task
from arceus_runner.http import NotFound, ServiceError
from arceus_runner.progress import RECENT

STATE_KEY = "ticket_progress"
MARKER = "[arceus:claim]"
STARTS = ("[arceus:claim]", "[arceus:continue:")  # the runner's comments that start an attempt
HEAD = "**Progress**"
SEPARATOR = "\n\n---\n" + HEAD
WAITING = ("needs_input", "code_review", "security_hold")  # people's turn: followed while it was followed before
ENDED = ("failed", "done", "stopped")
FORGET_AFTER = 30 * 86400  # a task whose comment said how it ended is forgotten after this; a later /continue
                           # starts its steps afresh
STEPS_KEPT = 14   # the first (taken) and the latest ones
TARGET_LIMIT = 110

# The engine's steps that are worth a line, (stage, state) -> words. The others (compose, context, peer-*) are the
# preparation of the brief, said once by "brief delivered".
STEP_WORDS = {
    ("deliver", "delivered"): "the brief reached the agent",
    ("gate", "running"): "the agent finished a pass; checks started",
    ("gate.verify", "done"): "build and checks passed",
    ("gate.verify", "failed"): "build or checks failed; the agent fixes them",
    ("gate.check", "done"): "independent code and security checks: nothing to send back",
    ("gate.check", "findings"): "independent checks found problems; sent back to the agent",
    ("gate.check", "skipped"): "independent checks skipped: the build is red",
    ("gate.review", "retrying"): "Codex review: sent back for rework",
    ("gate.review", "verified"): "Codex review: accepted",
    ("run", "needs-user"): "the agent asked a question",
    ("run", "passed"): "the attempt finished",
}
# The runner's own states, said when the task moves into one.
STATE_WORDS = {
    "claimed": "taken",
    "dispatched": "the agent started",
    "publishing": "opening merge requests",
    "needs_input": "waiting for an answer on this ticket",
    "code_review": "merge requests opened for review",
    "security_hold": "waiting for a security review",
    "failed": "failed",
    "done": "done",
    "stopped": "stopped",
}


def _now_words(task: Task, step: tuple[str, str] | None, working: bool = False) -> str:
    """What is happening, in one line. `working`: the agent has used tools in this attempt, so a step that still
    reads as preparation (the engine says `pipeline done` once the brief is out, and nothing more until the gate)
    is not what happens now."""
    if task.state == "claimed":
        return "Preparing the workspace."
    if task.state == "dispatched":
        if not step and not working:
            return "Preparing the brief for the agent."
        if not step:
            return "The agent is reading the code to answer." if task.research else "The agent is working on the change."
        stage, state = step
        if state in ("waiting", "parked"):
            return "Paused: the agent waits for its usage limit to reset; it goes on by itself."
        if stage == "gate.verify" and state == "running":
            return "Building and running the project's checks."
        if stage == "gate.check" and state == "running":
            return "Independent checks of the code and its security."
        if stage == "gate.review" and state == "running":
            return "Codex is reviewing the change."
        if stage == "gate" and state == "running":
            return "The agent finished a pass; checks are starting."
        if stage == "gate.review" and state == "retrying":
            return "The agent is reworking the change after Codex's review."
        if stage in ("outcome", "run"):
            return "The attempt is over; the runner takes its result."
        if not working and (stage in ("pipeline", "compose", "context", "align", "peer-claude", "peer-codex") or
                            (stage == "deliver" and state == "running")):
            return "Preparing the brief for the agent."
        return "The agent is reading the code to answer." if task.research else "The agent is working on the change."
    return {
        "publishing": "Opening merge requests.",
        "needs_input": "Waiting for an answer: the agent's question is in the comments below.",
        "code_review": "The merge requests are open and wait for review.",
        "security_hold": "Paused: a message for the agent waits for a security review.",
        "failed": "Stopped with an error: the reason is in the runner's comment below.",
        "done": "Done.",
        "stopped": "Stopped.",
    }.get(task.state, f"{task.state}.")


def _epoch(iso: str | None) -> float:
    """The attempt's start as epoch seconds; 0 when unknown."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def _start_marker(text: str) -> str | None:
    """The marker of a runner comment that starts an attempt, or None."""
    m = re.search(r"\[arceus:(?:claim|continue:[^\]\s]+)\]", text or "")
    return m.group(0) if m else None


def _code(text: str) -> str:
    """Words from the agent's session as inline code: no mention, link or markup can come of them."""
    text = " ".join(text.replace("`", "'").split())
    if len(text) > TARGET_LIMIT:
        text = text[:TARGET_LIMIT] + "…"
    return f"`{text}`" if text else ""


class TicketProgress:
    def __init__(self, delivery, watcher, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.d = delivery
        self.cfg = delivery.cfg
        self.store = delivery.store
        self.events = delivery.events
        self.tracker = delivery.tracker
        self.watcher = watcher
        self.now = now
        self.every = self.cfg.schedule.progress_comment_every.total_seconds()
        self.followed: dict[str, dict] = {}
        self.early: dict[str, list[tuple]] = {}  # steps of a task told before it was followed
        saved = self.store.state(STATE_KEY)
        if saved and isinstance(saved[0], dict):
            self.followed = saved[0]
        watcher.on_step = self.step

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.tracker.progress_comment) and callable(getattr(self.tracker, "edit_comment", None))

    def _save(self) -> None:
        self.store.set_state(STATE_KEY, self.followed)

    # -- what happened -----------------------------------------------------------------------------

    def step(self, task_id: str, attempt: int, stage: str, state: str, ms: int, note: str) -> None:
        """A new engine step of a running task (progress.ProgressWatcher.on_step). Never raises: it is called from
        inside the watcher's look."""
        try:
            entry = self.followed.get(task_id)
            if entry is None:  # not followed yet (its comment is read after this look): kept until it is
                early = self.early.setdefault(task_id, [])
                early.append((attempt, stage, state, ms, note))
                del early[:-STEPS_KEPT]
                return
            self._step(entry, attempt, stage, state, ms, note)
            self._save()
        except Exception as e:
            self.events.emit("progress_comment_failed", task_id=task_id, error=f"{type(e).__name__}: {e}")

    def _step(self, entry: dict, attempt: int, stage: str, state: str, ms: int, note: str) -> None:
        if entry.get("gone"):
            return
        entry["step"] = [stage, state]
        words = STEP_WORDS.get((stage, state))
        if state in ("waiting", "parked"):
            words = "paused for the usage limit"
        if words and stage == "gate.review" and state == "retrying":
            n = note.rsplit(" ", 1)[-1] if note else ""
            words += f" ({n} remarks)" if n.isdigit() else ""
        if words:
            self._note(entry, (ms / 1000) if ms else self.now().timestamp(), words, attempt)

    def _note(self, entry: dict, when: float, words: str, attempt: int = 0) -> None:
        steps = entry.setdefault("steps", [])
        if attempt and attempt != entry.get("attempt"):
            if entry.get("attempt"):
                steps.append([when, f"attempt {attempt} started"])
            entry["attempt"] = attempt
        if steps and steps[-1][1] == words:
            return
        steps.append([when, words])
        if len(steps) > STEPS_KEPT:
            del steps[1:len(steps) - STEPS_KEPT + 1]

    # -- one look ----------------------------------------------------------------------------------

    def tick(self) -> None:
        if not self.enabled:
            return
        now = self.now().timestamp()
        stale = [k for k, e in self.followed.items()
                 if (e.get("closed") or e.get("gone")) and now - float(e.get("edited") or e.get("since") or 0) > FORGET_AFTER]
        if stale:
            for k in stale:
                del self.followed[k]
            self._save()
        running = {t.task_id: t for t in self.store.tasks(*ACTIVE_STATES) if not t.people_work}
        for task_id in sorted(set(running) | {k for k, e in self.followed.items() if not e.get("closed")}):
            task = running.get(task_id) or self.store.task(task_id)
            if task is None or task.people_work:
                self.followed.pop(task_id, None)
                self._save()
                continue
            try:
                self._look(task)
            except ServiceError as e:  # the tracker is down or paused: the next look writes what is due then
                self._problem(task, str(e))
            except Exception as e:  # never let telling stop the work
                self._problem(task, f"{type(e).__name__}: {e}")
        # Steps told for what is not followed and not running (a code review's or a test run's own engine task): gone.
        self.early = {k: v for k, v in self.early.items() if k in running}

    def _problem(self, task: Task, reason: str) -> None:
        entry = self.followed.get(task.task_id)
        if entry is not None and entry.get("problem") != reason:
            entry["problem"] = reason
            self._save()
            self.events.emit("progress_comment_failed", task_id=task.task_id, reason=reason)

    def _look(self, task: Task) -> None:
        entry = self.followed.get(task.task_id)
        # A `/continue` the runner commented on (or an entry kept by an older runner, which has no count) looks
        # again for the comment that started the attempt: the section moves into the runner's newest comment.
        continues = self.store.count_actions(f"{task.task_id}:comment:continue:")
        if entry is None or (entry.get("closed") and task.active) or entry.get("continues") != continues:
            entry = self._follow(task, entry, continues)
            if entry is None:
                return
        if entry.get("leftover") and not self._restore(task, entry["leftover"]):
            pass  # tried again at the next look
        elif entry.get("leftover"):
            del entry["leftover"]
            self._save()
        if entry.get("gone"):
            return
        now = self.now().timestamp()
        if task.state != entry.get("state"):
            back = task.state in ACTIVE_STATES and entry.get("state") in (*WAITING, *ENDED)
            self._note(entry, now, "back at work" if back else STATE_WORDS.get(task.state, task.state))
        p = self.watcher.tasks.get(task.task_id) if task.state == "dispatched" else None
        if p is not None:
            # The watcher counts from its own start: after a restart it starts again at none, and what the entry
            # kept stays until there is something newer. A new attempt counts afresh.
            if p.attempt and not entry.get("attempt"):
                entry["attempt"] = p.attempt  # said by "Attempt N started" before any step of it is told
            if p.attempt != entry.get("calls_attempt"):
                entry.update(calls_attempt=p.attempt, calls=0, calls_seen=0)
            seen = int(entry.get("calls_seen") or 0)
            entry["calls"] = int(entry.get("calls") or 0) + (p.calls - seen if p.calls >= seen else p.calls)
            entry["calls_seen"] = p.calls
            if p.recent:
                new = [[t, tool, target] for t, tool, target in p.recent]
                kept = [r for r in entry.get("recent") or [] if r not in new and r[0] <= new[0][0]]
                entry["recent"] = (kept + new)[-RECENT:]
            if p.step:
                entry["step"] = [p.step.stage, p.step.state]
        body = self.render(task, entry)
        digest = hashlib.sha256(body.encode()).hexdigest()[:16]
        if digest == entry.get("digest"):
            entry["state"] = task.state
            return
        moved = task.state != entry.get("state") or entry.get("step") != entry.get("written_step")
        if not moved and now - float(entry.get("edited") or 0) < self.every:
            return
        text = self.compose(entry, body, now)
        try:
            self.tracker.edit_comment(task.issue_db_id, entry["comment"], text)
        except NotFound:
            entry["gone"] = True  # someone removed the comment: the ticket is not written to again for it
            self._save()
            self.events.emit("progress_comment_gone", task_id=task.task_id, comment=entry["comment"])
            return
        entry.update(digest=digest, edited=now, state=task.state, written_step=entry.get("step"), problem=None)
        if task.state in ENDED:
            entry["closed"] = True  # it has said how the task ended
        self._save()
        self.events.emit("progress_comment_updated", task_id=task.task_id, state=task.state,
                         step="/".join(entry.get("step") or []) or None)

    def _follow(self, task: Task, entry: dict | None, continues: int) -> dict | None:
        """Follow a task in the newest comment that started one of its attempts: the claim, or the runner's comment
        for a `/continue` (`continues` of them were journaled). The comment the section leaves is put back."""
        act = self.store.action(f"{task.task_id}:comment:claim")
        if not act or act[0] != "done" or not act[1]:
            return None  # not posted yet: the next look
        if any(k.startswith(f"{task.task_id}:comment:continue:") for k, *_ in self.store.pending_actions()):
            return None  # this attempt's own comment is about to be posted: follow it, not the one before
        bot = self.cfg.tracker.bot_user
        comments = self.tracker.comments(task.issue_db_id)
        starts = [c for c in comments if c.author == bot and _start_marker(c.text)]
        if sum(1 for c in starts if c.text and "[arceus:continue:" in c.text) < continues:
            return None  # posted, but the tracker does not list it yet: the next look
        if not starts:
            cid = (entry or {}).get("comment") or json.loads(act[1])[0]
            self.followed[task.task_id] = {"comment": cid, "gone": True, "since": self.now().timestamp(),
                                           "continues": continues}
            self._save()
            self.events.emit("progress_comment_gone", task_id=task.task_id, comment=cid)
            return None
        c = max(starts, key=lambda x: x.created or 0)
        marker = _start_marker(c.text)
        base = c.text.split(SEPARATOR, 1)[0].replace(marker, "").rstrip()
        if entry and not entry.get("gone") and entry.get("comment") == c.id:
            # The same comment (a `/continue` the runner did not comment on): the steps so far go on.
            entry.update(base=base, marker=marker, closed=False, continues=continues)
        else:
            since = (c.created / 1000) if c.created else self.now().timestamp()
            new = {"comment": c.id, "marker": marker, "base": base, "since": since, "steps": [],
                   "continues": continues}
            self._note(new, since, STATE_WORDS["claimed"] if marker == MARKER else "picked up again")
            if entry and not entry.get("gone") and entry.get("comment"):
                # What the watcher already told of this attempt stays with it; the comment it leaves is put back.
                for t, words in entry.get("steps") or []:
                    if float(t) >= since and words != "back at work":
                        self._note(new, float(t), words)
                for k in ("step", "attempt", "calls", "calls_seen", "calls_attempt"):
                    if k in entry:
                        new[k] = entry[k]
                new["recent"] = [r for r in entry.get("recent") or [] if float(r[0]) >= since]
                old = next((x for x in comments if x.id == entry["comment"]), None)
                if old is not None and SEPARATOR in (old.text or ""):
                    leftover = {"comment": old.id, "text": old.text.split(SEPARATOR, 1)[0].rstrip() + "\n\n" +
                                (_start_marker(old.text) or entry.get("marker") or MARKER)}
                    if not self._restore(task, leftover):
                        new["leftover"] = leftover
            entry = new
        for step in self.early.pop(task.task_id, []):
            self._step(entry, *step)
        self.followed[task.task_id] = entry
        self._save()
        return entry

    def _restore(self, task: Task, leftover: dict) -> bool:
        """Put the comment the section left back as it reads without the section: one Progress on the ticket at a
        time. What a person changed above the section stays. False: try again at the next look."""
        try:
            self.tracker.edit_comment(task.issue_db_id, leftover["comment"], leftover["text"])
        except NotFound:
            return True  # removed meanwhile: nothing to put back
        except ServiceError as e:
            self.events.emit("progress_comment_failed", task_id=task.task_id, reason=f"restoring the old one: {e}")
            return False
        return True

    # -- the text ----------------------------------------------------------------------------------

    def _clock(self, epoch: float) -> str:
        return datetime.fromtimestamp(epoch, UTC).astimezone(self.cfg.schedule.tz).strftime("%H:%M")

    def _day_clock(self, epoch: float, now: float) -> str:
        when = datetime.fromtimestamp(epoch, UTC).astimezone(self.cfg.schedule.tz)
        today = datetime.fromtimestamp(now, UTC).astimezone(self.cfg.schedule.tz).date()
        return when.strftime("%H:%M") if when.date() == today else when.strftime("%b %d %H:%M")

    def render(self, task: Task, entry: dict) -> str:
        """Everything the section says except the time it was written: what decides whether to edit."""
        now = self.now().timestamp()
        step = tuple(entry["step"]) if entry.get("step") else None
        working = int(entry.get("calls") or 0) > 0 or any(float(r[0]) >= _epoch(task.attempt_started)
                                                          for r in entry.get("recent") or [])
        started = self._day_clock(float(entry.get("since") or now), now)
        n = int(entry.get("attempt") or 0)
        lines = [f"**Now:** {_now_words(task, step, working)}", "",
                 f"Attempt {n} started {started}." if n > 1 else f"Started {started}."]
        recent = entry.get("recent") or []
        if recent and (task.active or task.state in WAITING):
            calls = int(entry.get("calls") or 0)
            lines += ["", "**Latest agent actions**" + (f" ({calls} tool call{'s' if calls != 1 else ''} in this attempt):" if calls else ":")]
            for t, _tool, target in reversed(recent):
                shown = _code(self.events.redact(str(target)))
                if shown:
                    lines.append(f"- {self._clock(float(t))} {shown}")
        if entry.get("steps"):
            lines += ["", "**Steps:**"]
            lines += [f"- {self._day_clock(float(t), now)} {words}" for t, words in entry["steps"]]
        mrs = sorted(m.url for m in self.store.task_mrs(task.task_id))
        if mrs:
            lines += ["", "**MRs:**"] + [f"- {u}" for u in mrs]
        return "\n".join(lines)

    def compose(self, entry: dict, body: str, now: float) -> str:
        tz = self.cfg.schedule.tz
        return (f"{entry['base']}{SEPARATOR} · updated {self._clock(now)} ({tz.key}), "
                f"kept up to date while the agent works\n\n{body}\n\n{entry.get('marker') or MARKER}")
