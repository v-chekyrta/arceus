"""Live delivery against a fake Bulava CLI, fake trackers and real git with a local origin."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from arceus_runner import loop
from arceus_runner.config import Config
from arceus_runner.db import ReviewStore as TaskStore
from arceus_runner.engine import Engine
from arceus_runner.events import EventLog
from arceus_runner.gitops import Git, GitAuth

from .conftest import GL_TOKEN, FakeClock, example_raw, providers, read_events
from .fakes import FakeGitLab, FakeScreen, FakeYouTrack

FAKE_ENGINE = Path(__file__).with_name("fake_engine.py")
NOW = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    # The fake engine sits in a temp folder this user owns; on a server /opt/bulava is root's.
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])


class World:
    def __init__(self, tmp: Path, secrets, engine_dir: Path | None = None, engine_env: dict | None = None,
                 claude_checks: dict | None = None):
        self.tmp = tmp
        self.engine_env = engine_env or {}
        if engine_dir is None:
            engine_dir = tmp / "engine"
            (engine_dir / "bin").mkdir(parents=True)
            for script, cmd in (("dispatch.sh", "dispatch"), ("worker-send.sh", "worker-send"),
                                ("night-shift.sh", "night-shift"), ("claude-usage.sh", "claude-usage"),
                                ("codex-usage.sh", "codex-usage")):
                f = engine_dir / "bin" / script
                f.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_ENGINE}" {cmd} "$@"\n')
                f.chmod(0o755)
            # A review gate that runs the independent checks, as the probe looks for it.
            (engine_dir / "hooks").mkdir()
            (engine_dir / "hooks" / "review-gate.sh").write_text(
                '#!/bin/bash\nif [ -n "${IDIR_SCOPE:-}" ] && claude_checks_enabled; then :; fi\n')
        (tmp / "bulava.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-test\n")
        (tmp / "bulava.env").chmod(0o600)

        # origins: a bare repository with one commit on main for every repository of the group
        self.origins = {}
        for name in ("acme-platform", "acme-theme", "acme-infra"):
            origin = tmp / "origin" / f"{name}.git"
            seed = tmp / "seed" / name
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
            git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
            git(seed, "push", "-q", str(origin), "main")
            self.origins[name] = origin
        self.origin = self.origins["acme-platform"]

        raw = example_raw()
        raw.pop("slack")
        raw["mode"] = "live"
        raw["code_host"]["exclude_repos"] = ["acme-infra"]
        raw["paths"] = {
            "workspace": str(tmp / "workspace"), "bulava": str(engine_dir), "bulava_state": str(tmp / "state"),
            "bulava_env": str(tmp / "bulava.env"), "secrets": str(tmp / "secrets.env"),
            "db": str(tmp / "runner.db"), "events": str(tmp / "events.jsonl"),
        }
        raw["bulava"]["mcp_config"] = None
        raw["bulava"]["claude_checks"].update(claude_checks or {})
        raw["health"].pop("devstack_check", None)
        self.cfg = Config.model_validate(raw)
        self.secrets = secrets
        self.clock = FakeClock()
        self.now = NOW
        self.yt = FakeYouTrack()
        self.gl = FakeGitLab()
        self.screen = FakeScreen()
        for name, origin in self.origins.items():
            self.gl.urls[name] = str(origin)
        self.store = TaskStore(self.cfg.paths.db)
        self.events = EventLog(self.cfg.paths.events, self.cfg.project, redact=secrets.values())
        self.runner = self._runner()

    def _runner(self) -> loop.Runner:
        tracker, host = providers(self.cfg, self.secrets, self.yt, self.gl, self.clock)
        g = Git(GitAuth(self.cfg.code_host.url, "bot", GL_TOKEN), "ACME Agent", "bot@example.com")
        return loop.Runner(self.cfg, self.store, self.events, tracker, host, now=lambda: self.now, mono=self.clock,
                           engine=Engine(self.cfg, extra_env=self.engine_env), git=g, classify=self.screen)

    def tick(self) -> None:
        self.runner.tick()
        self.clock.advance(600)

    def restart(self) -> None:
        self.store.close()
        self.store = TaskStore(self.cfg.paths.db)
        self.runner = self._runner()

    # -- the fake engine's state --
    def engine_state(self) -> dict:
        f = self.cfg.paths.bulava_state / "fake-engine.json"
        return json.loads(f.read_text()) if f.exists() else {"tasks": {}, "calls": [], "next_rc": {}}

    def set_engine(self, fn) -> None:
        s = self.engine_state()
        fn(s)
        self.cfg.paths.bulava_state.mkdir(parents=True, exist_ok=True)
        (self.cfg.paths.bulava_state / "fake-engine.json").write_text(json.dumps(s))

    def calls(self, cmd: str) -> list[dict]:
        return [c for c in self.engine_state()["calls"] if c["cmd"] == cmd]

    @property
    def repo(self) -> Path:
        return self.cfg.paths.workspace / "acme-platform"

    def agent_commits(self, name: str = "feature.txt") -> str:
        (self.repo / name).write_text("work\n")
        git(self.repo, "add", name)
        git(self.repo, "commit", "-q", "-m", f"add {name}")
        return git(self.repo, "rev-parse", "HEAD")

    def settle(self, task_id: str, state: str, review: str = "passed", reviewed: str | None = None, **extra) -> None:
        reviewed = reviewed or git(self.repo, "rev-parse", "HEAD")

        def fn(s):
            t = s["tasks"][task_id]
            t.update(state=state, attempt_live=False, state_reason=extra.pop("state_reason", review), **extra)
            t["attempts"][-1]["ended_at"] = "now"
            # Every repository the engine knows for the task, each at its HEAD now (this one at `reviewed`).
            t["receipt"] = {"review": review, "repos": [{
                "path": r["path"], "branch": t["branch"], "base_sha": r["base_sha"],
                "reviewed_sha": reviewed if Path(r["path"]) == self.repo else git(Path(r["path"]), "rev-parse", "HEAD"),
                "clean": True} for r in t["repos"]]}
        self.set_engine(fn)

    def events_named(self, *names) -> list[dict]:
        return [e for e in read_events(self.cfg.paths.events) if e["event"] in names]

    def bot_comments(self, db_id: str = "2-1") -> list[str]:
        return [c["text"] for c in self.yt.comments.get(db_id, []) if c["author"]["login"] == self.yt.login]

    def remote_branch(self, branch: str, repo: str = "acme-platform") -> str | None:
        out = subprocess.run(["git", "-C", str(self.origins[repo]), "rev-parse", "--verify", "-q",
                              f"refs/heads/{branch}"], capture_output=True, text=True).stdout.strip()
        return out or None


@pytest.fixture
def w(tmp_path, secrets):
    world = World(tmp_path, secrets)
    yield world
    world.events.close()
    world.store.close()


def take(w: World, n: int = 1, author: str = "j.doe", summary: str = "Fix login page") -> str:
    db_id = w.yt.add_issue(n, (f"a{n}", author, "+agent"))
    w.yt.issues[-1]["summary"] = summary
    w.tick()
    return db_id


def test_ticket_to_merge_request(w):
    take(w)
    task = w.store.task("ACME-1")
    # No repository is the main one: the session starts in the workspace folder holding all of them.
    assert (task.state, task.branch, task.repo) == ("dispatched", "agent/ACME-1-fix-login-page", "")
    assert json.loads(task.extra_repos) == ["acme-platform", "acme-theme"]

    [d] = w.calls("dispatch")
    assert d["argv"][:14] == ["--task-id", "ACME-1", "--message-id", "yt-ACME-1", "--source", "youtrack",
                              "--author", "j.doe", "--branch", "agent/ACME-1-fix-login-page", "--base", "main",
                              "--dirty", "fail"]
    assert d["argv"][14] == str(w.cfg.paths.workspace) and d["argv"][15].startswith("ACME-1: Fix login page")
    for name in ("acme-platform", "acme-theme"):
        assert git(w.cfg.paths.workspace / name, "branch", "--show-current") == "agent/ACME-1-fix-login-page"
    # Bulava gets the Claude token and the engine settings, never the runner's own tokens.
    assert d["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"
    assert d["env"]["SUPERVISOR_PROFILE"] == "server" and d["env"]["SUPERVISOR_REPORT_LANGUAGE"] == "English"
    assert d["env"]["SUPERVISOR_HANDSHAKE_WAIT"] == "30"   # a cold Claude takes longer than the engine's 5 s
    assert str(w.cfg.paths.secrets) in d["env"]["SUPERVISOR_SERVER_EXTRA_SECRET_PATHS"]
    assert GL_TOKEN not in json.dumps(w.engine_state()) and w.secrets.tracker not in json.dumps(w.engine_state())
    assert [c["argv"][0] for c in w.calls("night-shift") if c["argv"][0] != "doctor"][:2] == ["trust", "mcp-decide"]
    # The token is not in the clone's config either.
    assert GL_TOKEN not in (w.repo / ".git" / "config").read_text()

    assert w.yt.issue_fields["2-1"]["State"] == {"name": "In Progress"}
    assert w.yt.issue_fields["2-1"]["Assignee"] == {"login": "agent-runner"}
    assert any("[arceus:claim]" in t for t in w.bot_comments())

    w.tick()  # still running: nothing happens
    assert w.store.task("ACME-1").state == "dispatched"

    sha = w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    w.tick()

    assert w.remote_branch("agent/ACME-1-fix-login-page") == sha
    assert len(w.gl.mrs) == 1
    mr = w.gl.mrs[0]
    assert (mr["source_branch"], mr["target_branch"], mr["title"], mr["labels"]) == (
        "agent/ACME-1-fix-login-page", "main", "ACME-1: Fix login page", "agent")
    assert "Screenshots" not in mr["description"] and w.gl.uploads == [] and w.yt.attachments == {}  # no media
    assert w.yt.issue_fields["2-1"]["State"] == {"name": "Code Review"}
    assert any(mr["web_url"] in t for t in w.bot_comments())
    assert w.store.task("ACME-1").state == "code_review"

    w.tick()
    assert len(w.gl.mrs) == 1 and len(w.calls("dispatch")) == 1


def test_head_moved_after_review_pushes_nothing(w):
    take(w)
    reviewed = w.agent_commits("a.txt")
    w.agent_commits("b.txt")  # a commit Codex never saw
    w.settle("ACME-1", "waiting_review", reviewed=reviewed)
    w.tick()
    assert w.remote_branch("agent/ACME-1-fix-login-page") is None
    assert w.gl.mrs == []
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "is not the reviewed one" in task.reason
    assert "agent-failed" in w.yt.issue_tags["2-1"]


def test_commits_below_the_reviewed_range_are_not_published(w):
    """An engine that measured from the start of the last attempt: earlier attempts' commits were never reviewed."""
    take(w)
    w.agent_commits("a.txt")  # attempt 1, ended with a question: no review
    attempt_base = git(w.repo, "rev-parse", "HEAD")
    w.agent_commits("b.txt")
    w.settle("ACME-1", "waiting_review")

    def from_attempt(s):
        for r in s["tasks"]["ACME-1"]["receipt"]["repos"]:
            if Path(r["path"]) == w.repo:
                r["base_sha"] = attempt_base
    w.set_engine(from_attempt)
    w.tick()
    assert w.remote_branch("agent/ACME-1-fix-login-page") is None and w.gl.mrs == []
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "below the reviewed range" in task.reason
    assert any("add a.txt" in t and "never reviewed" in t for t in w.bot_comments())


