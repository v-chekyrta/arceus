"""The main loop.

`observe` polls YouTrack, works out which issues are queued and who queued
them, checks health and writes what it would do to events.jsonl. It sends only
GET requests and changes nothing in YouTrack, GitLab or the workspace.

`live` does the same and then delivers (delivery.py): takes one queued ticket at
a time, runs it in Bulava, publishes the reviewed commit as an MR, carries
questions to the ticket and answers back.

Every service has its own next-poll time: a paused or slow
GitLab does not delay YouTrack, and the other way round.
"""

from __future__ import annotations

import contextlib
import signal
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from arceus_runner import bulava as bulava_mod
from arceus_runner.checks import Check, gitlab_client, token_expiry, youtrack_client
from arceus_runner.cleanup import ClaudeAdvisor, Cleanup, Docker, Situation
from arceus_runner.config import Config, Schedule, Secrets
from arceus_runner.db import ACTIVE_STATES, InstanceLock, ReviewStore, TaskStore
from arceus_runner.delivery import Delivery
from arceus_runner.review import ReviewLoop
from arceus_runner.engine import Engine
from arceus_runner.events import EventLog
from arceus_runner.gitlab import GitLabReview
from arceus_runner.gitops import Git, GitAuth, write_git_credentials
from arceus_runner.health import engine_doctor, local_checks
from arceus_runner.metrics import Metrics
from arceus_runner.progress import ProgressWatcher
from arceus_runner.screening import ClaudeClassifier, Classify, classifier_env
from arceus_runner.http import AuthError, BadRequest, NotFound, ServiceClient, ServiceError, TransientError
from arceus_runner.youtrack import Issue, YouTrackWriter

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DOCTOR_EVERY = 600
TOKEN_CHECK_EVERY = 3600


class LiveModeUnavailable(Exception):
    pass


def require_mode_ready(cfg: Config, probe: bulava_mod.Probe) -> None:
    """Live work needs the engine contracts it relies on, read from the installed engine itself."""
    if cfg.mode != "live":
        return
    missing = probe.missing_for_live()
    if cfg.bulava.model_env():
        missing += probe.missing_for_models()
    if not probe.installed or probe.problems or missing:
        why = "; ".join([*probe.problems, *(f"no {m}" for m in missing)]) or "engine not installed"
        raise LiveModeUnavailable(f"mode: live needs the Bulava server contract at {probe.root}: {why}. "
                                  "Install the pinned engine (setup.sh, BULAVA_SOURCE) or set mode: observe.")


def is_working_time(now: datetime, schedule: Schedule) -> bool:
    local = now.astimezone(schedule.tz)
    wh = schedule.working_hours
    return DAYS[local.weekday()] in wh.days and wh.start <= local.time() < wh.end


