"""The project's Slack channel: what is told, how often, and that the webhook's trouble never stops the work."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from arceus_runner import notify
from arceus_runner.checks import Check
from arceus_runner.config import Config
from arceus_runner.db import ReviewStore as TaskStore
from arceus_runner.events import EventLog
from arceus_runner.notify import Notifier, Sender, clip_sentences
from arceus_runner.slack import Webhook

from .conftest import example_raw, read_events
from .test_delivery import NOW, World, no_real_tools, take  # noqa: F401  (the fixture applies here too)

HOOK = "https://hooks.slack.com/services/T0ACME/B0ACME/XXXXsecretXXXX"
SLACK = {"webhook_env": "SLACK_WEBHOOK_URL", "people": {"j.doe": "U0JDOE", "sec.lead": "U0SEC"}}


class FakeSlack:
    """The webhook's end: every post kept; answers are queued, 200 ok once they run out."""

    def __init__(self):
        self.posts: list[str] = []     # what Slack took
        self.attempts: list[str] = []  # everything that reached it
        self.answers: list = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        answer = self.answers.pop(0) if self.answers else (200, "ok", {})
        if isinstance(answer, Exception):
            raise answer
        code, body, headers = answer
        text = json.loads(request.content)["text"]
        self.attempts.append(text)
        if code == 200:
            self.posts.append(text)
        return httpx.Response(code, text=body, headers=headers)


class Clock:
    def __init__(self, t: datetime = NOW):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def wall(self) -> float:
        return self.t.timestamp()

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


class Bench:
    """A store, an event log, a notifier and a sender on one fake clock and a fake Slack."""

    def __init__(self, tmp: Path, review_agent: bool = False, **notify_settings):
        raw = example_raw()
        raw["slack"] = {**SLACK, "notify": notify_settings}
        raw["review_agent"]["enabled"] = review_agent
        raw["security"]["reviewers"] = ["sec.lead"]
        raw["paths"] = {"db": str(tmp / "runner.db"), "events": str(tmp / "events.jsonl"),
                        "secrets": str(tmp / "secrets.env")}
        self.cfg = Config.model_validate(raw)
        self.clock = Clock()
        self.slack = FakeSlack()
        self.store = TaskStore(self.cfg.paths.db)
        self.events = EventLog(self.cfg.paths.events, "acme", redact=[HOOK, "XXXXsecretXXXX"])
        self.notifier = self.new_notifier()

    def new_notifier(self) -> Notifier:
        n = Notifier(self.cfg, self.store, self.events, lambda key: f"https://yt.example/issue/{key}", self.clock)
        n.sender = Sender(self.cfg.paths.db, Webhook(HOOK, self.cfg.http, httpx.MockTransport(self.slack)), "acme",
                          self.events, self.cfg.http.backoff, clock=self.clock.wall, rng=lambda: 0.5,
                          schedule=self.cfg.schedule)
        return n

    @property
    def sender(self) -> Sender:
        return self.notifier.sender

    def flush(self) -> list[str]:
        """Everything due goes out, one post per MIN_GAP; what was posted now."""
        before = len(self.slack.posts)
        for _ in range(100):
            self.clock.advance(seconds=notify.MIN_GAP + 0.01)
            if not self.sender.send_one() and not self.sender.outbox().pending():
                break
        return self.slack.posts[before:]

    def task(self, task_id: str = "ACME-1", state: str = "dispatched", **fields) -> None:
        if self.store.task(task_id):
            self.store.update_task(task_id, state=state, **fields)
            return
        self.store.create_task(**{
            "task_id": task_id, "issue_db_id": "2-1", "state": state, "summary": "Fix login page",
            "tag_author": "j.doe", "repo": "", "repo_path": "/w", "branch": f"agent/{task_id}", "base_branch": "main",
            "question_id": None, "question_at_ms": None, "reason": None, "mr_urls": "[]", "attempt_started": None,
            "extra_repos": "[]", **fields})

    def events_named(self, *names) -> list[dict]:
        return [e for e in read_events(self.cfg.paths.events) if e["event"] in names]


@pytest.fixture
def b(tmp_path):
    bench = Bench(tmp_path)
    bench.notifier.tick("working")  # the first run records where things are and tells none of the past
    bench.flush()  # and the day's status: an idle spell untold by then would be told in it
    yield bench
    bench.events.close()
    bench.store.close()


# -- the webhook ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("answer, verdict, uncertain", [
    ((200, "ok", {}), "sent", False),
    ((429, "rate_limited", {"Retry-After": "30"}), "retry", False),
    ((500, "", {}), "retry", False),
    ((400, "invalid_payload", {}), "rejected", False),
    ((403, "action_prohibited", {}), "broken", False),
    ((404, "no_service", {}), "broken", False),
    ((410, "channel_is_archived", {}), "broken", False),
    ((302, "", {"location": "https://elsewhere.example"}), "broken", False),
    (httpx.ReadTimeout("read timed out for " + HOOK), "retry", True),
    (httpx.ConnectError("cannot connect to " + HOOK), "retry", False),
])
def test_webhook_answers_are_told_apart_and_never_quote_the_url(b, answer, verdict, uncertain):
    b.slack.answers.append(answer)
    posted = Webhook(HOOK, b.cfg.http, httpx.MockTransport(b.slack)).post("hi")
    assert (posted.verdict, posted.uncertain) == (verdict, uncertain)
    assert "XXXXsecretXXXX" not in posted.detail and "hooks.slack.com" not in posted.detail
    if isinstance(answer, tuple) and answer[0] == 429:
        assert posted.retry_after == 30


# -- the sender -------------------------------------------------------------------------------------------------


def test_a_question_goes_before_routine_and_one_post_per_gap(b):
    b.notifier.put("s", "summary", notify.ROUTINE, "summary")
    b.notifier.put("q", "question", notify.URGENT, "question")
    b.clock.advance(seconds=5)
    assert b.sender.send_one() and not b.sender.send_one()  # the second waits for MIN_GAP
    b.clock.advance(seconds=notify.MIN_GAP + 0.01)
    assert b.sender.send_one()
    assert [p.split("\n")[0] for p in b.slack.posts[-2:]] == ["question", "summary"]
    assert re.search(r"\n_acme · #\d+_$", b.slack.posts[0])  # the visible number: a repeated post is recognisable