def test_unchanged_branch_is_not_a_merge_request(w):
    take(w)
    w.settle("ACME-1", "waiting_review")  # reviewed == base
    w.tick()
    assert w.gl.mrs == [] and w.store.task("ACME-1").reason == "no changes"


def test_debt_is_not_published(w):
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "failed", review="debt", state_reason="debt",
             outcome={"summary": "Codex: tests are missing"})
    w.tick()
    assert w.remote_branch("agent/ACME-1-fix-login-page") is None and w.gl.mrs == []
    task = w.store.task("ACME-1")
    assert (task.state, task.reason) == ("failed", "the work did not pass review")
    [note] = [t for t in w.bot_comments() if "did not pass review" in t]
    assert "tests are missing" in note       # what the review found is kept, who said it is not
    assert not re.search(r"Codex|Claude|Bulava", note)


def test_question_goes_to_the_ticket_and_the_answer_back(w):
    take(w)
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="needs_input", attempt_live=False,
        question={"id": "q-1", "text": "Postgres or SQLite?", "options": ["Postgres", "SQLite"]}))
    w.tick()
    task = w.store.task("ACME-1")
    assert (task.state, task.question_id) == ("needs_input", "q-1")
    [q] = [t for t in w.bot_comments() if "[arceus:question:q-1]" in t]
    assert "@j.doe" in q and "1. Postgres" in q and "2. SQLite" in q
    assert "agent-needs-input" in w.yt.issue_tags["2-1"]

    w.yt.comment("2-1", "mallory", "use mongo")  # not on the allowlist: ignored
    w.tick()
    assert w.calls("worker-send") == []

    w.yt.comment("2-1", "a.smith", "2")
    w.tick()
    [send] = w.calls("worker-send")
    assert "--answers" in send["argv"] and send["argv"][send["argv"].index("--answers") + 1] == "q-1"
    assert send["argv"][-1] == "2" and "a.smith" in send["argv"]
    assert w.store.task("ACME-1").state == "dispatched"
    assert "agent-needs-input" not in w.yt.issue_tags["2-1"]