class Runner:
    def __init__(
        self,
        cfg: Config,
        store: TaskStore,
        events: EventLog,
        yt_client: ServiceClient,
        gl_client: ServiceClient,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        mono: Callable[[], float] = time.monotonic,
        engine: Engine | None = None,
        git: Git | None = None,
        metrics: Metrics | None = None,
        classify: Classify | None = None,
        cleanup: Cleanup | None = None,
    ):
        self.cfg = cfg
        self.metrics = metrics
        self.bulava_commit = ""
        self.store = store
        self.events = events
        self.now = now
        self.mono = mono
        self.yt_client = yt_client
        self.gl_client = gl_client
        self.yt = YouTrackWriter(yt_client)
        self.gl = GitLabReview(gl_client)
        self.delivery: Delivery | None = None
        if cfg.mode == "live":
            assert engine is not None and git is not None and classify is not None
            self.delivery = Delivery(cfg, store, events, self.yt, self.gl, engine, git, classify, now=now)
            self.review = ReviewLoop(self.delivery)
            self.delivery.review = self.review
        self.progress = ProgressWatcher(cfg.paths.bulava_state, cfg.paths.workspace, store, events, now,
                                        quiet_after=cfg.limits.agent_quiet_after) if self.delivery else None
        self.last_tick: float | None = None  # wall clock of the last finished tick, for the metrics
        self.cleanup = cleanup
        self.engine = engine
        self.health: dict[str, str] = {}
        self.next_due: dict[str, float] = {"local": 0.0, "youtrack": 0.0, "gitlab": 0.0, "doctor": 0.0,
                                           "token": 0.0, "progress": 0.0}
        self._unknown_trigger: set[str] = set()

    # -- health -------------------------------------------------------------

    def _record_health(self, checks: list[Check]) -> None:
        for c in checks:
            before = self.health.get(c.name)
            if before != c.status:
                self.events.emit("health_changed", check=c.name, status_from=before, status_to=c.status,
                                 detail=c.detail)
            self.health[c.name] = c.status

    def _service_failed(self, client: ServiceClient, err: ServiceError) -> None:
        if isinstance(err, AuthError):
            self._record_health([Check(f"service.{client.name}", "fail", str(err))])
            return
        if isinstance(err, TransientError):
            self.events.emit("service_paused", service=client.name, reason=str(err),
                             pause_s=round(client.state.pause_seconds))
        else:  # our request is wrong (400/404/422): no pause, needs a fix in the runner
            self.events.emit("request_failed", service=client.name, reason=str(err))
        self._record_health([Check(f"service.{client.name}", "warn", str(err))])

    # -- polls ----------------------------------------------------------------

    def poll_local(self) -> None:
        probe = bulava_mod.probe(self.cfg.paths.bulava)
        self.bulava_commit = probe.commit or probe.version or ""
        checks = local_checks(self.cfg, probe)
        # The engine's doctor talks to the CLIs; every DOCTOR_EVERY is enough to notice a lost sign-in.
        if self.engine and self.mono() >= self.next_due["doctor"]:
            self.engine.refresh_usage()
            self._doctor = engine_doctor(self.engine)
            self.next_due["doctor"] = self.mono() + DOCTOR_EVERY
        screening = self.delivery.gate.health() if self.delivery else None
        self._record_health(checks + getattr(self, "_doctor", []) + ([screening] if screening else []))

    def poll_gitlab(self) -> None:
        # GitLab as a health signal: reachable, token accepted, and (hourly) not about to expire.
        try:
            self.gl.me()
            checks = [Check("service.gitlab", "ok", "reachable")]
            if self.mono() >= self.next_due["token"]:
                checks.append(token_expiry(self.cfg, self.gl))
                self.next_due["token"] = self.mono() + TOKEN_CHECK_EVERY
        except ServiceError as e:
            self._service_failed(self.gl_client, e)
            return
        self._record_health(checks)

    def poll_youtrack(self) -> None:
        c = self.cfg.youtrack
        try:
            issues = self.yt.queued_issues(c.project, c.tags.queue, c.state_field, c.states.queued)
        except ServiceError as e:
            self._service_failed(self.yt_client, e)
            return
        for issue in issues:
            try:
                self._observe_issue(issue)
            except (NotFound, BadRequest) as e:
                # One broken or deleted issue is skipped; the rest are still processed.
                self.events.emit("issue_skipped", task_id=issue.key, reason=str(e))
            except ServiceError as e:
                self._service_failed(self.yt_client, e)
                return
        self._record_health([Check("service.youtrack", "ok", f"{len(issues)} queued")])
        if self.delivery:
            self.delivery.check_answers()
            self.delivery.check_continue()
            self.delivery.gate.check_decisions()

    def _observe_issue(self, issue: Issue) -> None:
        c = self.cfg.youtrack
        setter = self.yt.tag_setter(issue.db_id, c.tags.queue)
        if setter is None:
            # The search says the tag is there, the history does not say who set it.
            if issue.key not in self._unknown_trigger:
                self._unknown_trigger.add(issue.key)
                self.events.emit("trigger_unknown", task_id=issue.key,
                                 reason="tag present but its author is not in the issue history; not taken")
            return
        decision = self.store.record_trigger(issue.key, setter.activity_id, setter.author,
                                             setter.author in c.allowed_users, setter.timestamp)
        if decision.announced:
            return
        # Event first, then the mark: a crash in between repeats the event, it never loses it.
        self.events.emit(
            "task_queued" if decision.accepted else "trigger_rejected",
            task_id=issue.key,
            author=decision.author,
            tag_activity_id=decision.tag_activity_id,
            summary=issue.summary[:200],
            mode=self.cfg.mode,
            action="none: observe mode" if self.cfg.mode == "observe" else None,
            reason=None if decision.accepted else f"tag set by {decision.author}, who is not in allowed_users",
        )
        self.store.mark_announced(issue.key, setter.activity_id)
        if self.delivery and not decision.accepted:
            self.delivery.notice_rejected(issue.db_id, issue.key, decision.author, setter.activity_id)

    # -- scheduling -----------------------------------------------------------

    def _interval(self, client: ServiceClient) -> float:
        s = self.cfg.schedule
        base = (s.poll_working if is_working_time(self.now(), s) else s.poll_off_hours).total_seconds()
        if client.state.slow:
            base *= 2  # answered slowly: skip one regular poll of this service
        return max(base, client.seconds_left())

    def tick(self) -> None:
        """Run every poll that is due. Each one schedules its own next time."""
        now = self.mono()
        started = self.now()
        local_due = now >= self.next_due["local"]
        gitlab_due = now >= self.next_due["gitlab"]
        # The next time is set in `finally`: an unexpected error must not turn into a hot loop.
        if local_due:
            try:
                self.poll_local()
            finally:
                self.next_due["local"] = self.mono() + self.cfg.schedule.poll_local.total_seconds()
        for name, client, poll in (
            ("youtrack", self.yt_client, self.poll_youtrack),
            ("gitlab", self.gl_client, self.poll_gitlab),
        ):
            if now < self.next_due[name]:
                continue
            try:
                if client.available():
                    client.begin_poll()
                    poll()
            finally:
                self.next_due[name] = self.mono() + self._interval(client)
        if self.delivery and gitlab_due and self.gl_client.available() and self.yt_client.available():
            # The open MRs: review notes, CI, conflicts, merges (on GitLab's poll cadence).
            self.review.poll()
        if self.delivery and local_due:
            # Reconcile with `task show` every poll_local, then take new work if nothing is running.
            self.delivery.reconcile()
            can_take = self.can_take()
            self.clean_up(can_take, started)  # before new work: an overdue pass goes ahead of the next ticket
            if can_take:
                self.delivery.send_continue()  # a resumed task is new work for the workspace
            self.delivery.maybe_start(can_take)
        elif local_due:
            self.clean_up(False, started)  # observe: nothing runs; the pass only reports
        if self.cleanup and local_due:
            # Who holds the workspace: a change happened somewhere in this tick, so the hold before ends at its
            # start and the new one starts now; what was made in between is nobody's.
            try:
                self.cleanup.note_holder(next(iter(self.store.tasks(*ACTIVE_STATES)), None), started,
                                         began=self.cleanup.now())
            except Exception as e:
                self.events.emit("cleanup_failed", error=f"{type(e).__name__}: {e}")
        if self.progress and (local_due or now >= self.next_due["progress"]):
            # What the running task is doing: the engine's steps and the agent's transcript (files only).
            try:
                self.progress.poll()
            finally:
                self.next_due["progress"] = self.mono() + self.cfg.schedule.poll_progress.total_seconds()
        self.last_tick = self.now().timestamp()
        if self.metrics:
            self.metrics.update(self)

    def clean_up(self, can_take: bool, since: datetime | None = None) -> None:
        """A cleanup pass if one is due (cleanup.py). In the tick itself, so nothing switches the workspace
        meanwhile; it does not depend on health, which is often what low disk has broken."""
        if not self.cleanup:
            return
        holder = next(iter(self.store.tasks(*ACTIVE_STATES)), None)
        waiting = bool(self.delivery and can_take and (
            any(d.issue_key not in self.delivery._skipped for d in self.store.accepted_untaken())
            or self.delivery._continues_waiting()
            or (hasattr(self.store, "has_pending_review") and self.store.has_pending_review())))
        try:
            self.cleanup.maybe_run(Situation(holder, waiting, self.health.get("disk_free") in ("warn", "fail"),
                                             since))
        except Exception as e:  # cleanup must never stop the work it cleans up after
            self.events.emit("cleanup_failed", error=f"{type(e).__name__}: {e}")

    def can_take(self) -> bool:
        """New work only when nothing is failing and both trackers answer."""
        return (all(s != "fail" for s in self.health.values())
                and self.yt_client.available() and self.gl_client.available())

    def seconds_to_next(self) -> float:
        due = [t for k, t in self.next_due.items() if k not in ("doctor", "token")]  # ride on other polls
        return max(0.0, min(due) - self.mono())