def test_429_waits_for_retry_after_and_nothing_is_lost(b):
    b.notifier.put("q", "question", notify.URGENT, "question")
    b.slack.answers.append((429, "rate_limited", {"Retry-After": "40"}))
    b.clock.advance(seconds=2)
    assert b.sender.send_one()
    b.clock.advance(seconds=39)
    assert not b.sender.send_one() and b.slack.attempts[-1].startswith("question")  # only the refused attempt
    b.clock.advance(seconds=2)
    assert b.sender.send_one()
    assert b.slack.posts[-1].startswith("question") and b.sender.outbox().pending() == 0


def test_a_timeout_may_post_twice_with_the_same_number_and_a_sent_one_never_again(b, tmp_path):
    b.notifier.put("q", "question", notify.URGENT, "question")
    b.slack.answers.append(httpx.ReadTimeout("slow"))
    [post] = b.flush()  # the timeout, then the same message again after the backoff
    [sent] = [e for e in b.events_named("slack_sent") if e["kind"] == "question"]
    assert sent["maybe_twice"] is True and sent["attempts"] == 2 and post.endswith(f"#{sent['id']}_")
    # A restart: a new sender on the same runner.db sends nothing that was sent.
    b.notifier = b.new_notifier()
    b.clock.advance(minutes=5)
    assert b.flush() == []


def test_retry_after_outlives_a_restart(b):
    b.notifier.put("q", "question", notify.URGENT, "question")
    b.slack.answers.append((429, "rate_limited", {"Retry-After": "120"}))
    b.clock.advance(seconds=2)
    assert b.sender.send_one()
    b.notifier = b.new_notifier()  # the runner restarts within the two minutes
    b.clock.advance(seconds=60)
    assert not b.sender.send_one() and sum(x.startswith("question") for x in b.slack.attempts) == 1
    b.clock.advance(seconds=61)
    assert b.sender.send_one() and b.slack.posts[-1].startswith("question")


def test_a_broken_webhook_stays_paused_across_a_restart(b):
    b.notifier.put("q", "question", notify.URGENT, "question")
    b.slack.answers.append((404, "no_service", {}))
    b.flush()
    b.notifier = b.new_notifier()
    b.clock.advance(minutes=5)
    assert not b.sender.send_one() and b.sender.check().status == "warn"


def test_the_runners_secrets_never_reach_slack(b):
    secret = "glpat-SECRET&<token>1234"
    b.events._redact.append(secret)
    escaped = notify.esc(secret)
    b.task(summary=f"Rotate {secret}", reason=None)
    b.notifier.tick("working")
    out = b.flush()
    b.task(state="needs_input", question_id="q-1", question_at_ms=1, question=json.dumps(
        {"id": "q-1", "text": f"Is {secret} still valid?", "options": [f"keep {secret}", "rotate"]}))
    b.notifier.tick("waiting")
    out += b.flush()
    b.task(state="failed", reason=f"push refused for {secret}", failed_at_ms=1)
    b.notifier.tick("idle")
    out = "\n".join(out + b.flush())
    assert out.count("<redacted>") >= 6  # in the title of each message, the question, the option, the reason
    assert secret not in out and escaped not in out and "SECRET" not in out


def test_a_rejected_message_is_not_retried_and_the_next_goes(b):
    b.notifier.put("a", "x", notify.TRANSITION, "bad")
    b.notifier.put("b", "x", notify.TRANSITION, "good")
    b.slack.answers.append((400, "invalid_payload", {}))
    assert [p.split("\n")[0] for p in b.flush()] == ["good"]
    assert b.events_named("slack_rejected")[0]["reason"] == "HTTP 400 invalid_payload"


def test_a_broken_webhook_is_a_warning_not_a_failure_and_keeps_the_messages(b):
    b.notifier.put("q", "question", notify.URGENT, "question")
    b.slack.answers.append((404, "no_service", {}))
    b.flush()
    check = b.sender.check()
    assert check.status == "warn" and "no_service" in check.detail  # a fail would stop new tasks
    assert b.sender.outbox().pending() == 1
    b.clock.advance(minutes=30)
    assert b.flush() == []  # not hammered: tried again after an hour
    b.clock.advance(minutes=31)
    assert [p.split("\n")[0] for p in b.flush()] == ["question"]
    assert b.sender.check().status == "ok" and b.events_named("slack_restored")
    # doctor reads what the runner last knew
    assert notify.webhook_state(b.store).status == "ok"
    log = b.cfg.paths.events.read_text()
    assert "XXXXsecretXXXX" not in log and "hooks.slack.com" not in log


def test_stale_routine_is_dropped(b):
    b.notifier.put("s", "summary", notify.ROUTINE, "summary")
    b.clock.advance(hours=3)
    assert b.flush() == []
    assert b.events_named("slack_dropped")[0]["reason"] == "expired"


def test_the_thread_drains_on_close(b):
    b.clock.t = datetime.now(UTC)  # the real thread reads the real clock
    b.notifier.put("stop", "runner_stopped", notify.RUNNER, "stopped")
    real = Sender(b.cfg.paths.db, Webhook(HOOK, b.cfg.http, httpx.MockTransport(b.slack)), "acme", b.events,
                  b.cfg.http.backoff)
    real.start()
    real.close()
    assert b.slack.posts and b.slack.posts[-1].startswith("stopped")


# -- what a task's states are told as ---------------------------------------------------------------------------