def test_only_one_task_runs_at_a_time(w):
    w.yt.add_issue(1, ("a1", "j.doe", "+agent"))
    w.yt.add_issue(2, ("b1", "j.doe", "+agent"))
    w.tick()
    w.tick()
    assert [c["argv"][1] for c in w.calls("dispatch")] == ["ACME-1"]
    w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    w.tick()
    w.tick()
    assert [c["argv"][1] for c in w.calls("dispatch")] == ["ACME-1", "ACME-2"]


def test_dirty_tree_refusal_fails_the_task(w):
    w.set_engine(lambda s: s["next_rc"].update({"dispatch": 77}))
    take(w)
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "uncommitted changes" in task.reason


def test_session_lost_after_reboot_is_sent_on(w):
    take(w)
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(attempt_live=False))
    w.tick()
    [send] = w.calls("worker-send")
    assert "resume-ACME-1-1" in send["argv"] and "--answers" not in send["argv"]
    assert w.store.task("ACME-1").state == "dispatched"


def test_hung_attempt_fails_after_the_timeout(w):
    take(w)
    w.now = datetime.now(UTC) + timedelta(hours=9)
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and task.reason.startswith("the agent hung")
    assert [c["argv"][0] for c in w.calls("night-shift")][-1] == "stop"


def test_crash_during_publishing_does_not_duplicate(w):
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    # The MR comment fails after the push and the MR: the runner stops in "publishing".
    w.yt.fail_once[("POST", "/api/issues/2-1/comments")] = 503
    w.tick()
    assert w.store.task("ACME-1").state == "publishing" and len(w.gl.mrs) == 1
    w.restart()
    w.clock.advance(3600)
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"
    assert len(w.gl.mrs) == 1
    assert len([t for t in w.bot_comments() if "[arceus:mr:" in t]) == 1


def test_comment_posted_before_a_crash_is_not_posted_again(w):
    take(w)
    task = w.store.task("ACME-1")
    d = w.runner.delivery
    # Simulate: intent recorded, comment posted, crash before "done".
    w.store.action_intent("ACME-1:comment:x", "ACME-1", "comment", json.dumps({"kind": "x", "text": "hello"}))
    w.yt.comment("2-1", w.yt.login, "hello\n\n[arceus:x]")
    d.comment_once(task, "x", "hello")
    assert len([t for t in w.bot_comments() if "[arceus:x]" in t]) == 1


def test_failure_notice_survives_youtrack_outage(w):
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "failed", review="debt", state_reason="debt")
    w.yt.fail_once[("POST", "/api/issues/2-1/comments")] = 503
    w.tick()
    assert w.store.task("ACME-1").state == "failed"
    assert not [t for t in w.bot_comments() if "could not take this ticket" in t]
    w.clock.advance(3600)
    w.tick()
    assert len([t for t in w.bot_comments() if "could not take this ticket" in t]) == 1
    assert "agent-failed" in w.yt.issue_tags["2-1"]


def test_untagged_or_moved_ticket_is_not_taken(w):
    db_id = w.yt.add_issue(1, ("a1", "j.doe", "+agent"))
    w.runner.poll_tracker()  # decision recorded while it is queued
    w.yt.issue_fields[db_id] = {"State": {"name": "In Progress"}}  # someone took it by hand
    w.runner.delivery.maybe_start(True)
    assert w.calls("dispatch") == [] and w.store.task("ACME-1") is None
    assert w.events_named("task_not_taken")


def test_live_mode_refused_when_engine_lacks_the_contract(w, tmp_path):
    from arceus_runner.bulava import probe
    p = probe(w.cfg.paths.bulava)  # the fake scripts advertise nothing
    with pytest.raises(loop.LiveModeUnavailable, match="dispatch.sh --task-id"):
        loop.require_mode_ready(w.cfg, p)


# -- crash and fault windows ---------------------------------------------------------------------

def test_crash_right_after_failing_still_notifies(w, monkeypatch):
    take(w)
    w.settle("ACME-1", "failed", review="debt", state_reason="debt")
    real = w.runner.delivery.tracker.add_comment

    def die(*a, **k):
        raise RuntimeError("killed")  # not a service error: the process dies here

    monkeypatch.setattr(w.runner.delivery.tracker, "add_comment", die)
    with pytest.raises(RuntimeError):
        w.tick()
    assert w.store.task("ACME-1").state == "failed"
    monkeypatch.setattr(w.runner.delivery.tracker, "add_comment", real)
    w.restart()
    w.clock.advance(3600)
    w.tick()
    assert len([t for t in w.bot_comments() if "could not take this ticket" in t]) == 1
    assert "agent-failed" in w.yt.issue_tags["2-1"]


def test_needs_input_tag_comes_off_even_if_youtrack_fails_then(w):
    take(w)
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="needs_input", attempt_live=False, question={"id": "q-1", "text": "?", "options": []}))
    w.tick()
    w.yt.comment("2-1", "j.doe", "yes")
    w.yt.fail_once[("DELETE", "/api/issues/2-1/tags/t-agent-needs-input")] = 503
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    assert "agent-needs-input" in w.yt.issue_tags["2-1"]
    w.tick()
    assert "agent-needs-input" not in w.yt.issue_tags["2-1"]


def test_answer_in_the_same_millisecond_counts(w):
    take(w)
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="needs_input", attempt_live=False, question={"id": "q-1", "text": "?", "options": []}))
    w.tick()
    q_at = w.store.task("ACME-1").question_at_ms
    w.yt.comments["2-1"].append({"id": "c-same", "text": "yes", "created": q_at, "author": {"login": "j.doe"}})
    w.tick()
    assert len(w.calls("worker-send")) == 1


def test_push_taken_by_the_server_before_an_error_still_gets_its_mr(w, monkeypatch):
    from arceus_runner.gitops import GitError
    take(w)
    sha = w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    git_ops = w.runner.delivery.git
    real_push = git_ops.push_sha

    def push_then_break(repo, s, branch):
        real_push(repo, s, branch)
        raise GitError("git push: connection reset")

    monkeypatch.setattr(git_ops, "push_sha", push_then_break)
    w.tick()
    assert w.remote_branch("agent/ACME-1-fix-login-page") == sha
    assert len(w.gl.mrs) == 1 and w.store.task("ACME-1").state == "code_review"


def test_sibling_missing_from_the_group_means_no_push_at_all(w):
    take(w)
    w.agent_commits()  # the primary repository has a real change, and still nothing may go out
    other = w.cfg.paths.workspace / "acme-extra"
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
    base = git(other, "rev-parse", "HEAD")
    git(other, "checkout", "-q", "-b", "agent/ACME-1-fix-login-page")
    git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "work")
    extra = {"path": str(other), "branch": "agent/ACME-1-fix-login-page", "base_sha": base,
             "reviewed_sha": git(other, "rev-parse", "HEAD"), "clean": True}
    w.settle("ACME-1", "waiting_review")
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"]["repos"].append(extra))
    w.tick()
    assert w.remote_branch("agent/ACME-1-fix-login-page") is None and w.gl.mrs == []
    # A repository the task was never given is refused before anything is pushed, even the real change.
    assert "nothing was pushed" in w.store.task("ACME-1").reason
    assert any("acme-extra: not one of this task's repositories" in c for c in w.bot_comments())


