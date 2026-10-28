from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from arceus_runner import loop
from arceus_runner.db import AlreadyRunning, InstanceLock, ReviewStore as Store
from arceus_runner.events import EventLog
from arceus_runner.http import AUTH_RETRY_SECONDS

from .conftest import FakeClock, providers, read_events
from .fakes import FakeGitLab, FakeYouTrack
from .test_cleanup import FakeDocker

# A Tuesday at 10:00 in Kyiv: working hours, so the poll interval is 2 minutes.
WORKING = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def own_home(tmp_path, monkeypatch):
    # run() writes the bot's git credentials into ~: never into the real home of whoever runs tests.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    # Health must not depend on what is installed on the machine running the tests.
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    # That makes Docker look installed too: run() must not then send its cleanup pass to the host's Docker.
    monkeypatch.setattr(loop, "Docker", FakeDocker)


class World:
    def __init__(self, cfg, secrets):
        self.cfg, self.secrets = cfg, secrets
        self.clock = FakeClock()
        self.yt = FakeYouTrack()
        self.gl = FakeGitLab()
        self.store = Store(cfg.paths.db)
        self.events = EventLog(cfg.paths.events, cfg.project, redact=secrets.values())
        self.runner = self.make_runner()

    def make_runner(self) -> loop.Runner:
        tracker, host = providers(self.cfg, self.secrets, self.yt, self.gl, self.clock)
        return loop.Runner(self.cfg, self.store, self.events, tracker, host, now=lambda: WORKING, mono=self.clock)

    def restart(self) -> None:
        self.store.close()
        self.store = Store(self.cfg.paths.db)
        self.runner = self.make_runner()

    def events_named(self, *names) -> list[dict]:
        return [e for e in read_events(self.cfg.paths.events) if e["event"] in names]


@pytest.fixture
def world(cfg, secrets):
    w = World(cfg, secrets)
    yield w
    w.events.close()
    w.store.close()


def test_observe_sends_only_get_requests(world):
    world.yt.add_issue(1, ("a1", "j.doe", "+agent"))
    world.yt.add_issue(2, ("b1", "mallory", "+agent"))
    world.runner.tick()
    methods = {r.method for r in world.yt.requests + world.gl.requests}
    assert methods == {"GET"}
    queued = world.events_named("task_queued")
    rejected = world.events_named("trigger_rejected")
    assert [(e["task_id"], e["author"], e["action"]) for e in queued] == [("ACME-1", "j.doe", "none: observe mode")]
    assert [(e["task_id"], e["author"]) for e in rejected] == [("ACME-2", "mallory")]


def test_rejection_is_announced_once_even_after_restart(world):
    world.yt.add_issue(2, ("b1", "mallory", "+agent"))
    world.runner.tick()
    world.clock.advance(600)
    world.runner.tick()
    world.restart()
    world.runner.tick()
    assert len(world.events_named("trigger_rejected")) == 1


def test_retagging_is_a_new_decision(world):
    db_id = world.yt.add_issue(2, ("b1", "mallory", "+agent"))
    world.runner.tick()
    world.yt.activities[db_id] += [
        {"id": "b2", "timestamp": 5, "author": {"login": "mallory"}, "added": [], "removed": [{"name": "agent"}]},
        {"id": "b3", "timestamp": 6, "author": {"login": "j.doe"}, "added": [{"name": "agent"}], "removed": []},
    ]
    world.clock.advance(600)
    world.runner.tick()
    assert [e["event"] for e in world.events_named("trigger_rejected", "task_queued")] == [
        "trigger_rejected", "task_queued"]


def test_crash_between_decision_and_event_does_not_lose_the_event(world, monkeypatch):
    world.yt.add_issue(1, ("a1", "j.doe", "+agent"))
    real_emit = world.events.emit

    def crash(event, **fields):
        if event == "task_queued":
            raise OSError("disk full")
        return real_emit(event, **fields)

    monkeypatch.setattr(world.events, "emit", crash)
    with pytest.raises(OSError):
        world.runner.tick()
    assert world.store.trigger_decision("ACME-1", "a1").announced is False

    monkeypatch.setattr(world.events, "emit", real_emit)
    world.restart()
    world.runner.tick()
    assert len(world.events_named("task_queued")) == 1
    assert world.store.trigger_decision("ACME-1", "a1").announced is True


def test_one_broken_issue_does_not_stop_the_others(world):
    db1 = world.yt.add_issue(1, ("a1", "j.doe", "+agent"))
    world.yt.add_issue(2, ("b1", "a.smith", "+agent"))
    del world.yt.activities[db1]  # history of issue 1 answers 404
    world.runner.tick()
    assert [e["task_id"] for e in world.events_named("issue_skipped")] == ["ACME-1"]
    assert [e["task_id"] for e in world.events_named("task_queued")] == ["ACME-2"]
    assert world.runner.tracker.client.available()
    assert world.runner.health["service.youtrack"] == "ok"