def test_each_transition_is_told_once_and_survives_a_crash_before_the_telling(b):
    b.task(state="claimed")
    b.task(state="dispatched")
    b.notifier.tick("working")
    b.notifier.tick("working")
    [started] = b.flush()
    assert started.startswith("▶️ Started *<https://yt.example/issue/ACME-1|ACME-1>* Fix login page")
    # The state changes and the runner dies before any tick tells it: the next runner tells it.
    b.task(state="failed", reason="the gate refused <!channel> twice", failed_at_ms=1)
    b.notifier = b.new_notifier()
    b.notifier.tick("idle")
    [failed] = b.flush()
    assert failed.startswith("❌ <@U0JDOE> *<https://yt.example/issue/ACME-1|ACME-1>* Fix login page failed")
    assert "&lt;!channel&gt;" in failed and "<!channel>" not in failed
    assert "`/continue`" in failed
    b.notifier.tick("idle")
    assert b.flush() == []


def test_the_first_task_after_the_first_start_is_told(tmp_path):
    bench = Bench(tmp_path)  # a fresh runner.db: the constructor records the past, which is nothing
    bench.task(state="claimed")
    bench.notifier.tick("working")
    assert any(x.startswith("▶️ Started") for x in bench.flush())


def test_every_step_within_one_tick_is_told_when_its_event_comes(b):
    b.task(state="needs_input", question_id="q-1", question_at_ms=1)
    b.notifier.tick("waiting")
    b.flush()
    # The answer goes in and the task finishes before the end of the tick: both told.
    b.task(state="dispatched", question_id=None)
    b.events.emit("answer_sent", task_id="ACME-1")
    b.task(state="done", reason="no code changes")
    b.events.emit("task_done", task_id="ACME-1")
    b.notifier.tick("idle")
    out = b.flush()
    assert [x.split(" ")[0] for x in out if "ACME-1" in x] == ["▶️", "✅"] and "Got an answer" in out[0]


def test_a_telling_that_breaks_never_breaks_the_caller(b, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(b.notifier, "put", broken)
    b.events.emit("tick_failed", error="x")  # no exception here
    b.notifier.health(Check("disk_free", "fail", "1% free"))
    assert [e["on"] for e in b.events_named("slack_notify_failed") if "on" in e] == ["tick_failed"]
    assert b.events_named("slack_notify_failed")[-1]["check"] == "disk_free"


def test_question_carries_its_words_options_mention_and_where_to_answer(b):
    b.task()
    b.notifier.tick("working")
    b.flush()
    b.task(state="needs_input", question_id="q-1", question_at_ms=int(NOW.timestamp() * 1000),
           question=json.dumps({"id": "q-1", "text": "Postgres or <@U0BOSS>?", "options": ["Postgres", "SQLite"]}))
    b.notifier.tick("waiting")
    [q] = b.flush()
    assert q.startswith("❓ <@U0JDOE> *<https://yt.example/issue/ACME-1|ACME-1>* Fix login page needs an answer.")
    assert "> Postgres or &lt;@U0BOSS&gt;?" in q and "1. Postgres\n2. SQLite" in q
    assert "Answer with a comment on the ticket: https://yt.example/issue/ACME-1" in q


def test_reminders_daily_in_working_hours_and_none_after_the_answer(b):
    b.task(state="needs_input", question_id="q-1", question_at_ms=int(NOW.timestamp() * 1000))
    b.notifier.tick("waiting")
    b.flush()
    b.clock.advance(hours=23)
    b.notifier.tick("waiting")
    assert [x for x in b.flush() if "⏰" in x] == []
    b.clock.t = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)  # 21:00 in Kyiv: off hours
    b.notifier.tick("waiting")
    assert [x for x in b.flush() if "⏰" in x] == []
    b.clock.t = datetime(2026, 10, 22, 6, 30, tzinfo=UTC)  # 09:30 next day
    b.notifier.tick("waiting")
    [r] = [x for x in b.flush() if "⏰" in x]
    assert "<@U0JDOE>" in r and "still waits for an answer" in r and "Without one it fails in" in r
    b.notifier.tick("waiting")
    assert [x for x in b.flush() if "⏰" in x] == []  # once per period
    # The next one is queued, then the answer comes before it goes out: dropped, not posted.
    b.clock.advance(hours=24)
    b.notifier.remind()
    b.task(state="dispatched", question_id=None)
    b.notifier.sync_tasks()
    out = b.flush()
    assert [x for x in out if "⏰" in x] == [] and any("Got an answer" in x for x in out)
    assert b.events_named("slack_dropped")[-1]["reason"] == "the task moved on before it went out"


def test_held_text_mentions_the_security_reviewers(b):
    b.task()
    b.notifier.tick("working")
    b.flush()
    b.task(state="security_hold")
    b.notifier.tick("waiting")
    [held] = b.flush()
    assert held.startswith("🛡 <@U0SEC> *<https://yt.example/issue/ACME-1|ACME-1>*")


def test_mrs_done_and_stopped(b):
    b.task()
    b.notifier.tick("working")
    b.task(state="code_review", mr_urls=json.dumps(["https://gl.example/acme/mr/1"]))
    b.notifier.tick("waiting")
    b.task(state="done", reason="merge requests merged")
    b.notifier.tick("idle")
    out = b.flush()
    assert out[1].startswith("📬 Merge requests ready for review") and "• https://gl.example/acme/mr/1" in out[1]
    assert out[2].startswith("✅") and "every merge request is merged" in out[2]