def test_mr_closed_by_a_person_after_a_crash_is_not_recreated(w):
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    w.gl.mrs.append({"project_id": 0, "iid": 1, "state": "closed", "source_branch": "agent/ACME-1-fix-login-page",
                     "web_url": "https://gl/clients/acme/mr/1"})
    w.tick()
    assert len(w.gl.mrs) == 1
    assert not [r for r in w.gl.requests if r.method == "POST" and r.url.path.endswith("/merge_requests")]
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "closed without merge" in task.reason
    assert w.remote_branch("agent/ACME-1-fix-login-page") is None and w.store.task_mrs("ACME-1") == []


def test_engine_hiccup_is_not_an_unknown_task(w):
    take(w)
    w.set_engine(lambda s: s["next_rc"].update({"night-shift task": 1}))  # exit 1 without «немає»
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    assert w.events_named("reconcile_failed")


def test_done_is_a_result_not_a_failure(w):
    take(w)
    w.settle("ACME-1", "done", outcome={"summary": "It already works, no test needed"})
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "done"
    assert "agent-failed" not in w.yt.issue_tags["2-1"]
    assert any("It already works" in t for t in w.bot_comments())


def test_example_token_never_reaches_the_engine(w):
    (w.tmp / "bulava.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-replace-me\nCLAUDE_CODE_OAUTH_TOKEN_ISSUED_AT=\n")
    take(w)
    [d] = w.calls("dispatch")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in d["env"] and "CLAUDE_CODE_OAUTH_TOKEN_ISSUED_AT" not in d["env"]


def test_no_bulava_env_means_claude_uses_its_own_sign_in(w):
    (w.tmp / "bulava.env").unlink()
    take(w)
    [d] = w.calls("dispatch")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in d["env"] and w.store.task("ACME-1").state == "dispatched"


def test_no_new_work_while_claude_is_signed_out(w):
    w.set_engine(lambda s: s.update(doctor=[{"name": "auth.claude", "status": "fail",
                                             "detail": {"loggedIn": False}}]))
    take(w)
    assert w.calls("dispatch") == [] and w.runner.health["bulava.auth.claude"] == "fail"
    w.set_engine(lambda s: s.update(doctor=[{"name": "auth.claude", "status": "ok", "detail": "signed in"}]))
    w.clock.advance(600)
    w.tick()
    assert len(w.calls("dispatch")) == 1



# -- the whole group, no repository in the ticket ------------------------------------------------

def test_every_repository_of_the_group_is_open_to_the_agent(w):
    w.gl.descriptions["acme-theme"] = "Brand theme:\n colours and  fonts"
    take(w)
    task = w.store.task("ACME-1")
    assert (task.repo, json.loads(task.extra_repos)) == ("", ["acme-platform", "acme-theme"])
    home, theme = w.cfg.paths.workspace, w.cfg.paths.workspace / "acme-theme"
    for repo in (w.repo, theme):
        assert git(repo, "symbolic-ref", "--short", "HEAD") == task.branch  # branch made before dispatch
    assert not (home / "acme-infra").exists()                                 # excluded: never cloned
    [d] = w.calls("dispatch")
    listing = Path(d["env"]["SUPERVISOR_EXTRA_DIRS_FILE"])
    assert listing.read_text() == f"{w.repo}\n{theme}\n"
    assert (listing.parent / "writable-directories.txt").read_text() == f"{w.repo}\n{theme}\n"
    assert [c["argv"][1] for c in w.calls("night-shift") if c["argv"][0] == "trust"] == [
        str(w.repo), str(theme), str(home)]
    # The session's folder: the runner's notes only, every clone in it ignored, nothing to commit.
    assert git(home, "ls-files").split() == [".gitignore", "AGENTS.md", "CLAUDE.md"]
    assert git(home, "status", "--porcelain") == ""
    notes = (home / "AGENTS.md").read_text()
    assert "- `acme-platform`\n- `acme-theme`: Brand theme: colours and fonts\n" in notes
    assert f"Its absolute path (the workspace): `{home.resolve()}`\n" in notes
    assert "acme-infra" not in notes and (home / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    assert git(home, "log", "-1", "--format=%an <%ae>|%s", "main") == (
        "ACME Agent <bot@example.com>|Arceus: workspace of GitLab group clients/acme")


def test_nested_repositories_never_enter_the_home(w):
    take(w)
    home = w.cfg.paths.workspace
    w.agent_commits()
    (w.repo / "loose.txt").write_text("not committed\n")
    git(home, "add", "-A")
    assert git(home, "diff", "--cached", "--name-only") == ""  # no gitlink, no file of a clone
    assert "160000" not in git(home, "ls-files", "--stage")


def test_switching_the_home_never_touches_the_clones(w):
    take(w)
    home, task = w.cfg.paths.workspace, w.store.task("ACME-1")
    (w.repo / "wip.txt").write_text("uncommitted work of a task\n")
    git(home, "add", "-f", "acme-platform")  # a force-added gitlink on the task branch
    git(home, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "oops")
    w.runner.delivery.git.home_on(home, "main")
    w.runner.delivery.git.home_on(home, task.branch)
    w.runner.delivery.git.home_on(home, "main")
    assert (w.repo / "wip.txt").read_text() == "uncommitted work of a task\n"
    assert git(w.repo, "symbolic-ref", "--short", "HEAD") == task.branch and (w.repo / ".git").is_dir()
    assert git(home, "symbolic-ref", "--short", "HEAD") == "main" and git(home, "status", "--porcelain") == ""


def test_the_home_is_never_published_and_its_commits_stay_local(w):
    take(w)
    home = w.cfg.paths.workspace
    (home / "AGENTS.md").write_text("the agent rewrote the notes\n")
    git(home, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "notes")
    sha = w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    assert str(home) in [r["path"] for r in w.engine_state()["tasks"]["ACME-1"]["receipt"]["repos"]]
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and w.remote_branch(task.branch) == sha and len(w.gl.mrs) == 1
    assert w.events_named("home_commits_not_published")
    # The next task starts from the runner's own notes again, not the agent's.
    take(w, 2, summary="Another")
    assert git(home, "symbolic-ref", "--short", "HEAD") == "agent/ACME-2-another"
    assert (home / "AGENTS.md").read_text().startswith("# GitLab group `clients/acme`")


def test_a_changed_repository_missing_from_bulavas_report_blocks_publication(w):
    take(w)
    theme = w.cfg.paths.workspace / "acme-theme"
    (theme / "style.css").write_text("a{}\n")
    git(theme, "add", "style.css")
    git(theme, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "style")
    w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(repos=[
        r for r in s["tasks"]["ACME-1"]["receipt"]["repos"] if r["path"] != str(theme)]))
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and w.gl.mrs == [] and w.remote_branch(task.branch) is None
    assert any("acme-theme: has commits on agent/ACME-1-fix-login-page, but the execution engine did not report" in c
               for c in w.bot_comments())