def make_cleanup(cfg: Config, store: TaskStore, events: EventLog, engine: Engine | None) -> Cleanup:
    advise = None
    if cfg.cleanup.llm == "advise":
        advise = ClaudeAdvisor(classifier_env((engine or Engine(cfg)).token_env()), cfg.cleanup.llm_model,
                               cfg.cleanup.llm_timeout.total_seconds())
    # Local git only (status, stash, branches): no credentials.
    git = Git(None, cfg.gitlab.git_author.name, cfg.gitlab.git_author.email)
    return Cleanup(cfg, store, events, docker=Docker(), git=git, engine=engine, advise=advise)


def run(cfg: Config, secrets: Secrets, once: bool = False) -> int:
    probe = bulava_mod.probe(cfg.paths.bulava)
    require_mode_ready(cfg, probe)
    stop = threading.Event()
    with contextlib.ExitStack() as stack:
        lock = InstanceLock(cfg.paths.db.with_suffix(".lock"))
        lock.acquire()
        stack.callback(lock.release)
        store = ReviewStore(cfg.paths.db)
        stack.callback(store.close)
        events = EventLog(cfg.paths.events, cfg.project, redact=secrets.values())
        stack.callback(events.close)
        yc = youtrack_client(cfg, secrets)
        stack.callback(yc.close)
        gc = gitlab_client(cfg, secrets)
        stack.callback(gc.close)
        engine = git = classify = None
        if cfg.mode == "live":
            engine = Engine(cfg, events=events)
            classify = ClaudeClassifier(classifier_env(engine.token_env()), cfg.security.model,
                                        cfg.security.timeout.total_seconds())
            git = Git(GitAuth(cfg.gitlab.url, cfg.gitlab.bot_username, secrets.gitlab),
                      cfg.gitlab.git_author.name, cfg.gitlab.git_author.email)
        metrics = None
        if cfg.metrics.enabled:
            metrics = Metrics()
            stack.callback(metrics.serve(cfg.metrics.address, cfg.metrics.port))
        cleanup = make_cleanup(cfg, store, events, engine) if cfg.cleanup.enabled else None
        if cleanup:
            cleanup.recover()
        runner = Runner(cfg, store, events, yc, gc, engine=engine, git=git, metrics=metrics, classify=classify,
                        cleanup=cleanup)

        def _stop(signum, _frame):
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            previous = signal.signal(sig, _stop)
            stack.callback(signal.signal, sig, previous)

        events.emit("runner_started", mode=cfg.mode, db_schema=store.version, bulava=probe.as_dict())
        # Plain git works for the runner and the agent: the bot's credentials for the GitLab host only.
        if write_git_credentials(cfg.gitlab.url, cfg.gitlab.bot_username, secrets.gitlab):
            events.emit("git_credentials_written", host=cfg.gitlab.url)
        stack.callback(lambda: events.emit("runner_stopped", reason="signal" if stop.is_set() else "exit"))
        while not stop.is_set():
            try:
                runner.tick()
            except Exception as e:  # one bad tick must not kill the service
                events.emit("tick_failed", error=f"{type(e).__name__}: {e}")
                if once:
                    raise
            if once:
                break
            stop.wait(max(1.0, runner.seconds_to_next()))
    return 0