def test_a_code_review_is_told_when_it_starts_and_with_its_result_per_merge_request(b):
    b.task(state="code_review")
    b.notifier.tick("waiting")
    b.flush()
    row = b.store.add_review("ACME-1", "c-1", "j.doe", 1, "")
    mrs = [{"label": "M1", "repo": "acme-platform", "url": "https://gl.example/acme/acme-platform/mr/4"},
           {"label": "M2", "repo": "acme-theme", "url": "https://gl.example/acme/acme-theme/mr/64"}]
    b.store.update_review(row.id, state="running", snapshot=json.dumps({"mrs": mrs}))
    b.events.emit("review_dispatched", task_id="ACME-1", review=row.id)
    [started] = b.flush()
    assert started.startswith("🔍 Code review of *<https://yt.example/issue/ACME-1|ACME-1>*")
    assert "started, asked by j.doe: 2 merge requests." in started
    b.store.update_review(row.id, state="done", outcome="unverified", result=json.dumps({"mrs": mrs}))
    b.events.emit("review_published", task_id="ACME-1", review=row.id, outcome="unverified",
                  mrs={"https://gl.example/acme/acme-platform/mr/4": "approved", "https://gl.example/acme/acme-theme/mr/64": "unverified"})
    [result] = b.flush()
    assert result.startswith("⚠️ Code review could not confirm: *<https://yt.example/issue/ACME-1|ACME-1>*")
    assert "• <https://gl.example/acme/acme-platform/mr/4|acme-platform>: approved, ready to merge" in result
    assert "• <https://gl.example/acme/acme-theme/mr/64|acme-theme>: could not be confirmed" in result
    b.events.emit("review_published", task_id="ACME-1", review=row.id, outcome="unverified", mrs={})
    assert b.flush() == []  # told once
    again = b.store.add_review("ACME-1", "c-2", "j.doe", 2, "")
    b.store.update_review(again.id, state="done", outcome="ready", result=json.dumps({"mrs": mrs}))
    b.events.emit("review_published", task_id="ACME-1", review=again.id, outcome="ready",
                  mrs={"https://gl.example/acme/acme-platform/mr/4": "ready", "https://gl.example/acme/acme-theme/mr/64": "approved"})
    [mixed] = b.flush()  # one approved, one not (no CI): the head does not say none was approved
    assert mixed.startswith("☑️ Code review found no problems, not everything is approved")


def test_a_test_run_of_the_platform_is_told_when_it_starts_and_ends_with_its_summary(b):
    run = b.store.add_run("qa", "schedule", "schedule", slot="2026-10-20T02:00:00+03:00")
    b.events.emit("qa_dispatched", task_id=run.id, run=run.n, source="schedule", by="schedule")
    [started] = b.flush()
    assert started.startswith("🧪 Platform test started: run 1, on schedule.")
    b.store.update_run(run.id, state="done", outcome="found", result=json.dumps(
        {"summary": "Checked sign-in and checkout. <!channel> Checkout with a discount code fails.",
         "findings": [{}, {}]}))
    b.events.emit("qa_published", task_id=run.id, run=run.n, outcome="found", findings=2,
                  url="http://localhost:3300/1/")
    [result] = b.flush()
    assert result.startswith("🐞 Platform test found 2 problems: run 1.")
    assert "&lt;!channel&gt;" in result and "<!channel>" not in result
    assert "Report (through the SSH tunnel): http://localhost:3300/1/" in result
    b.events.emit("qa_published", task_id=run.id, run=run.n, outcome="found", findings=2, url="x")
    assert b.flush() == []  # told once
    # The agent's activity while it tests reads as the run, not as a ticket.
    b.events.emit("agent_quiet", task_id=run.id, minutes=20, ts="t1")
    [quiet] = b.flush()
    assert "*the platform test, run 1*" in quiet and "yt.example" not in quiet
    # A run that checked nothing is told as unfinished with the runner's reason, however its summary sounds.
    again = b.store.add_run("qa", "command", "ops")
    b.store.update_run(again.id, state="done", outcome="unfinished",
                          reason="every check was skipped: nothing was tested",
                          result=json.dumps({"summary": "All good.", "findings": []}))
    b.events.emit("qa_published", task_id=again.id, run=again.n, outcome="unfinished", findings=0, url="u")
    [told] = b.flush()
    assert told.startswith("⚠️ Platform test did not finish: run 2.")
    assert "Why: every check was skipped: nothing was tested" in told and "no problems" not in told



def test_an_architecture_review_is_told_with_what_is_new_fixed_and_what_codex_thinks(b):
    run = b.store.add_run("arch", "schedule", "schedule", slot="2026-10-25T03:00:00+02:00")
    b.events.emit("arch_dispatched", task_id=run.id, run=run.n, source="schedule", by="schedule")
    [started] = b.flush()
    assert started.startswith("🏛 Architecture review started: run 1, on schedule.")
    b.store.update_run(run.id, state="done", outcome="found", result=json.dumps(
        {"summary": "Billing grew a retry of its own.", "findings": [{}, {}],
         "ledger": {"new": [3], "still": [1], "fixed": [2]},
         "second_opinion": {"status": "done", "verdicts": {"1": {"verdict": "confirmed"},
                                                           "2": {"verdict": "rejected"}}}}))
    b.events.emit("arch_published", task_id=run.id, run=run.n, outcome="found", findings=2, url="http://localhost:3301/1/")
    [told] = b.flush()
    assert told.startswith("🏛 Architecture review: 2 findings: run 1 (1 new, 1 still open, 1 fixed).")
    assert "Second check: confirmed 1 of 2." in told and "Billing grew a retry of its own." in told
    assert "Codex" not in told
    assert "Report (through the SSH tunnel): http://localhost:3301/1/" in told
    b.events.emit("agent_quiet", task_id=run.id, minutes=20, ts="t1")
    [quiet] = b.flush()
    assert "*the architecture review, run 1*" in quiet and "yt.example" not in quiet
    # Codex missing is said, never counted as agreement.
    again = b.store.add_run("arch", "command", "ops")
    b.store.update_run(again.id, state="done", outcome="found", result=json.dumps(
        {"summary": "", "findings": [{}], "second_opinion": {"status": "unavailable", "why": "quota"}}))
    b.events.emit("arch_published", task_id=again.id, run=again.n, outcome="found", findings=1, url="u")
    [missing] = b.flush()
    assert "The second check of the findings did not run: see the report." in missing
    assert "confirmed" not in missing and "Codex" not in missing


def test_a_background_run_started_by_the_command_does_not_name_the_servers_user(b):
    for kind, head in (("qa", "🧪 Platform test started: run 1."), ("arch", "🏛 Architecture review started: run 1.")):
        run = b.store.add_run(kind, "command", "ai")
        b.events.emit(f"{kind}_dispatched", task_id=run.id, run=run.n, source="command", by="ai")
        [started] = b.flush()
        assert started.splitlines()[0] == head and "ai" not in started.splitlines()[0]