def test_mr_only_where_the_agent_changed_something(w):
    take(w)
    task = w.store.task("ACME-1")
    theme = w.cfg.paths.workspace / "acme-theme"
    base_theme = git(theme, "rev-parse", "HEAD")
    (theme / "style.css").write_text("a{}\n")
    git(theme, "add", "style.css")
    git(theme, "commit", "-q", "-m", "style")
    sha = git(theme, "rev-parse", "HEAD")
    untouched = git(w.repo, "rev-parse", "HEAD")

    def fn(s):
        t = s["tasks"]["ACME-1"]
        t.update(state="waiting_review", attempt_live=False)
        t["receipt"] = {"review": "passed", "repos": [
            {"path": str(w.repo), "branch": task.branch, "base_sha": untouched, "reviewed_sha": untouched,
             "clean": True},
            {"path": str(theme), "branch": task.branch, "base_sha": base_theme, "reviewed_sha": sha, "clean": True},
        ]}
    w.set_engine(fn)
    w.tick()
    assert w.remote_branch(task.branch, "acme-theme") == sha
    assert w.remote_branch(task.branch, "acme-platform") is None
    assert [m["source_branch"] for m in w.gl.mrs] == [task.branch] and w.gl.mrs[0]["project_id"] == 1
    assert w.store.task("ACME-1").state == "code_review"


def test_uncommitted_work_in_another_repository_stops_the_start(w):
    theme = w.cfg.paths.workspace / "acme-theme"
    subprocess.run(["git", "clone", "-q", str(w.origins["acme-theme"]), str(theme)], check=True)
    (theme / "leftover.txt").write_text("x")
    take(w)
    task = w.store.task("ACME-1")
    assert task.state == "failed" and "acme-theme" in task.reason and w.calls("dispatch") == []


def test_follow_ups_carry_the_same_repositories(w):
    take(w)
    home, theme = w.cfg.paths.workspace, w.cfg.paths.workspace / "acme-theme"
    git(theme, "checkout", "-q", "main")  # someone switched it between attempts
    git(home, "checkout", "-q", "main")   # another task started from the home's main
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(attempt_live=False))
    w.tick()
    [send] = w.calls("worker-send")
    assert str(home) in send["argv"]
    assert Path(send["env"]["SUPERVISOR_EXTRA_DIRS_FILE"]).read_text() == f"{w.repo}\n{theme}\n"
    for repo in (home, theme):
        assert git(repo, "symbolic-ref", "--short", "HEAD") == w.store.task("ACME-1").branch


def test_a_group_of_one_repository_starts_in_the_workspace_too(w):
    w.gl.repos = ["acme-platform"]
    take(w)
    [d] = w.calls("dispatch")
    assert d["argv"][14] == str(w.cfg.paths.workspace)
    assert Path(d["env"]["SUPERVISOR_EXTRA_DIRS_FILE"]).read_text() == f"{w.repo}\n"
    assert w.store.task("ACME-1").extra_repos == '["acme-platform"]'


def test_metrics_show_the_current_picture(w):
    from prometheus_client import generate_latest
    from arceus_runner.metrics import Metrics
    w.cfg.paths.bulava_state.mkdir(parents=True, exist_ok=True)
    (w.cfg.paths.bulava_state / "usage.json").write_text(json.dumps(
        {"five_hour": {"used_percentage": 4.0, "resets_at": 1792778399}, "seven_day": {"used_percentage": 5.0}}))
    m = Metrics()
    w.runner.metrics = m
    w.yt.add_issue(1, ("a1", "j.doe", "+agent"))
    w.yt.add_issue(2, ("b1", "j.doe", "+agent"))
    w.tick()
    text = generate_latest(m.registry).decode()
    assert 'runner_task{state="dispatched",summary="Issue 1",task_id="ACME-1"} 1.0' in text
    assert "runner_queue_length 1.0" in text and "runner_agent_busy 1.0" in text
    assert 'runner_health{check="service.youtrack"} 0.0' in text
    assert 'runner_usage_percent{provider="claude",window="5h"} 4.0' in text
    assert 'runner_usage_resets_at_seconds{provider="claude",window="5h"} 1.792778399e+09' in text
    assert 'runner_service_up{role="code_host",service="gitlab"} 1.0' in text



def test_commits_pushed_on_top_of_the_reviewed_one_are_named_not_overwritten(w):
    take(w)
    reviewed = w.agent_commits("a.txt")
    w.agent_commits("b.txt")
    git(w.repo, "push", "-q", "origin", f"HEAD:refs/heads/{w.store.task('ACME-1').branch}")  # the agent pushed more
    git(w.repo, "reset", "-q", "--hard", reviewed)
    w.settle("ACME-1", "waiting_review", reviewed=reviewed)
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and task.reason == "the branch on GitLab has commits that were not reviewed"
    assert w.gl.mrs == []


def test_a_branch_the_agent_already_pushed_is_not_pushed_again(w):
    take(w)
    sha = w.agent_commits()
    git(w.repo, "push", "-q", "origin", f"HEAD:refs/heads/{w.store.task('ACME-1').branch}")  # the agent pushed it
    w.settle("ACME-1", "waiting_review")
    w.tick()
    assert w.remote_branch(w.store.task("ACME-1").branch) == sha
    assert len(w.gl.mrs) == 1 and w.store.task("ACME-1").state == "code_review"



# -- requirements found missing on review ----------------------------------------------------------

def _ask(w):
    take(w)
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="needs_input", attempt_live=False, question={"id": "q-1", "text": "?", "options": []}))
    w.tick()
    assert w.store.task("ACME-1").state == "needs_input"


def test_unanswered_question_gets_a_reminder_then_fails(w):
    _ask(w)
    w.now = NOW + timedelta(hours=25)
    w.tick()
    reminders = [t for t in w.bot_comments() if "[arceus:reminder:q-1]" in t]
    assert len(reminders) == 1 and "@j.doe" in reminders[0]
    w.tick()
    assert len([t for t in w.bot_comments() if "[arceus:reminder:q-1]" in t]) == 1
    w.now = NOW + timedelta(days=6)
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and task.reason.startswith("no answer to the agent")


def test_a_usage_limit_pause_is_not_a_hang(w):
    take(w)
    w.cfg.paths.bulava_state.mkdir(parents=True, exist_ok=True)
    usage = w.cfg.paths.bulava_state / "usage.json"
    usage.write_text(json.dumps({"seven_day": {"used_percentage": 100}}))
    w.now = datetime.now(UTC) + timedelta(hours=30)
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched" and w.events_named("attempt_paused_for_limit")
    usage.write_text(json.dumps({"seven_day": {"used_percentage": 3}}))
    w.now += timedelta(hours=1)
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"  # the clock restarted when the limit was out
    w.now += timedelta(hours=9)
    w.tick()
    assert w.store.task("ACME-1").reason.startswith("the agent hung")