def test_gitlab_outage_does_not_delay_youtrack(world):
    world.gl.status = 503
    world.gl.retry_after = "900"
    world.runner.tick()
    assert world.runner.health["service.gitlab"] == "warn"
    yt_calls = len(world.yt.requests)
    gl_calls = len(world.gl.requests)

    world.clock.advance(120)  # one regular interval
    world.runner.tick()
    assert len(world.yt.requests) > yt_calls  # YouTrack polled on time
    assert len(world.gl.requests) == gl_calls  # GitLab still waits out Retry-After

    world.gl.status = None
    world.clock.advance(900)
    world.runner.tick()
    assert world.runner.health["service.gitlab"] == "ok"


def test_youtrack_auth_failure_waits_ten_minutes_and_marks_health(world):
    world.yt.fail["/api/issues"] = 401
    world.runner.tick()
    assert world.runner.health["service.youtrack"] == "fail"
    calls = len(world.yt.requests)
    for _ in range(4):
        world.clock.advance(120)
        world.runner.tick()
    assert len(world.yt.requests) == calls  # 480 s later: still nothing sent
    del world.yt.fail["/api/issues"]
    world.clock.advance(AUTH_RETRY_SECONDS)
    world.runner.tick()
    assert world.runner.health["service.youtrack"] == "ok"


def test_slow_queue_answer_skips_one_poll_of_that_service_only(world):
    def slow_yt(request, fake=world.yt):
        if request.url.path == "/api/issues":
            world.clock.advance(15)
        return fake(request)

    world.runner.tracker.client._client._transport = httpx.MockTransport(slow_yt)
    world.yt.add_issue(1, ("a1", "j.doe", "+agent"))
    start = world.clock()
    world.runner.tick()
    assert world.runner.next_due["tracker"] - start >= 240 - 1  # 2 intervals: slow
    assert world.runner.next_due["code_host"] - start <= 120 + 16


def test_tokens_never_reach_the_event_log(world):
    world.yt.fail["/api/issues"] = 400  # the fake echoes the Authorization header into the body
    world.runner.tick()
    text = world.cfg.paths.events.read_text()
    assert "request_failed" in text and "<redacted>" in text
    assert world.secrets.tracker not in text and world.secrets.code_host not in text


def test_second_instance_is_refused(cfg):
    a = InstanceLock(cfg.paths.db.with_suffix(".lock"))
    a.acquire()
    b = InstanceLock(cfg.paths.db.with_suffix(".lock"))
    with pytest.raises(AlreadyRunning, match="pid"):
        b.acquire()
    a.release()
    b.acquire()
    b.release()


def test_run_once_releases_everything(cfg, secrets, monkeypatch):
    fake_yt, fake_gl = FakeYouTrack(), FakeGitLab()
    real_yt, real_gl = loop.build_tracker, loop.build_code_host
    monkeypatch.setattr(loop, "build_tracker", lambda c, s: real_yt(c, s, transport=httpx.MockTransport(fake_yt)))
    monkeypatch.setattr(loop, "build_code_host", lambda c, s: real_gl(c, s, transport=httpx.MockTransport(fake_gl)))
    assert loop.run(cfg, secrets, once=True) == 0
    # The lock is free again and the log is closed with runner_stopped.
    lock = InstanceLock(cfg.paths.db.with_suffix(".lock"))
    lock.acquire()
    lock.release()
    names = [e["event"] for e in read_events(cfg.paths.events)]
    assert names[0] == "runner_started" and names[-1] == "runner_stopped"


def test_failed_start_releases_the_lock(cfg, secrets, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("cannot open db")

    monkeypatch.setattr(loop, "ReviewStore", broken)
    with pytest.raises(RuntimeError):
        loop.run(cfg, secrets, once=True)
    lock = InstanceLock(cfg.paths.db.with_suffix(".lock"))
    lock.acquire()
    lock.release()


def test_run_writes_git_credentials_for_the_gitlab_host_only(cfg, secrets, monkeypatch, own_home):
    (own_home / ".git-credentials").write_text("https://me:mine@github.com\nhttps://old:stale@gitlab.example.com\n")
    fake_yt, fake_gl = FakeYouTrack(), FakeGitLab()
    real_yt, real_gl = loop.build_tracker, loop.build_code_host
    monkeypatch.setattr(loop, "build_tracker", lambda c, s: real_yt(c, s, transport=httpx.MockTransport(fake_yt)))
    monkeypatch.setattr(loop, "build_code_host", lambda c, s: real_gl(c, s, transport=httpx.MockTransport(fake_gl)))
    loop.run(cfg, secrets, once=True)
    store = own_home / ".git-credentials"
    assert store.read_text().splitlines() == [
        "https://me:mine@github.com", f"https://acme-agent-bot:{secrets.code_host}@gitlab.example.com"]
    assert oct(store.stat().st_mode & 0o777) == "0o600"
    import subprocess
    helper = subprocess.run(["git", "config", "--global", "--get", "credential.https://gitlab.example.com.helper"],
                            env={"HOME": str(own_home), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
                            capture_output=True, text=True).stdout.strip()
    assert helper == "store"
    assert secrets.code_host not in cfg.paths.events.read_text()