def test_code_in_a_summary_shows_as_code_and_a_cut_inside_it_is_closed(b):
    summary = "The retry is in billing/retry.py:12 and `send()`. " + "The client in `" + "x" * 700 + "` too."
    run = b.store.add_run("arch", "command", "ops")
    b.store.update_run(run.id, state="done", outcome="found", result=json.dumps({"summary": summary, "findings": [{}]}))
    b.events.emit("arch_published", task_id=run.id, run=run.n, outcome="found", findings=1, url="u")
    [told] = b.flush()
    said = told.splitlines()[1]
    assert said.startswith("The retry is in `billing/retry.py:12` and `send()`.")
    assert said.count("`") % 2 == 0


def test_a_skipped_second_check_is_said_without_a_count(b):
    run = b.store.add_run("arch", "command", "ops")
    b.store.update_run(run.id, state="done", outcome="found", result=json.dumps(
        {"summary": "", "findings": [{}, {}], "second_opinion": {"status": "skipped", "why": "the run was stopped"}}))
    b.events.emit("arch_published", task_id=run.id, run=run.n, outcome="found", findings=2, url="u")
    [told] = b.flush()
    assert "The second check of the findings was skipped: the run was stopped." in told
    assert "confirmed" not in told


def test_a_fenced_block_in_a_summary_stays_one_and_a_cut_inside_it_is_closed(b):
    run = b.store.add_run("arch", "command", "ops")
    summary = "Before ```one``` and `two`. The wrapper is ```" + "y" * 700 + "``` here."
    b.store.update_run(run.id, state="done", outcome="found", result=json.dumps({"summary": summary, "findings": [{}]}))
    b.events.emit("arch_published", task_id=run.id, run=run.n, outcome="found", findings=1, url="u")
    [told] = b.flush()
    said = told.splitlines()[1]
    assert said.startswith("Before ```one``` and `two`.")
    assert said.count("```") % 2 == 0 and said.replace("```", "").count("`") % 2 == 0


def test_a_long_test_run_summary_is_told_in_whole_sentences(b):
    # Run 1 on the server: a five-sentence summary was cut at 600 characters in the middle of a word.
    first = "I tested the piloting work merged on Dev in the last two weeks as six roles. " * 3
    summary = first + "I found 4 major problems: a terminated request can still be edited through the API. " + \
        "I also found 5 minor UI and copy problems. " * 12
    run = b.store.add_run("qa", "command", "ops")
    b.store.update_run(run.id, state="done", outcome="found",
                          result=json.dumps({"summary": summary, "findings": [{}] * 9}))
    b.events.emit("qa_published", task_id=run.id, run=run.n, outcome="found", findings=9, url="u")
    [told] = b.flush()
    said = told.splitlines()[1]
    assert said.endswith("problems. …") and len(said) <= 602 and "prob …" not in said


def test_clip_sentences_keeps_whole_sentences_or_whole_words():
    assert clip_sentences("One. Two.", 600) == "One. Two."
    assert clip_sentences("First sentence here. Second one is longer than this.", 30) == "First sentence here. …"
    assert clip_sentences("a" * 20 + " " + "b" * 40, 30) == "a" * 20 + "…"
    assert clip_sentences("One two. Three four five six.", 20) == "One two. …"  # a short first sentence still fits
    assert clip_sentences("  spaced\n  out  ", 600) == "spaced out"

# -- routine: summaries, idle, the daily status ------------------------------------------------------------------


def test_activity_is_summed_up_every_half_hour_with_its_errors_and_only_when_there_was_any(b):
    b.task()
    b.notifier.tick("working")
    b.flush()
    b.events.emit("agent_activity", task_id="ACME-1", tools=5, by_tool={"Edit": 3, "Bash": 2}, errors=2,
                  last_tool="Bash", last_target="run the <!here> tests")
    b.events.emit("engine_step", task_id="ACME-1", stage="agent", state="running")
    b.clock.advance(minutes=10)
    b.notifier.tick("working")
    assert b.flush() == []
    b.clock.advance(minutes=21)
    b.notifier.tick("working")
    [s] = b.flush()
    assert "5 tool calls (Edit 3, Bash 2); 2 came back with an error" in s  # calls, never "done"
    assert "step: agent (running)" in s and "&lt;!here&gt;" in s
    b.clock.advance(minutes=31)
    b.notifier.tick("working")
    assert [x for x in b.flush() if "tool calls" in x] == []  # nothing new: no summary


def test_the_call_count_is_exact_when_the_detail_is_cut_to_the_most_used(b):
    b.task()
    b.notifier.tick("working")
    b.flush()
    # progress.py names at most six kinds; the count is of every call.
    b.events.emit("agent_activity", task_id="ACME-1", tools=8,
                  by_tool={"Read": 2, "Edit": 1, "Bash": 1, "Grep": 1, "Glob": 1, "Write": 1})
    b.clock.advance(minutes=31)
    b.notifier.tick("working")
    [s] = b.flush()
    assert "8 tool calls (Read 2, Edit 1, Bash 1, Grep 1, other 3)" in s


def test_idle_is_told_once_after_a_quarter_hour_and_never_while_people_owe_an_answer(b):
    b.notifier.tick("waiting")
    b.clock.advance(hours=1)
    b.notifier.tick("waiting")
    assert [x for x in b.flush() if "💤" in x] == []
    b.notifier.tick("idle")
    b.clock.advance(minutes=10)
    b.notifier.tick("idle")
    assert [x for x in b.flush() if "💤" in x] == []
    b.clock.advance(minutes=6)
    b.notifier.tick("idle")
    [idle] = [x for x in b.flush() if "💤" in x]
    assert idle.startswith("💤 Nothing to take: the queue is empty. Add the `agent` tag")
    # At once, with what it can take.
    assert "What the agent can take:\n• `agent` tag on a ticket" in idle
    assert "`agent-research`" in idle and "`/continue`" in idle
    assert "• Code review on request (`/review` on a ticket in Code Review) is off for this project" in idle
    for _ in range(4):  # and never again while the spell lasts
        b.clock.advance(hours=1)
        b.notifier.tick("idle")
    assert [x for x in b.flush() if "💤" in x] == []
    b.notifier.tick("unknown")  # the queue could not be read: not the end of the spell
    b.clock.advance(minutes=30)
    b.notifier.tick("idle")
    b.clock.advance(minutes=30)
    b.notifier.tick("idle")
    assert [x for x in b.flush() if "💤" in x] == []
    b.notifier.tick("working")  # work, then idle again: a new spell, told once
    b.notifier.tick("idle")
    b.clock.advance(minutes=16)
    b.notifier.tick("idle")
    assert len([x for x in b.flush() if "💤" in x]) == 1