def test_tag_from_outside_the_allowlist_is_answered_once_on_the_ticket(w):
    w.yt.add_issue(1, ("a1", "mallory", "+agent"))
    w.tick()
    w.tick()
    w.restart()
    w.tick()
    notes = [t for t in w.bot_comments() if "[arceus:rejected:a1]" in t]
    assert len(notes) == 1 and "mallory" in notes[0] and w.calls("dispatch") == []


def test_devstack_check_failing_stops_new_work(w):
    object.__setattr__(w.cfg.health, "devstack_check", "exit 3")
    take(w)
    assert w.calls("dispatch") == [] and w.runner.health["devstack"] == "fail"
    object.__setattr__(w.cfg.health, "devstack_check", "true")
    w.tick()
    assert len(w.calls("dispatch")) == 1


def test_gitlab_token_expiry_is_watched_while_running(w):
    w.gl.token = {"scopes": ["api"], "expires_at": "2026-10-01"}
    take(w)
    assert w.runner.health["gitlab.token_expiry"] == "fail" and w.calls("dispatch") == []



def test_usage_readings_are_refreshed_before_the_doctor(w):
    take(w)
    names = [c["cmd"] for c in w.engine_state()["calls"]]
    assert "claude-usage" in names and "codex-usage" in names
    assert names.index("claude-usage") < next(i for i, c in enumerate(w.engine_state()["calls"])
                                              if c["cmd"] == "night-shift" and c["argv"][:1] == ["doctor"])


def test_everything_the_runner_writes_to_people_is_in_english(w):
    """Tickets, MRs and threads are read by the whole team: no Cyrillic in what the runner posts."""
    import re
    cyrillic = re.compile("[Ѐ-ӿ]")
    w.yt.add_issue(5, ("x1", "mallory", "+agent"))      # the allowlist note
    take(w)                                              # claim
    w.agent_commits()
    w.settle("ACME-1", "waiting_review", outcome={"summary": "Added the error pages"})
    w.tick()                                             # MR, its description, the ticket comment
    texts = [c["text"] for cs in w.yt.comments.values() for c in cs if c["author"]["login"] == w.yt.login]
    texts += [m["description"] + m["title"] for m in w.gl.mrs]
    assert texts and not [t for t in texts if cyrillic.search(t)]


def test_clones_are_readable_by_the_dev_stacks_containers(w):
    """The runner's private umask must not leak into the workspace (nginx in a container reads it)."""
    import os
    seed = w.tmp / "seed" / "acme-theme"
    (seed / "theme.css").write_text("a{}\n")
    git(seed, "add", "theme.css")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "theme")
    git(seed, "push", "-q", str(w.origins["acme-theme"]), "main")
    old = os.umask(0o077)
    try:
        take(w)  # clones every repository of the group under the runner's own umask
        theme = w.cfg.paths.workspace / "acme-theme"
        assert oct((theme / "theme.css").stat().st_mode & 0o777) == "0o644"
        assert (theme.stat().st_mode & 0o055) == 0o055
    finally:
        os.umask(old)


def test_a_tracked_file_left_private_gets_the_mode_git_records(w):
    take(w)
    (w.repo / "conf.txt").write_text("x")
    (w.repo / "conf.txt").chmod(0o600)
    git(w.repo, "add", "conf.txt")
    assert w.runner.delivery.git.fix_modes(w.repo) == 1
    assert oct((w.repo / "conf.txt").stat().st_mode & 0o777) == "0o644"


def test_debt_after_a_codex_pass_says_so_instead_of_blaming_codex(w):
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "failed", review="debt", state_reason="debt",
             outcome={"result": "succeeded_changes", "summary": "Codex: Error pages added"})
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(
        review_why="STATE: COMPLETE\nVERDICT: PASS\n\nChecked.",
        findings=[{"kind": "note", "text": "**Claude Code:** /oauth/ is not proxied by the MFE nginx"}]))
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed"
    assert task.reason.startswith("The change passed review, but the automated checks did not confirm it")
    [note] = [t for t in w.bot_comments() if "The change passed review" in t]
    assert "acme-platform" in note and "- /oauth/ is not proxied" in note
    assert "Summary of the work: Error pages added" in note
    assert "inconclusive" not in note and not re.search(r"Codex|Claude|Bulava", note)  # labels taken off
    assert w.gl.mrs == []


def _verifier_evidence(w, run_id: str, criteria: list[dict], logs: dict[str, str]) -> None:
    ev = w.cfg.paths.bulava_state / "runs" / run_id / "evidence" / "sid-1"
    ev.mkdir(parents=True)
    (ev / "evidence.json").write_text(json.dumps({"criteria": criteria}))
    for name, text in logs.items():
        (ev / name).write_text(text)


def test_debt_from_a_failed_verifier_check_names_the_check_and_its_cause(w):
    """OMD-1: the verifier's own `make test` failed on a missing tool; the ticket must not say "inconclusive"."""
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "failed", review="debt", state_reason="debt", outcome={"result": "succeeded_changes"})
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(
        run_id="run-7", review_why="STATE: COMPLETE\nVERDICT: PASS\n\n[VERIFIER OVERRIDE] failed"))
    _verifier_evidence(w, "run-7", [
        {"criterion": "omnis: make test", "status": "fail", "exit_code": 2, "note": "",
         "artifact": "/elsewhere/instances/x/evidence/sid-1/omnis-make-test.log"},
        {"criterion": "Vitest passes", "status": "pass", "exit_code": 0, "note": ""},
    ], {"omnis-make-test.log": "pylint --errors-only ./x\nmake: pylint: No such file or directory\n"
                               "make: *** [Makefile:13: test-lint] Error 127\n"})
    w.tick()
    task = w.store.task("ACME-1")
    assert task.reason == "The change passed review, but the automated checks failed, so it was not published"
    [note] = [t for t in w.bot_comments() if "The change passed review" in t]
    assert "- `omnis: make test` failed (exit 2): make: pylint: No such file or directory" in note
    assert "Vitest" not in note and "inconclusive" not in note
    assert w.gl.mrs == []


def test_debt_from_an_inconclusive_verifier_says_inconclusive(w):
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "failed", review="debt", state_reason="debt", outcome={"result": "succeeded_changes"})
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(
        run_id="run-8", review_why="STATE: COMPLETE\nVERDICT: PASS\n\nChecked."))
    _verifier_evidence(w, "run-8", [
        {"criterion": "make test", "status": "inconclusive", "exit_code": 142, "note": "timed out after 600s"},
    ], {})
    w.tick()
    [note] = [t for t in w.bot_comments() if "The change passed review" in t]
    assert "the automated checks were inconclusive" in note
    assert "- `make test` was inconclusive: timed out after 600s" in note
    assert w.gl.mrs == []