def test_an_idle_spell_is_told_once_across_a_restart_and_with_an_old_idle_repeat(tmp_path):
    bench = Bench(tmp_path, idle_repeat="2h")  # a config.yaml from before still loads; the setting does nothing
    bench.notifier.tick("working")
    bench.notifier.tick("idle")
    bench.clock.advance(minutes=16)
    bench.notifier.tick("idle")
    assert len([x for x in bench.flush() if "💤" in x]) == 1
    bench.notifier = bench.new_notifier()
    for _ in range(3):
        bench.clock.advance(hours=2)
        bench.notifier.tick("idle")
    assert [x for x in bench.flush() if "💤" in x] == []


def test_an_idle_spell_from_off_hours_is_told_in_the_morning_once(b):
    b.clock.t = datetime(2026, 10, 20, 17, 0, tzinfo=UTC)  # 20:00 in Kyiv: the work is over, off hours
    b.notifier.tick("idle")
    b.clock.t = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)
    b.notifier.tick("idle")
    assert b.flush() == []
    b.clock.t = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)  # 09:00 next day: the plain status, then the 💤
    b.notifier.tick("idle")
    daily, idle = b.flush()
    assert daily.startswith("☀️") and "Nothing in work and nothing queued" in daily
    assert "What the agent can take" not in daily
    assert idle.startswith("💤 Nothing to take") and "What the agent can take:\n• `agent` tag on a ticket" in idle
    b.clock.t = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    b.notifier.tick("idle")
    b.clock.t = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)  # the next morning, the same spell: the status alone
    b.notifier.tick("idle")
    [daily] = b.flush()
    assert daily.startswith("☀️")


def test_an_idle_spell_starting_with_the_day_waits_its_quarter_hour(tmp_path):
    bench = Bench(tmp_path)
    bench.notifier.tick("idle")  # the first start, in working hours, nothing to do: the status, no 💤 yet
    [daily] = bench.flush()
    assert daily.startswith("☀️") and "What the agent can take" not in daily
    bench.clock.advance(minutes=10)
    bench.notifier.tick("idle")
    assert bench.flush() == []
    bench.clock.advance(minutes=6)
    bench.notifier.tick("idle")
    [idle] = bench.flush()
    assert idle.startswith("💤 Nothing to take") and "What the agent can take" in idle


def test_the_morning_status_held_by_slack_offers_no_work(b):
    b.clock.t = datetime(2026, 10, 20, 17, 0, tzinfo=UTC)  # idle since 20:00
    b.notifier.tick("idle")
    b.slack.answers += [(503, "", {})] * 20
    b.clock.t = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)  # 09:00: the status and the 💤 are queued, Slack is down
    b.notifier.tick("idle")
    assert b.flush() == []
    b.notifier.tick("working")  # a ticket is taken before Slack is back
    b.slack.answers.clear()
    b.clock.advance(minutes=10)
    out = b.flush()
    assert [x[:2] for x in out] == ["☀️"]  # the status still goes: it says what was so at 09:00, no offer in it


def test_an_idle_notice_slack_never_took_is_told_once_slack_is_back(b):
    b.notifier.tick("idle")
    b.slack.answers += [(503, "", {})] * 1000  # Slack down past routine_expire (2 h)
    for _ in range(20):
        b.clock.advance(minutes=10)
        b.notifier.tick("idle")
        b.flush()
    assert b.events_named("slack_dropped")[0]["reason"] == "expired"
    b.slack.answers.clear()
    b.clock.advance(minutes=10)
    b.notifier.tick("idle")
    b.clock.advance(minutes=5)  # the sender's backoff after the 503s runs out
    out = [x for x in b.flush() if "💤" in x]
    assert len(out) == 1 and "What the agent can take" in out[0]
    for _ in range(4):
        b.clock.advance(hours=1)
        b.notifier.tick("idle")
    assert [x for x in b.flush() if "💤" in x] == []


def test_an_idle_notice_held_past_the_working_day_is_told_the_next_morning(b):
    b.clock.t = datetime(2026, 10, 20, 15, 30, tzinfo=UTC)  # 18:30 in Kyiv
    b.notifier.tick("idle")
    b.clock.t = datetime(2026, 10, 20, 15, 50, tzinfo=UTC)
    b.slack.answers += [(503, "", {})] * 3
    b.notifier.tick("idle")
    b.flush()
    b.clock.t = datetime(2026, 10, 20, 16, 30, tzinfo=UTC)  # 19:30: Slack back, the day over: dropped
    b.notifier.tick("idle")
    assert [x for x in b.flush() if "💤" in x] == []
    assert b.events_named("slack_dropped")[-1]["reason"] == "the working day ended before it went out"
    b.clock.t = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)  # 09:00, the same spell: one 💤 after the status
    b.notifier.tick("idle")
    out = b.flush()
    b.clock.advance(minutes=10)  # past the sender's backoff after the last 503
    out += b.flush()
    b.clock.advance(hours=3)
    b.notifier.tick("idle")
    out = [x for x in out + b.flush() if "💤" in x]
    assert len(out) == 1 and "What the agent can take" in out[0]


def test_idle_notices_an_older_runner_queued_are_not_posted(b):
    b.notifier.tick("idle")
    b.clock.advance(minutes=16)
    live = {"meta": "idle_since", "value": b.notifier.outbox.meta("idle_since"), "hours": True}
    since = float(live["value"])
    with b.store.transaction():  # what the old runner left in runner.db: its first 💤 and a repeat, unsent
        b.notifier.put(f"idle:{since:.0f}", "idle", notify.ROUTINE, "💤 Nothing to take: the queue is empty.",
                       live_while=live)
        b.notifier.put(f"idle:{since:.0f}:1", "idle", notify.ROUTINE, "💤 Still idle, 2 h now", live_while=live)
        b.notifier.put("started:1", "runner_started", notify.RUNNER, "🟢 Arceus runner started (live mode).")
        b.notifier.outbox.set_meta("idle_told", str(since))
    b.notifier = b.new_notifier()  # the new runner starts on the same runner.db
    b.notifier.tick("idle")
    out = b.flush()
    assert [x for x in out if "Still idle" in x or "runner started" in x] == []
    [idle] = [x for x in out if "💤" in x]  # the spell told once, the new way
    assert "What the agent can take" in idle
    b.clock.advance(hours=3)
    b.notifier.tick("idle")
    assert b.flush() == []


def test_a_waiting_idle_notice_is_not_posted_once_work_came(b):
    b.notifier.tick("idle")
    b.clock.advance(minutes=16)
    b.notifier.tick("idle")  # the 💤 is queued; Slack has not taken it yet
    b.notifier.tick("working")  # then a ticket is taken
    b.clock.advance(minutes=5)
    assert [x for x in b.flush() if "💤" in x] == []  # Slack answers again: nothing false goes out
    [dropped] = b.store.conn.execute("SELECT last_error FROM slack_outbox WHERE kind = 'idle'").fetchall()
    assert dropped[0] == "the idle spell ended before it went out"


def test_the_sender_checks_the_idle_spell_itself(b):
    b.notifier.tick("idle")
    b.clock.advance(minutes=16)
    b.notifier.tick("idle")
    b.notifier.outbox.set_meta("idle_since", "1")  # another spell by the time the sender gets to it
    assert [x for x in b.flush() if "💤" in x] == []
    last = b.events_named("slack_dropped")[-1]
    assert (last["kind"], last["reason"]) == ("idle", "the idle spell ended before it went out")


def test_idle_reminder_offers_the_code_review_when_it_is_on(tmp_path):
    bench = Bench(tmp_path, review_agent=True)
    [review] = [line for line in bench.notifier.can_do() if "review" in line.lower() and "/review" in line]
    assert review.startswith("`/review` (or `@agent-runner review`) in a comment on a ticket in Code Review")
    assert "without changing the code" in review and "does not approve" in review  # the example has no approver
    # With MRs waiting on people, the reminder says the agent can take their review.
    bench.task(state="code_review", mr_urls=json.dumps(["https://gl.example/acme/mr/1"]))
    bench.notifier.tick("working")
    bench.notifier.tick("idle")
    bench.clock.advance(minutes=20)
    bench.notifier.tick("idle")
    [idle] = [x for x in bench.flush() if "💤" in x]
    assert "1 task(s) wait in code review with people. `/review` on their ticket hands the review to the agent." in idle


def test_the_daily_status_goes_once_a_working_day(tmp_path):
    bench = Bench(tmp_path)
    bench.task(state="needs_input", question_id="q-1", question_at_ms=int((NOW - timedelta(hours=3)).timestamp() * 1000))
    bench.notifier.tick("waiting")
    bench.notifier.tick("waiting")
    [daily] = [x for x in bench.flush() if x.startswith("☀️")]
    assert "Waiting for an answer:\n• *<https://yt.example/issue/ACME-1|ACME-1>* Fix login page — asked 3 h ago" in daily
    bench.clock.t = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)  # Saturday
    bench.notifier.tick("waiting")
    assert [x for x in bench.flush() if x.startswith("☀️")] == []


def test_the_daily_status_does_not_call_an_unread_queue_empty(tmp_path):
    bench = Bench(tmp_path)
    bench.notifier.tick("unknown")  # the tracker does not answer
    [daily] = [x for x in bench.flush() if x.startswith("☀️")]
    assert "Queue: not known right now" in daily and "as runner.db last knew it" in daily.lower()
    assert "empty" not in daily and "Nothing in work and nothing queued" not in daily


def test_the_daily_status_with_a_task_running_and_the_tracker_down(lw):
    w = lw
    w.tick()
    drain(w)
    take(w)
    drain(w)
    w.yt.fail["/api/issues"] = 503  # the tracker stops answering its queue search
    w.now += timedelta(days=1)  # Wednesday morning: the next tick is the day's first
    w.tick()
    assert w.runner.health[f"service.{w.runner.tracker.client.name}"] != "ok"
    assert w.runner.situation() == "working"
    [daily] = [x for x in drain(w) if x.startswith("☀️")]
    assert "In work:\n• *<" in daily and "ACME-1" in daily  # what runs is still told
    assert "Queue: not known right now" in daily and "as runner.db last knew it" in daily.lower()
    assert "Queue: empty" not in daily


# -- health and the runner itself -------------------------------------------------------------------------------


def test_health_fail_at_once_service_after_ten_minutes_and_back_once(b):
    b.notifier.health(Check("disk_free", "fail", "4% free"))
    b.notifier.health(Check("disk_free", "fail", "4% free"))
    b.notifier.health(Check("slack.webhook", "warn", "Slack refuses"))  # not news for Slack
    b.notifier.health(Check("service.youtrack", "warn", "HTTP 502"))
    b.clock.advance(minutes=9)
    b.notifier.health(Check("service.youtrack", "warn", "HTTP 502"))
    out = b.flush()
    assert len(out) == 1 and out[0].startswith("🚨 disk_free failed: 4% free")
    b.clock.advance(minutes=2)
    b.notifier.health(Check("service.youtrack", "warn", "HTTP 502"))
    b.notifier.health(Check("service.youtrack", "warn", "HTTP 502"))
    b.notifier.health(Check("disk_free", "ok", "30% free"))
    b.notifier.health(Check("service.youtrack", "ok", "reachable"))
    out = b.flush()
    assert [x.split(":")[0] for x in out] == ["⚠️ youtrack has not answered for 11 min", "✅ disk_free is back to normal",
                                               "✅ service.youtrack is back to normal"]