def test_the_models_in_config_reach_the_engine_and_the_event_log(w):
    w.cfg = w.cfg.model_copy(update={"bulava": w.cfg.bulava.model_copy(update={
        "claude_model": "claude-opus-5-5", "claude_effort": "xhigh", "codex_effort": "medium"})})
    # bulava.env is for the token: a model in it must not outrank config.yaml, nor sneak in under auto.
    (w.tmp / "bulava.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-test\nSUPERVISOR_CLAUDE_EFFORT=low\n"
                                      "SUPERVISOR_CODEX_MODEL=gpt-old\n")
    w.runner = w._runner()
    take(w)
    [d] = w.calls("dispatch")
    env = d["env"]
    assert env["SUPERVISOR_CLAUDE_MODEL"] == "claude-opus-5-5" and env["SUPERVISOR_CLAUDE_EFFORT"] == "xhigh"
    assert env["SUPERVISOR_CODEX_EFFORT"] == "medium" and "SUPERVISOR_CODEX_MODEL" not in env
    assert env["NIGHT_SHIFT_SETTINGS"] == "off"  # ~/.config/night-shift/config.json cannot outrank it
    [e] = w.events_named("task_dispatched")
    assert (e["claude_model"], e["claude_effort"], e["codex_model"], e["codex_effort"]) == \
        ("claude-opus-5-5", "xhigh", "auto", "medium")


def test_auto_passes_no_model_and_says_auto(w):
    take(w)
    [d] = w.calls("dispatch")
    assert not [k for k in d["env"] if k.endswith(("_MODEL", "_EFFORT"))]
    [e] = w.events_named("task_dispatched")
    assert e["claude_model"] == e["claude_effort"] == e["codex_model"] == e["codex_effort"] == "auto"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 32


def _media(w, items: list[dict], files: dict[str, bytes]) -> Path:
    folder = w.cfg.paths.workspace / ".arceus" / "media" / "ACME-1"
    folder.mkdir(parents=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    (folder / "media.json").write_text(json.dumps({"items": items}))
    return folder


def test_visual_proof_goes_to_its_mr_and_to_the_ticket(w):
    take(w)
    w.agent_commits()
    (w.cfg.paths.workspace / "secret.txt").write_text("token")
    _media(w, [
        {"file": "404.png", "caption": "404 page, desktop", "repo": "acme-platform"},
        {"file": "retry.webm", "caption": "503, then [Retry] recovers"},
        {"file": "theme.png", "caption": "Theme only", "repo": "acme-theme"},
        {"file": "fake.png", "caption": "not an image"},
        {"file": "../secret.txt", "caption": "outside"},
        {"file": "dup.png", "caption": "same bytes as 404.png"},
    ], {"404.png": PNG, "retry.webm": WEBM, "theme.png": PNG + b"t", "fake.png": b"hello", "dup.png": PNG})
    w.settle("ACME-1", "waiting_review")
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"
    [mr] = w.gl.mrs
    names = [n.split("-", 1)[1] for _, n, _ in w.gl.uploads]
    assert names == ["404.png", "retry.webm"] and {p for p, _, _ in w.gl.uploads} == {mr["project_id"]}
    assert w.gl.uploads[0][2] == PNG
    desc = mr["description"]
    assert "## Screenshots and recordings" in desc and "![404 page, desktop](/uploads/" in desc
    assert "503, then Retry recovers" in desc and "Theme only" not in desc and "secret" not in desc
    [note] = [c for c in w.yt.comments["2-1"] if "Merge request" in c["text"]]
    attached = [n.split("-", 1)[1] for n, _ in w.yt.attachments[note["id"]]]
    assert attached == ["404.png", "retry.webm", "theme.png"]
    assert "![404 page, desktop](" in note["text"] and "-404.png)" in note["text"] and "![Theme only](" in note["text"]
    skipped = " ".join(e["detail"] for e in w.events_named("media_skipped"))
    assert "fake.png" in skipped and "../secret.txt" in skipped
    assert git(w.cfg.paths.workspace, "status", "--porcelain") == ""  # the media folder is never committed


def test_a_refused_upload_does_not_hold_the_mr_back(w):
    take(w)
    w.agent_commits()
    _media(w, [{"file": "a.png", "caption": "A"}], {"a.png": PNG})
    w.gl.refuse_uploads = True
    w.settle("ACME-1", "waiting_review")
    w.tick()
    [mr] = w.gl.mrs
    assert w.store.task("ACME-1").state == "code_review"
    assert "Screenshots" not in mr["description"]
    assert any("a.png" in e["detail"] for e in w.events_named("media_skipped"))


def test_a_crash_after_the_uploads_does_not_upload_again(w):
    take(w)
    w.agent_commits()
    _media(w, [{"file": "a.png", "caption": "A"}, {"file": "b.webm", "caption": "B"}], {"a.png": PNG, "b.webm": WEBM})
    w.settle("ACME-1", "waiting_review")
    w.yt.fail_once[("POST", "/api/issues/2-1/comments")] = 503
    w.tick()
    assert w.store.task("ACME-1").state == "publishing" and len(w.gl.uploads) == 2
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"
    assert len(w.gl.uploads) == 2 and len(w.gl.mrs) == 1
    [note] = [c for c in w.yt.comments["2-1"] if "Merge request" in c["text"]]
    assert len(w.yt.attachments[note["id"]]) == 2


# -- the shared workspace: no follow-up switches it under another task ---------------------------

def occupy(w: World, n: int = 2) -> str:
    """ACME-n works in the workspace now (Bulava runs it): its branch checked out in the home and every clone."""
    db_id = w.yt.add_issue(n, (f"o{n}", "j.doe", "+agent"))
    branch = f"agent/ACME-{n}-other"
    w.store.create_task(task_id=f"ACME-{n}", issue_db_id=db_id, state="dispatched", summary="other",
                        tag_author="j.doe", repo="", repo_path=str(w.cfg.paths.workspace), branch=branch,
                        base_branch="", question_id=None, question_at_ms=None, reason=None, mr_urls="[]",
                        attempt_started=datetime.now(UTC).isoformat(), extra_repos='["acme-platform", "acme-theme"]')
    for repo in workspace_repos(w):
        git(repo, "checkout", "-q", "-b", branch)

    def running(s):
        s["tasks"][f"ACME-{n}"] = {**s["tasks"]["ACME-1"], "task_id": f"ACME-{n}", "state": "running",
                                   "attempt_live": True, "branch": branch, "question": None,
                                   "attempts": [{"n": 1, "kind": "initial", "started_at": datetime.now(UTC).isoformat(),
                                                 "ended_at": None}]}
    w.set_engine(running)
    return branch


def release(w: World, n: int = 2) -> None:
    """ACME-n is over: the workspace is free."""
    w.store.update_task(f"ACME-{n}", state="done")


def workspace_repos(w: World) -> list[Path]:
    home = w.cfg.paths.workspace
    return [home, home / "acme-platform", home / "acme-theme"]


def branches(w: World) -> dict[str, str]:
    return {r.name: git(r, "symbolic-ref", "--short", "HEAD") for r in workspace_repos(w)}


def test_an_answer_waits_while_another_task_holds_the_workspace(w):
    _ask(w)
    mine = w.store.task("ACME-1").branch
    other = occupy(w)
    w.yt.comment("2-1", "j.doe", "Postgres")
    for _ in range(3):
        w.tick()
    assert w.calls("worker-send") == [] and set(branches(w).values()) == {other}
    assert w.store.task("ACME-1").state == "needs_input" and "agent-needs-input" in w.yt.issue_tags["2-1"]
    assert not w.events_named("send_busy", "branch_caught_up")
    [wait] = [e for e in w.events_named("followup_waits") if e["task_id"] == "ACME-1"]
    assert (wait["reason"], wait["holder"]) == ("workspace_taken", "ACME-2")

    release(w)
    w.tick()
    w.tick()
    [send] = w.calls("worker-send")
    assert send["argv"][send["argv"].index("--task-id") + 1] == "ACME-1" and send["argv"][-1] == "Postgres"
    assert w.store.task("ACME-1").state == "dispatched" and set(branches(w).values()) == {mine}


def test_a_resume_waits_while_another_task_holds_the_workspace(w):
    take(w)
    mine = w.store.task("ACME-1").branch
    other = occupy(w)
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(attempt_live=False))
    w.tick()
    w.tick()
    assert w.calls("worker-send") == [] and set(branches(w).values()) == {other}
    release(w)
    w.tick()
    [send] = w.calls("worker-send")
    assert "resume-ACME-1-1" in send["argv"] and set(branches(w).values()) == {mine}