def test_starts_and_stops_are_not_told(b):
    b.notifier.tick("idle")
    b.flush()
    for _ in range(3):
        b.events.emit("runner_started", mode="live")
        b.clock.advance(hours=1)
        b.events.emit("runner_stopped", reason="signal")
        b.clock.advance(hours=1)
    assert [x for x in b.flush() if "runner" in x] == []
    assert len(b.events_named("runner_started")) == 3  # still in events.jsonl


def test_a_start_or_stop_queued_before_is_not_posted(b):
    b.notifier.put("stop", "runner_stopped", notify.RUNNER, "⚪️ stopped")
    b.notifier = b.new_notifier()
    assert b.flush() == []


def test_quick_restarts_are_counted_and_downtime_is_told(tmp_path):
    b = Bench(tmp_path, runner_lifecycle=True)
    b.notifier.tick("working")  # heartbeat
    b.flush()
    b.events.emit("runner_started", mode="live")
    b.clock.advance(minutes=1)
    b.events.emit("runner_stopped", reason="exit")
    b.clock.advance(minutes=1)
    b.events.emit("runner_started", mode="live")
    out = b.flush()
    assert len(out) == 1 and out[0].startswith("🟢 Arceus runner started (live mode).")
    b.notifier.tick("idle")
    b.clock.advance(hours=2)  # dead, no ticks
    b.events.emit("runner_started", mode="live")
    [again] = b.flush()
    assert "It was down since" in again and "(2 h)" in again and "restarted 1 more time since" in again


def test_a_failing_tick_is_told_once_an_hour(b):
    for _ in range(3):
        b.events.emit("tick_failed", error="KeyError: 'x'")
    b.clock.advance(hours=1)
    b.events.emit("tick_failed", error="KeyError: 'x'")
    assert len(b.flush()) == 2


# -- in the loop ---------------------------------------------------------------------------------------------------


@pytest.fixture
def lw(tmp_path, secrets):
    world = World(tmp_path, secrets, settings={"slack": SLACK})
    world.slack = FakeSlack()
    world.runner.notifier = n = notify.build(world.cfg, world.store, world.events, HOOK, world.runner.tracker.issue_url,
                                             now=lambda: world.now, transport=httpx.MockTransport(world.slack))
    n.sender.clock = lambda: world.now.timestamp() + world.clock.t  # every tick is 10 minutes later
    yield world
    world.events.close()
    world.store.close()


def drain(w) -> list[str]:
    before = len(w.slack.posts)
    while w.runner.notifier.sender.send_one() or w.runner.notifier.sender.outbox().pending():
        w.clock.advance(notify.MIN_GAP + 0.01)
    return w.slack.posts[before:]


def test_the_loop_tells_start_question_answer_and_which_situation_it_is_in(lw):
    w = lw
    w.tick()  # seeds: nothing of the past
    drain(w)  # the day's status
    assert w.runner.situation() == "idle"
    take(w)
    assert w.runner.situation() == "working"
    [started] = drain(w)
    assert "▶️ Started" in started and "ACME-1" in started
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="needs_input", attempt_live=False,
        question={"id": "q-1", "text": "Postgres or SQLite?", "options": ["Postgres", "SQLite"]}))
    w.tick()
    assert w.runner.situation() == "waiting"  # not idle: a person owes an answer
    [q] = drain(w)
    assert "❓ <@U0JDOE>" in q and "Postgres or SQLite?" in q and "1. Postgres" in q
    w.yt.comment("2-1", "a.smith", "2")
    w.tick()
    [back] = drain(w)
    assert "Got an answer" in back
    # A Slack that is down is a warning: it keeps no new work out.
    sender = w.runner.notifier.sender
    w.slack.answers += [(503, "", {})] * 3
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(state="failed", attempt_live=False, state_reason="boom"))
    w.tick()
    for _ in range(3):
        sender.paused_until = 0
        w.clock.advance(notify.MIN_GAP + 0.01)
        assert sender.send_one()
    w.runner.poll_local()
    assert w.runner.health["slack.webhook"] == "warn" and w.runner.can_take()
    assert w.store.task("ACME-1").state == "failed" and sender.outbox().pending() == 1


def test_a_crash_between_the_question_and_its_event_still_quotes_the_question(lw, monkeypatch):
    w = lw
    w.tick()
    take(w)
    drain(w)
    real = w.events.emit

    def dies_at_the_event(event, **fields):
        if event == "task_needs_input":
            raise SystemExit("killed")  # the process ends right after the state was saved
        return real(event, **fields)

    monkeypatch.setattr(w.events, "emit", dies_at_the_event)
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="needs_input", attempt_live=False,
        question={"id": "q-1", "text": "Postgres or SQLite?", "options": ["Postgres", "SQLite"]}))
    with pytest.raises(SystemExit):
        w.tick()
    monkeypatch.setattr(w.events, "emit", real)
    assert w.store.task("ACME-1").state == "needs_input"
    # The next runner: a new notifier on the same runner.db.
    w.runner.notifier = notify.build(w.cfg, w.store, w.events, HOOK, w.runner.tracker.issue_url,
                                     now=lambda: w.now, transport=httpx.MockTransport(w.slack))
    w.runner.notifier.sender.clock = lambda: w.now.timestamp() + w.clock.t
    w.tick()
    [q] = [x for x in drain(w) if x.startswith("❓")]
    assert "> Postgres or SQLite?" in q and "1. Postgres\n2. SQLite" in q


def test_an_unreadable_queue_is_not_idle(lw):
    w = lw
    w.runner.tick()
    w.runner.health[f"service.{w.runner.tracker.client.name}"] = "warn"
    assert w.runner.situation() == "unknown"