def test_a_follow_up_never_switches_a_repository_with_uncommitted_work(w):
    _ask(w)
    other = occupy(w)
    w.store.update_task("ACME-2", state="needs_input")  # not running, but its work is still in the tree
    (w.repo / "wip.txt").write_text("ACME-2's work, not committed yet\n")
    w.yt.comment("2-1", "j.doe", "Postgres")
    for _ in range(3):
        w.tick()
    assert w.calls("worker-send") == [] and set(branches(w).values()) == {other}
    assert (w.repo / "wip.txt").read_text() == "ACME-2's work, not committed yet\n"
    [wait] = [e for e in w.events_named("followup_waits") if e["task_id"] == "ACME-1"]
    assert (wait["reason"], wait["repos"]) == ("uncommitted_changes", ["acme-platform"])

    git(w.repo, "add", "wip.txt")
    git(w.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "wip")
    w.tick()
    assert len(w.calls("worker-send")) == 1 and git(w.repo, "log", "-1", "--format=%s", other) == "wip"


def test_uncommitted_work_on_the_tasks_own_branch_does_not_hold_its_follow_up(w):
    _ask(w)
    (w.repo / "wip.txt").write_text("the task's own work\n")
    w.yt.comment("2-1", "j.doe", "Postgres")
    w.tick()
    assert len(w.calls("worker-send")) == 1 and not w.events_named("followup_waits")
    assert (w.repo / "wip.txt").exists()


def test_the_engine_gets_the_independent_checks_from_config_and_never_from_bulava_env(w):
    (w.tmp / "bulava.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-test\nSUPERVISOR_CLAUDE_CHECKS=0\n"
                                      "SUPERVISOR_CLAUDE_CHECKS_ROUNDS=5\n")
    take(w)
    [d] = w.calls("dispatch")
    assert (d["env"]["SUPERVISOR_CLAUDE_CHECKS"], d["env"]["SUPERVISOR_CLAUDE_CHECKS_LIST"],
            d["env"]["SUPERVISOR_CLAUDE_CHECKS_ROUNDS"]) == ("1", "code security", "1")
    [e] = w.events_named("task_dispatched")
    assert (e["claude_checks"], e["claude_checks_rounds"], e["claude_checks_required"]) == ("code security", 1, True)


def test_a_required_check_that_did_not_run_is_not_called_codex_s_decision(w):
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "failed", review="failed", state_reason="needs-user")
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(
        review_why="independent check failed (security) and SUPERVISOR_CLAUDE_CHECKS_REQUIRED=1"))
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed"
    assert task.reason.startswith("a required pre-review check (code or security) did not run")
    assert w.remote_branch("agent/ACME-1-fix-login-page") is None and w.gl.mrs == []
    [note] = [t for t in w.bot_comments() if "pre-review check" in t]
    assert "Checks that did not run: security." in note
    assert not re.search(r"Codex|Claude|Bulava|SUPERVISOR_|independent check failed", note)
    [missed] = [e for e in read_events(w.cfg.paths.events) if e["event"] == "required_check_missed"]
    assert missed["review_why"].startswith("independent check failed (security)")  # the gate's words stay inside


def test_a_person_asked_by_codex_is_still_codex_s_decision(w):
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "failed", review="failed", state_reason="needs-user")
    w.tick()
    assert w.store.task("ACME-1").reason == "the review asked for a person's decision"


def test_a_model_or_engine_label_at_a_line_start_goes_and_the_same_word_in_the_text_stays():
    from arceus_runner.replies import unattributed
    assert unattributed("Codex: tests are missing") == "tests are missing"
    assert unattributed("- Codex: a\n- **Claude Code:** b\n1. [Bulava]: c\nClaude: d") == "- a\n- b\n1. c\nd"
    assert unattributed("Ran `codex exec`: fine\nuse the Codex API: it works") == \
        "Ran `codex exec`: fine\nuse the Codex API: it works"


def test_a_failure_recorded_by_an_earlier_version_is_posted_once_with_its_own_key(w):
    """An upgrade while a failure comment waits: the old intent (hashed from its old words) is finished as it
    was written, the new code does not post the same failure again under a new key, and a failure the new
    code records is numbered, not named by its words."""
    take(w)
    old = "failed:0123456789"
    body = "The agent could not take this ticket to a merge request: Codex did not accept the work."
    with w.store.transaction():
        w.store.update_task("ACME-1", state="failed", reason="Codex did not accept the work")
        w.store.action_intent(f"ACME-1:comment:{old}", "ACME-1", "comment", json.dumps({"kind": old, "text": body}))
    w.tick()
    w.tick()
    posted = [t for t in w.bot_comments() if "could not take this ticket" in t]
    assert posted == [f"{body}\n\n[arceus:{old}]"]
    w.runner.delivery.fail(w.store.task("ACME-1"), "a later failure")
    w.tick()
    numbered = [t for t in w.bot_comments() if "[arceus:failed:n" in t]
    assert len(numbered) == 1 and numbered[0].endswith("[arceus:failed:n2]")   # numbered after the old one
    assert len([t for t in w.bot_comments() if "could not take this ticket" in t]) == 2
