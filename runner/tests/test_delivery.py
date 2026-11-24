"""Live delivery against a fake Bulava CLI, fake trackers and real git with a local origin."""

from __future__ import annotations

import json
import atexit
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from arceus_runner import loop
from arceus_runner.delivery import GATE_STOPPED
from arceus_runner.config import Config
from arceus_runner.texts import PROJECT_NOTES_LIMIT, ProjectNotesError, project_notes
from arceus_runner.db import ReviewStore as TaskStore
from arceus_runner.engine import Engine
from arceus_runner.events import EventLog
from arceus_runner.gitops import Git, GitAuth
from arceus_runner.progress import task_key

from .conftest import GL_TOKEN, FakeClock, example_raw, org_words, providers, read_events
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


REPOS = ("acme-platform", "acme-theme", "acme-infra")
_template: Path | None = None


def _origins_template() -> Path:
    """The origins and their seed clones, made once per test process and copied into each World:
    every test still gets repositories of its own, without paying for git init, commit and push."""
    global _template
    if _template is None:
        root = Path(tempfile.mkdtemp(prefix="arceus-origins-"))
        atexit.register(shutil.rmtree, root, True)
        for name in REPOS:
            origin, seed = root / "origin" / f"{name}.git", root / "seed" / name
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
            git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
            git(seed, "push", "-q", str(origin), "main")
        _template = root
    return _template


class World:
    def __init__(self, tmp: Path, secrets, engine_dir: Path | None = None, engine_env: dict | None = None,
                 settings: dict | None = None):
        self.tmp = tmp
        self.engine_env = dict(engine_env or {})
        if engine_dir is None:
            # The fake engine's tmux server is its own too (fake_engine.tmux): such a test never asks a real tmux.
            # The real engine (test_real_engine.py) starts its workers in a real one.
            fakebin = tmp / "fakebin"
            fakebin.mkdir(parents=True, exist_ok=True)
            (fakebin / "tmux").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_ENGINE}" tmux "$@"\n')
            (fakebin / "tmux").chmod(0o755)
            self.engine_env = {"PATH": f"{fakebin}:/usr/local/bin:/usr/bin:/bin", **self.engine_env}
            engine_dir = tmp / "engine"
            (engine_dir / "bin").mkdir(parents=True)
            for script, cmd in (("dispatch.sh", "dispatch"), ("worker-send.sh", "worker-send"),
                                ("night-shift.sh", "night-shift"), ("claude-usage.sh", "claude-usage"),
                                ("codex-usage.sh", "codex-usage")):
                f = engine_dir / "bin" / script
                f.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_ENGINE}" {cmd} "$@"\n')
                f.chmod(0o755)
        (tmp / "bulava.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-test\n")
        (tmp / "bulava.env").chmod(0o600)

        # origins: a bare repository with one commit on main for every repository of the group
        for part in ("origin", "seed"):
            shutil.copytree(_origins_template() / part, tmp / part)
        self.origins = {name: tmp / "origin" / f"{name}.git" for name in REPOS}
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
        raw["health"].pop("devstack_check", None)
        for section, values in (settings or {}).items():  # a test's own settings, section by section
            raw[section] = {**raw.get(section, {}), **values} if isinstance(values, dict) else values
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
        (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
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
    assert len(d["argv"]) == 16   # no review budget of its own: a ticket's report is reviewed by the server's rule
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


EMPTY_DIFF_WHY = ("Declared 'succeeded_no_change' with an empty diff — no machine verification of a no-change/"
                  "research claim is possible; needs human review.")


def passed_earlier(w: World, sha: str, passed: str | None) -> None:
    """A first attempt committed `sha` and passed without declaring an outcome; the next one only declared it."""
    w.settle("ACME-1", "failed", review="debt", state_reason="debt",
             outcome={"result": "succeeded_no_change", "summary": "Shows when each link was created"})
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(review_why=EMPTY_DIFF_WHY))
    f = w.cfg.paths.bulava_state / "tasks" / task_key("ACME-1") / "task.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"repo_reviewed": {str(w.repo): passed} if passed else {}}))


def test_work_an_earlier_attempt_passed_is_published_when_the_next_only_declares_it(w):
    # Staging, STG-29: the agent committed, Codex passed it, the run ended without an outcome; on /continue it
    # declared "no change" and the engine banked the empty diff as debt.
    take(w)
    sha = w.agent_commits()
    passed_earlier(w, sha, sha)
    w.tick()
    assert w.remote_branch("agent/ACME-1-fix-login-page") == sha and len(w.gl.mrs) == 1
    assert w.store.task("ACME-1").state == "code_review" and w.events_named("published_earlier_pass")


def test_commits_no_passing_review_saw_are_not_published_on_an_empty_diff(w):
    take(w)
    first = w.agent_commits()
    w.agent_commits("more.txt")
    passed_earlier(w, first, first)                      # HEAD moved past what the review passed
    w.tick()
    assert w.remote_branch("agent/ACME-1-fix-login-page") is None and w.gl.mrs == []
    assert w.store.task("ACME-1").state == "failed" and not w.events_named("published_earlier_pass")


def test_an_empty_diff_without_any_passing_review_stays_debt(w):
    take(w)
    passed_earlier(w, w.agent_commits(), None)
    w.tick()
    task = w.store.task("ACME-1")
    assert w.gl.mrs == [] and task.state == "failed" and "agent-failed" in w.yt.issue_tags["2-1"]
    assert "commits it made earlier were never reviewed" in task.reason   # not a no-change question: work is there


def passed_without_outcome(w: World, sha: str, passed: str | None) -> None:
    """The attempt passed review but declared no outcome: `waiting_review`, no receipt, the repos in `task show`."""
    def fn(s):
        t = s["tasks"]["ACME-1"]
        t.update(state="waiting_review", state_reason="passed", attempt_live=False, receipt=None, outcome=None)
        t["attempts"][-1].update(ended_at="now", review="passed")
        t["repos"] = [{**r, "branch": t["branch"], "clean": True,
                       "reviewed_sha": sha if Path(r["path"]) == w.repo else r["base_sha"]} for r in t["repos"]]
    w.set_engine(fn)
    f = w.cfg.paths.bulava_state / "tasks" / task_key("ACME-1") / "task.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"repo_reviewed": {str(w.repo): passed} if passed else {}}))


def test_a_pass_without_a_declared_outcome_is_published(w):
    # Staging, STG-37: Codex passed the round, the agent stopped without declaring the outcome, and Bulava
    # wrote no receipt; what the review accepted is in `task show` and `repo_reviewed`.
    take(w)
    sha = w.agent_commits()
    passed_without_outcome(w, sha, sha)
    w.tick()
    assert w.remote_branch("agent/ACME-1-fix-login-page") == sha and len(w.gl.mrs) == 1
    assert w.store.task("ACME-1").state == "code_review" and w.events_named("published_without_outcome")


def test_a_later_round_without_a_summary_keeps_what_the_mr_says(w):
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "waiting_review", outcome={"summary": "Imports links from a CSV file"})
    w.tick()
    assert "Imports links from a CSV file" in w.gl.mrs[0]["description"]
    w.store.update_task("ACME-1", state="dispatched")          # a review round
    sha = w.agent_commits("more.txt")
    passed_without_outcome(w, sha, sha)
    w.tick()
    assert w.remote_branch("agent/ACME-1-fix-login-page") == sha
    assert "Imports links from a CSV file" in w.gl.mrs[0]["description"]


def test_a_pass_without_an_outcome_publishes_only_what_the_review_saw(w):
    take(w)
    seen = w.agent_commits()
    sha = w.agent_commits("more.txt")
    passed_without_outcome(w, sha, seen)
    w.tick()
    assert w.gl.mrs == [] and w.store.task("ACME-1").state == "failed"


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
    assert send["argv"][-1] == "2. SQLite" and "a.smith" in send["argv"]   # the option in words, not a bare number
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


def test_a_reboot_is_sent_on_once_and_the_time_it_was_down_is_not_counted(w):
    take(w)
    w.now = datetime.now(UTC) + timedelta(hours=7)  # down for most of attempt_timeout (8 h)
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(attempt_live=False))
    w.tick()
    w.tick()
    assert ["resume-ACME-1-1" in c["argv"] for c in w.calls("worker-send")] == [True]
    assert "--answers" not in w.calls("worker-send")[0]["argv"] and w.store.task("ACME-1").state == "dispatched"
    w.now += timedelta(hours=5)  # 12 h since the first start, 5 h into the attempt after the reboot
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    # The next week's reboot: the attempt it cut off is sent on in turn.
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(attempt_live=False))
    w.tick()
    assert [c["argv"][c["argv"].index("--message-id") + 1] for c in w.calls("worker-send")] \
        == ["resume-ACME-1-1", "resume-ACME-1-2"]


def test_an_attempt_that_works_on_while_a_limit_is_out_is_not_paused_and_stops_at_the_ceiling(w):
    """A used-up subscription pauses only an attempt that waits for it. A final check that goes on beating meanwhile
    is working: its time counts toward the ceiling."""
    take(w)
    w.cfg.paths.bulava_state.mkdir(parents=True, exist_ok=True)
    (w.cfg.paths.bulava_state / "usage.json").write_text(json.dumps({"five_hour": {"used_percentage": 100}}))
    start = w.now = datetime.now(UTC)
    _heartbeat(w)
    w.tick()
    for h in range(1, 24):
        w.now = start + timedelta(hours=h)
        _heartbeat(w, heartbeat=True, note=f"Фінальні перевірки: {h * 60} хв")
        w.tick()
        assert w.store.task("ACME-1").state == "dispatched", f"stopped at {h} h"
    w.now = start + timedelta(hours=25)
    _heartbeat(w, heartbeat=True, note="Фінальні перевірки: 1500 хв")
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed"
    assert task.reason == ("the attempt ran out of time: no verdict in 24 h, still working "
                           "(last step: gate.verify (running))")


@pytest.mark.parametrize("step", [{"stage": "agent", "state": "waiting", "note": "Чекає на фонових агентів"}, None])
def test_a_used_up_limit_without_the_engine_s_pause_of_this_attempt_pauses_nothing(w, step):
    """Only Bulava's pause record for this very attempt stops its clock. A `waiting` step of another kind (background
    agents), or no step at all, with a limit used up is no pause: the ceiling counts, and silence too."""
    take(w)
    w.cfg.paths.bulava_state.mkdir(parents=True, exist_ok=True)
    (w.cfg.paths.bulava_state / "usage.json").write_text(json.dumps({"five_hour": {"used_percentage": 100}}))
    start = w.now = datetime.now(UTC)
    if step:
        _heartbeat(w, **step)
    else:
        _heartbeat(w, did="d-0")  # the folder and record exist; no line of this attempt
    _paused(w, did="d-0")  # a pause record another attempt left
    w.tick()
    if step:
        for h in range(1, 24):
            w.now = start + timedelta(hours=h)
            _heartbeat(w, **step)
            w.tick()
            assert w.store.task("ACME-1").state == "dispatched", f"stopped at {h} h"
        w.now = start + timedelta(hours=25)
        _heartbeat(w, **step)
        w.tick()
        assert w.store.task("ACME-1").reason.startswith("the attempt ran out of time: no verdict in 24 h")
    else:
        w.now = start + timedelta(hours=8, minutes=1)
        w.tick()
        assert w.store.task("ACME-1").reason == "the agent hung: no sign of activity for 8 h"


def test_a_reviewer_that_cannot_be_reached_is_no_pause_and_the_ceiling_counts(w):
    """Bulava parks a run whose Codex cannot be reached with the same marker, "reviewer unreachable": no usage window,
    so the clock goes on; a watchdog's quota pause in the same place stops it."""
    take(w)
    start = w.now = datetime.now(UTC)
    _heartbeat(w)
    w.tick()
    for h in range(1, 24):
        w.now = start + timedelta(hours=h)
        _heartbeat(w, heartbeat=True, note=f"Фінальні перевірки: {h * 60} хв")
        _paused(w, reason="reviewer unreachable")
        w.tick()
    w.now = start + timedelta(hours=25)
    _heartbeat(w, heartbeat=True, note="Фінальні перевірки: 1500 хв")
    w.tick()
    assert w.store.task("ACME-1").reason.startswith("the attempt ran out of time: no verdict in 24 h")


def test_a_pause_the_engine_ended_while_the_runner_was_down_counts_only_until_it_ended(w):
    """Paused from 20 h to 21 h, the runner down from 20 h to 30 h: the engine's own record of when it took the pause
    off (pause-last.json) bounds it, so the attempt has worked 29 h, past its ceiling."""
    take(w)
    start = w.now = datetime.now(UTC)
    _heartbeat(w)
    w.tick()
    for h in (5, 10, 15, 20):
        w.now = start + timedelta(hours=h)
        _heartbeat(w)
        w.tick()
    _paused(w)
    w.tick()
    assert json.loads(w.store.task("ACME-1").attempt_pause)["since"]
    w.restart()
    w.now = start + timedelta(hours=21)
    _paused(w, on=False)  # the watchdog takes it off and says when
    (w.cfg.paths.bulava_state / "instances" / "acme" / "pause-last.json").write_text(json.dumps(
        {"attempts": 0, "cleared_at": int(w.now.timestamp()), "verdict": "cleared-fresh"}))
    for h in (23, 27, 30):
        w.now = start + timedelta(hours=h)
        _heartbeat(w)
    w.tick()
    assert w.store.task("ACME-1").reason.startswith("the attempt ran out of time: no verdict in 24 h")


def test_a_pause_the_runner_first_sees_after_a_restart_counts_from_when_the_engine_recorded_it(w):
    """Work to 20 h, the engine pauses at 20 h while the runner is down, the runner is back at 28 h, the engine goes on
    at 30 h: 10 h of pause, so at 33 h the attempt has worked 23 h, and at 35 h it is past its ceiling."""
    take(w)
    start = w.now = datetime.now(UTC)
    _heartbeat(w)
    w.tick()
    for h in (5, 10, 15, 20):
        w.now = start + timedelta(hours=h)
        _heartbeat(w)
        w.tick()
    _paused(w)  # recorded at 20 h; the runner does not look again before it goes down
    w.restart()
    w.now = start + timedelta(hours=28)
    w.tick()
    since = datetime.fromisoformat(json.loads(w.store.task("ACME-1").attempt_pause)["since"])
    assert since == (start + timedelta(hours=20)).replace(microsecond=0)  # the marker's recorded_at, whole seconds
    w.now = start + timedelta(hours=30)
    _paused(w, on=False)
    (w.cfg.paths.bulava_state / "instances" / "acme" / "pause-last.json").write_text(json.dumps(
        {"attempts": 0, "cleared_at": int(w.now.timestamp()), "verdict": "cleared-fresh"}))
    for h in (31, 33):
        w.now = start + timedelta(hours=h)
        _heartbeat(w)
        w.tick()
        assert w.store.task("ACME-1").state == "dispatched", f"stopped at {h} h"
    w.now = start + timedelta(hours=35)
    _heartbeat(w)
    w.tick()
    assert w.store.task("ACME-1").reason.startswith("the attempt ran out of time: no verdict in 24 h")


def test_two_pauses_the_runner_saw_as_one_across_a_restart_are_counted_apart(w):
    """Paused 20–21 h (the runner saw it begin, then went down), paused again 27–29 h, the runner back at 28 h: 3 h of
    pause, so at 29 h the attempt has worked 26 h and is past its ceiling."""
    take(w)
    start = w.now = datetime.now(UTC)
    _heartbeat(w)
    w.tick()
    for h in (5, 10, 15, 20):
        w.now = start + timedelta(hours=h)
        _heartbeat(w)
        w.tick()
    _paused(w)
    w.tick()  # the runner sees the first pause, then goes down
    w.restart()
    last = w.cfg.paths.bulava_state / "instances" / "acme" / "pause-last.json"
    w.now = start + timedelta(hours=21)
    _paused(w, on=False)
    last.write_text(json.dumps({"attempts": 0, "cleared_at": int(w.now.timestamp()), "verdict": "cleared-fresh"}))
    w.now = start + timedelta(hours=27)
    _paused(w)  # the second one, recorded at 27 h
    w.now = start + timedelta(hours=28)
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    w.now = start + timedelta(hours=29)
    _paused(w, on=False)
    last.write_text(json.dumps({"attempts": 0, "cleared_at": int(w.now.timestamp()), "verdict": "cleared-fresh"}))
    _heartbeat(w)
    w.tick()
    assert w.store.task("ACME-1").reason.startswith("the attempt ran out of time: no verdict in 24 h")


def test_hung_attempt_fails_after_the_timeout(w):
    take(w)
    w.now = datetime.now(UTC) + timedelta(hours=9)
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and task.reason.startswith("the agent hung")


def _heartbeat(w: World, did: str = "d-1", stage: str = "gate.verify", state: str = "running", **extra) -> None:
    """The engine's record of the attempt the runner watches, and one line of its journal at w.now: a step, or the
    gate repeating it while a long check runs."""
    inst = w.cfg.paths.bulava_state / "instances" / "acme"
    inst.mkdir(parents=True, exist_ok=True)
    rec = w.cfg.paths.bulava_state / "tasks" / task_key("ACME-1")
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "task.json").write_text(json.dumps({"task_id": "ACME-1", "state": "running", "attempts": [
        {"n": 1, "dispatch_id": "d-1", "instance": str(inst)}]}))
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["attempts"][-1].update(dispatch_id="d-1"))
    with (inst / "run-events.jsonl").open("a") as f:
        f.write(json.dumps({"dispatch_id": did, "stage": stage, "state": state,
                            "ms": int(w.now.timestamp() * 1000), **extra}) + "\n")


def _paused(w: World, did: str = "d-1", on: bool = True, reason: str = "usage limit seen in the worker pane") -> None:
    """Bulava's own record that an attempt waits for a usage window (supervisor-lib.sh pause_record, as its watchdog
    and review gate write it, Bulava 1.20.9); removed when the attempt goes on."""
    f = w.cfg.paths.bulava_state / "instances" / "acme" / "paused-for-limit.json"
    if not on:
        f.unlink(missing_ok=True)
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"provider": "claude", "resume_after": int(w.now.timestamp()) + 3600,
                             "reason": reason, "recorded_at": int(w.now.timestamp()), "prior_attempts": 0,
                             "run_id": "r-1", "dispatch_id": did, "session_id": "sess-1"}))


def test_an_attempt_at_work_is_timed_from_its_newest_sign_of_life_and_stopped_at_the_ceiling(w):
    """An attempt in a long back-and-forth with its review was failed as hung after 8 h while it was working.
    Silence counts from the attempt's newest engine line (the gate repeats it every minute during a long check);
    `attempt_ceiling` still bounds the attempt however busy it is."""
    take(w)
    w.now = datetime.now(UTC)
    _heartbeat(w)
    w.tick()
    for _ in range(4):  # 20 h in all, a sign of life every 5 h
        w.now += timedelta(hours=5)
        _heartbeat(w)
        w.tick()
        assert w.store.task("ACME-1").state == "dispatched"
    w.now += timedelta(hours=4, minutes=30)  # 24.5 h since the start
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed"
    assert task.reason == ("the attempt ran out of time: no verdict in 24 h, still working "
                           "(last step: gate.verify (running))")
    assert [c["argv"][0] for c in w.calls("night-shift")][-1] == "stop"


def _ledger(w: World, **items) -> None:
    """The task's review list as Bulava keeps it: id → open."""
    (w.cfg.paths.bulava_state / f"review-ledger-{task_key('ACME-1')}").write_text(json.dumps(
        {"items": [{"id": i, "text": "…", "open": o} for i, o in items.items()], "stall": 0, "churn": 0}))


def test_a_long_check_that_keeps_saying_it_runs_is_work_until_its_heartbeat_stops(w):
    """From PR #22: a final check that runs for hours repeats one step with a heartbeat. While it beats the attempt is
    not hung, though the step never changes; when the beat stops, silence counts from the last beat."""
    take(w)
    start = w.now = datetime.now(UTC)
    _heartbeat(w)
    w.tick()
    for h in range(1, 12):
        w.now = start + timedelta(hours=h)
        _heartbeat(w, heartbeat=True, note=f"Фінальні перевірки: {h * 60} хв")
        w.tick()
        assert w.store.task("ACME-1").state == "dispatched", f"failed at {h} h while the check was beating"
    w.now = start + timedelta(hours=18)  # the beat stopped at 11 h: 7 h of silence
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    w.now = start + timedelta(hours=19, minutes=30)
    w.tick()
    assert w.store.task("ACME-1").reason == "the agent hung: no sign of activity for 8 h (last step: gate.verify (running))"


@pytest.mark.parametrize("restart", [False, True])
def test_silence_is_counted_from_the_last_sign_of_life_and_names_where_the_attempt_was(w, restart):
    take(w)
    w.now = datetime.now(UTC)
    _heartbeat(w)
    w.tick()
    w.now += timedelta(hours=3)
    _heartbeat(w, stage="gate.review", state="retrying", round=3, max=5)
    _ledger(w, T1=False, T2=True, N1=True)
    w.tick()
    if restart:  # a new process reads the round again from the journal
        w.restart()
        w.tick()
    w.now += timedelta(hours=8, minutes=1)
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed"
    assert task.reason == ("the agent hung: no sign of activity for 8 h (last step: gate.review (retrying); "
                           "review round 3; open review items: T2, N1)")


def test_a_restarted_runner_reads_the_attempt_before_it_counts_its_silence(w):
    take(w)
    w.now = datetime.now(UTC)
    _heartbeat(w)
    w.tick()
    w.restart()
    w.now += timedelta(hours=9)
    _heartbeat(w)  # it went on working while the runner was down
    w.tick()  # the new process reads the attempt's files before it decides
    assert w.store.task("ACME-1").state == "dispatched"


def test_a_sign_of_life_between_two_looks_is_read_before_the_attempt_is_stopped(w):
    take(w)
    w.now = datetime.now(UTC)
    _heartbeat(w)
    w.tick()
    w.now += timedelta(hours=7)
    _heartbeat(w)  # written after the last look
    w.now += timedelta(hours=1, minutes=1)  # 8 h 1 min since the look before it
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"


def test_the_ceiling_on_the_first_pass_after_a_restart_still_says_where_the_attempt_was(w):
    take(w)
    w.now = datetime.now(UTC)
    _heartbeat(w)
    w.tick()
    w.now += timedelta(hours=20)
    _heartbeat(w, stage="gate.review", state="retrying", round=4, max=5)
    _ledger(w, T2=True)
    w.tick()
    w.restart()
    w.now += timedelta(hours=4, minutes=30)
    w.tick()
    assert w.store.task("ACME-1").reason == (
        "the attempt ran out of time: no verdict in 24 h, still working "
        "(last step: gate.review (retrying); review round 4; open review items: T2)")


def test_an_earlier_attempt_s_journal_does_not_keep_this_one_alive(w):
    take(w)
    w.now = datetime.now(UTC)
    _heartbeat(w, did="d-0")  # another dispatch's line in the same folder
    w.tick()
    w.now += timedelta(hours=8, minutes=1)
    _heartbeat(w, did="d-0")
    w.tick()
    assert w.store.task("ACME-1").reason == "the agent hung: no sign of activity for 8 h"
    assert [c["argv"][0] for c in w.calls("night-shift")][-1] == "stop"


def test_an_mr_refused_while_gitlab_does_not_know_the_pushed_branch_yet_is_asked_again(w, monkeypatch):
    """GitLab's API learns of a pushed branch a moment after the push: the MR is asked for again a few seconds
    apart, in the same tick, and only one is made."""
    waits: list[float] = []
    monkeypatch.setattr("arceus_runner.gitlab._sleep", waits.append)
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    w.gl.branch_late = 2
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"
    assert len(w.gl.mrs) == 1 and waits == [1.0, 2.0]
    assert not w.events_named("reconcile_failed")


def test_a_branch_gitlab_still_does_not_know_after_the_tries_is_published_on_a_later_tick(w, monkeypatch):
    monkeypatch.setattr("arceus_runner.gitlab._sleep", lambda s: None)
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    w.gl.branch_late = 4  # one more than the tries
    w.tick()
    assert w.store.task("ACME-1").state == "publishing" and not w.gl.mrs
    [failed] = w.events_named("reconcile_failed")
    assert "does not exist" in failed["reason"]
    w.tick()
    assert w.store.task("ACME-1").state == "code_review" and len(w.gl.mrs) == 1


def test_any_other_refusal_of_an_mr_is_not_asked_again(monkeypatch):
    from arceus_runner.gitlab import GitLabWriter
    from arceus_runner.http import BadRequest
    waits: list[float] = []
    monkeypatch.setattr("arceus_runner.gitlab._sleep", waits.append)
    asked: list[str] = []

    class Client:
        def request(self, method, path, **kwargs):
            asked.append(path)
            raise BadRequest("gitlab", 'POST merge_requests: HTTP 400: {"message":{"target_branch":["is invalid"]}}',
                             400)

    writer = GitLabWriter.__new__(GitLabWriter)
    writer.c = Client()
    with pytest.raises(BadRequest):
        writer.create_mr(1, source_branch="b", target_branch="nope", title="t", description="d", label="agent")
    assert len(asked) == 1 and waits == []


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


def _commit_in_both(w: World) -> dict[str, str]:
    shas = {"acme-platform": w.agent_commits()}
    theme = w.cfg.paths.workspace / "acme-theme"
    (theme / "theme.css").write_text("body {}\n")
    git(theme, "add", "theme.css")
    git(theme, "commit", "-q", "-m", "theme")
    shas["acme-theme"] = git(theme, "rev-parse", "HEAD")
    return shas


@pytest.mark.parametrize("dies_in", ["push_sha", "create_mr"])
def test_a_stop_after_the_first_push_or_the_first_mr_publishes_each_repository_once(w, monkeypatch, dies_in):
    """Two repositories to publish; the runner stops right after the first push, or right after the first MR
    is created. After a restart each branch has its reviewed commit, each repository one MR, the ticket one note."""
    take(w)
    shas = _commit_in_both(w)
    w.settle("ACME-1", "waiting_review")
    pushes: list[str] = []
    real_push = Git.push_sha

    def push(self, repo, sha, branch, replaces=None):
        real_push(self, repo, sha, branch, replaces)
        pushes.append(Path(repo).name)
        if dies_in == "push_sha" and len(pushes) == 1:
            raise KeyboardInterrupt  # the branch is on the code host, nothing after it is done

    real_create = type(w.runner.code_host).create_mr

    def create(self, *args, **kwargs):
        mr = real_create(self, *args, **kwargs)
        if dies_in == "create_mr" and len(w.gl.mrs) == 1:
            raise KeyboardInterrupt  # the MR exists, the runner never wrote it down
        return mr

    monkeypatch.setattr(Git, "push_sha", push)
    monkeypatch.setattr(type(w.runner.code_host), "create_mr", create)
    with pytest.raises(KeyboardInterrupt):
        w.tick()
    assert w.store.task("ACME-1").state == "publishing"
    w.restart()
    w.clock.advance(3600)
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "code_review"
    branch = task.branch
    assert {name: w.remote_branch(branch, name) for name in shas} == shas
    assert sorted(pushes) == ["acme-platform", "acme-theme"]  # each branch pushed once: the retry found it there
    assert len(w.gl.mrs) == 2 and len({(m["project_id"], m["source_branch"]) for m in w.gl.mrs}) == 2
    assert len({m.repo for m in w.store.task_mrs("ACME-1")}) == 2
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

    def push_then_break(repo, s, branch, replaces=None):
        real_push(repo, s, branch, replaces)
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
                     "web_url": "https://gl/clients/acme/-/merge_requests/1"})
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


def notes_world(tmp_path, secrets, text: str | None) -> tuple[World, Path]:
    notes = tmp_path / "project-notes.md"
    if text is not None:
        notes.write_text(text)
    return World(tmp_path, secrets, settings={"paths": {"project_notes": str(notes)}}), notes


def test_the_project_notes_end_the_agents_notes_and_each_task_keeps_its_own(tmp_path, secrets):
    w, notes = notes_world(tmp_path, secrets, "<!-- for people -->\n## Tests\n\nTest files are `*_spec.py`.\n")
    home = w.cfg.paths.workspace
    take(w)
    task = w.store.task("ACME-1")
    text = (home / "AGENTS.md").read_text()
    assert text.endswith("- `acme-theme`\n\n## Project notes\n\nThese are the project's notes. "
                         "Where they contradict the server's notes or the rules above, those win.\n\n"
                         "## Tests\n\nTest files are `*_spec.py`.\n")
    assert "for people" not in text and not org_words(text) and (home / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    [ev] = w.events_named("workspace_home_updated")
    assert ev["project_notes"] == len("## Tests\n\nTest files are `*_spec.py`.") and \
        ev["commit"] == git(home, "rev-parse", "main")

    # Changed while ACME-1 works: ACME-1 keeps what it started with, through its review, the next task gets the new.
    notes.write_text("Test files are `test_*.py`.\n")
    w.tick()
    assert "`*_spec.py`" in (home / "AGENTS.md").read_text()
    w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"
    take(w, 2, summary="Another")
    assert git(home, "symbolic-ref", "--short", "HEAD") == "agent/ACME-2-another"
    assert (home / "AGENTS.md").read_text().endswith("those win.\n\nTest files are `test_*.py`.\n")
    assert "`*_spec.py`" in git(home, "show", f"{task.branch}:AGENTS.md")
    w.runner.delivery.git.home_on(home, task.branch)  # how a later round of ACME-1 gets its folder
    assert "`*_spec.py`" in (home / "AGENTS.md").read_text()


def test_notes_of_comments_only_change_nothing(tmp_path, secrets):
    (tmp_path / "plain").mkdir()
    (tmp_path / "noted").mkdir()
    plain = World(tmp_path / "plain", secrets)
    take(plain)
    w, _ = notes_world(tmp_path / "noted", secrets, "<!--\nWhat the agent should know about the project.\n-->\n")
    take(w)
    assert (w.cfg.paths.workspace / "AGENTS.md").read_text() == \
        (plain.cfg.paths.workspace / "AGENTS.md").read_text().replace(str(plain.cfg.paths.workspace.resolve()),
                                                                     str(w.cfg.paths.workspace.resolve()))
    assert "Project notes" not in (w.cfg.paths.workspace / "AGENTS.md").read_text()
    assert git(w.cfg.paths.workspace, "rev-list", "--count", "main") == "1"
    assert w.events_named("workspace_home_updated")[0]["project_notes"] == 0
    for world in (plain, w):
        world.events.close()
        world.store.close()


@pytest.mark.parametrize("text,reason", [
    (None, "does not exist"),
    ("x" * (PROJECT_NOTES_LIMIT + 1),
     f"is {PROJECT_NOTES_LIMIT + 1} bytes without comments, more than {PROJECT_NOTES_LIMIT}; shorten it"),
    (b"\xff\xfe", "cannot be read"),
])
def test_notes_that_cannot_be_given_whole_stop_the_task(tmp_path, secrets, text, reason):
    w, notes = notes_world(tmp_path, secrets, text if isinstance(text, str) else None)
    if isinstance(text, bytes):
        notes.write_bytes(text)
    take(w)
    task = w.store.task("ACME-1")
    assert task.state == "failed" and task.reason.startswith("paths.project_notes: ") and reason in task.reason
    assert w.calls("dispatch") == [] and not (w.cfg.paths.workspace / "AGENTS.md").exists()
    w.events.close()
    w.store.close()


def test_notes_count_bytes_up_to_the_limit_inclusive(tmp_path):
    notes = tmp_path / "project-notes.md"
    notes.write_text("<!-- guidance -->\n" + "я" * (PROJECT_NOTES_LIMIT // 2) + "\n")
    assert project_notes(notes) == "я" * (PROJECT_NOTES_LIMIT // 2)
    notes.write_text("я" * (PROJECT_NOTES_LIMIT // 2) + "x")
    with pytest.raises(ProjectNotesError, match=f"is {PROJECT_NOTES_LIMIT + 1} bytes"):
        project_notes(notes)


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


def test_a_push_refused_in_one_repository_publishes_none_after_it(w):
    take(w)
    branch = w.store.task("ACME-1").branch
    theme = w.cfg.paths.workspace / "acme-theme"
    w.agent_commits()
    (theme / "theme.txt").write_text("work\n")
    git(theme, "add", "theme.txt")
    git(theme, "commit", "-q", "-m", "add theme.txt")
    w.settle("ACME-1", "waiting_review")
    first, second = [Path(r["path"]) for r in w.engine_state()["tasks"]["ACME-1"]["receipt"]["repos"]
                     if Path(r["path"]) != w.cfg.paths.workspace]
    other = w.tmp / "colleague"  # a person pushed something else to the first repository's task branch
    subprocess.run(["git", "clone", "-q", str(w.origins[first.name]), str(other)], check=True)
    git(other, "checkout", "-q", "-b", branch)
    git(other, "-c", "user.name=c", "-c", "user.email=c@c", "commit", "-q", "--allow-empty", "-m", "theirs")
    git(other, "push", "-q", "origin", branch)
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and task.reason == "the branch on GitLab has diverged from the reviewed commit"
    assert git(w.origins[second.name], "branch", "--list", branch) == ""  # never pushed after the refusal
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
    """Bulava's watchdog records the pause (paused-for-limit.json) and writes no step: the last one stays `running`."""
    take(w)
    w.cfg.paths.bulava_state.mkdir(parents=True, exist_ok=True)
    usage = w.cfg.paths.bulava_state / "usage.json"
    usage.write_text(json.dumps({"seven_day": {"used_percentage": 100}}))
    w.now = datetime.now(UTC) + timedelta(minutes=10)
    _heartbeat(w, stage="agent", state="running")
    _paused(w)
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched" and w.events_named("attempt_paused_for_limit")
    w.now += timedelta(hours=30)
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    usage.write_text(json.dumps({"seven_day": {"used_percentage": 3}}))
    _paused(w, on=False)
    w.now += timedelta(hours=1)
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"  # the clock restarted when the limit was out
    w.now += timedelta(hours=9)
    w.tick()
    assert w.store.task("ACME-1").reason.startswith("the agent hung")


def test_a_usage_limit_pause_neither_counts_toward_the_ceiling_nor_restarts_it(w):
    """`attempt_ceiling` counts the time an attempt could work: 20 h of work, a 30 h pause, then 5 h more is 25 h."""
    take(w)
    w.cfg.paths.bulava_state.mkdir(parents=True, exist_ok=True)
    usage = w.cfg.paths.bulava_state / "usage.json"
    w.now = datetime.now(UTC)
    _heartbeat(w)
    w.tick()
    for _ in range(4):
        w.now += timedelta(hours=5)
        _heartbeat(w)
        w.tick()
    usage.write_text(json.dumps({"seven_day": {"used_percentage": 100}}))
    _heartbeat(w, stage="agent", state="waiting", note="Вікно Claude вичерпано — продовжить, коли повернеться")
    _paused(w)
    w.tick()
    w.now += timedelta(hours=30)
    w.tick()
    w.restart()  # the pause so far is kept with the task
    w.tick()
    usage.write_text(json.dumps({"seven_day": {"used_percentage": 3}}))
    _paused(w, on=False)
    w.now += timedelta(minutes=1)
    _heartbeat(w)
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"  # 20 h of work: the pause did not count
    w.now += timedelta(hours=3)
    _heartbeat(w)
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"  # 23 h of work
    w.now += timedelta(hours=2)
    _heartbeat(w)
    w.tick()
    assert w.store.task("ACME-1").reason.startswith("the attempt ran out of time: no verdict in 24 h")  # nor restarted it


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
        findings=[{"kind": "note", "text": "**Claude Code:** /oauth/ is not proxied by the frontend's nginx"}]))
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
    """ACME-1: the verifier's own `make test` failed on a missing tool; the ticket must not say "inconclusive"."""
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "failed", review="debt", state_reason="debt", outcome={"result": "succeeded_changes"})
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(
        run_id="run-7", review_why="STATE: COMPLETE\nVERDICT: PASS\n\n[VERIFIER OVERRIDE] failed"))
    _verifier_evidence(w, "run-7", [
        {"criterion": "web: make test", "status": "fail", "exit_code": 2, "note": "",
         "artifact": "/elsewhere/instances/x/evidence/sid-1/web-make-test.log"},
        {"criterion": "Vitest passes", "status": "pass", "exit_code": 0, "note": ""},
    ], {"web-make-test.log": "pylint --errors-only ./x\nmake: pylint: No such file or directory\n"
                               "make: *** [Makefile:13: test-lint] Error 127\n"})
    w.tick()
    task = w.store.task("ACME-1")
    assert task.reason == "The change passed review, but the automated checks failed, so it was not published"
    [note] = [t for t in w.bot_comments() if "The change passed review" in t]
    assert "- `web: make test` failed (exit 2): make: pylint: No such file or directory" in note
    assert "Vitest" not in note and "inconclusive" not in note
    assert w.gl.mrs == []


def test_what_a_failed_check_printed_never_reaches_the_ticket_when_the_gate_stops_the_work(w):
    """The gate's own stop on failed checks names each check on the ticket by a neutral label and its exit code:
    its name (written by the agent or the project), command, note and log go only to the local event log."""
    _review_stopped(w, "")
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(review_why="[VERIFIER OVERRIDE] failed"))
    _verifier_evidence(w, "run-9", [
        {"criterion": "GET https://ci.internal.example/api with key sk-live-LEAKME answers", "status": "fail",
         "exit_code": 7, "command": "curl -H 'PRIVATE-TOKEN: glpat-LEAKME' https://ci.internal.example/api",
         "note": "401 with glpat-LEAKME from ci.internal.example", "artifact": "x/api.log"},
        {"status": "fail", "exit_code": 2, "command": "deploy --password=hunter2-LEAKME"},
    ], {"api.log": "error: token glpat-LEAKME rejected by ci.internal.example\n"})
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and task.reason.startswith("the project's automated checks kept failing")
    text = "\n".join(w.bot_comments())
    assert "- check 1 failed (exit 7)" in text and "- check 2 failed (exit 2)" in text
    assert "LEAKME" not in text and "internal.example" not in text and "curl" not in text and "GET" not in text
    first, second = w.events_named("verifier_gap")
    assert (first["check"], first["status"], first["exit_code"], first["run_id"], second["check"]) == \
        (1, "fail", 7, "run-9", 2)
    stream = w.cfg.paths.events.read_text()                      # what Alloy ships to Loki, line for line
    assert "LEAKME" not in stream and "internal.example" not in stream and "hunter2" not in stream


def test_a_cause_from_a_check_s_log_has_the_runner_s_tokens_taken_out_and_never_shows_the_command(w):
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "failed", review="debt", state_reason="debt", outcome={"result": "succeeded_changes"})
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(
        run_id="run-6", review_why="STATE: COMPLETE\nVERDICT: PASS\n\n[VERIFIER OVERRIDE] failed"))
    _verifier_evidence(w, "run-6", [
        {"status": "fail", "exit_code": 1, "command": f"git push https://bot:{GL_TOKEN}@gitlab/x.git",
         "artifact": "push.log"},
    ], {"push.log": f"fatal: Authentication failed for https://bot:{GL_TOKEN}@gitlab/x.git\n"})
    w.tick()
    [note] = [t for t in w.bot_comments() if "The change passed review" in t]
    assert "- check 1 failed (exit 1): fatal: Authentication failed" in note
    assert GL_TOKEN not in note and "git push" not in note


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


def png_header(width: int, height: int) -> bytes:
    """The start of a PNG: signature and IHDR, which is all the runner reads of it."""
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))


PNG = png_header(2880, 1800)  # a retina screenshot of a 1440×900 window
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
        {"file": "odd.png", "caption": "No size in its header", "repo": "acme-platform"},
    ], {"404.png": PNG, "retry.webm": WEBM, "theme.png": PNG + b"t", "fake.png": b"hello", "dup.png": PNG,
        "odd.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 32})
    w.settle("ACME-1", "waiting_review")
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"
    [mr] = w.gl.mrs
    names = [n.split("-", 1)[1] for _, n, _ in w.gl.uploads]
    assert names == ["404.png", "retry.webm", "odd.png"] and {p for p, _, _ in w.gl.uploads} == {mr["project_id"]}
    assert w.gl.uploads[0][2] == PNG
    desc = mr["description"]
    assert "## Screenshots and recordings" in desc and "![404 page, desktop](/uploads/" in desc
    # A screenshot is a preview within 480×320 (2880×1800 → 480 wide, 300 high); the recording keeps its player.
    assert re.search(r"!\[404 page, desktop\]\(/uploads/[^)]+-404\.png\)\{width=480\}", desc)
    assert re.search(r"-retry\.webm\)\n", desc)
    # A screenshot whose size its header does not give is a link to it: nothing unbounded in the page.
    assert re.search(r"\[Open the screenshot: No size in its header\]\(/uploads/[^)]+-odd\.png\)", desc)
    assert "![No size in its header]" not in desc
    assert "503, then Retry recovers" in desc and "Theme only" not in desc and "secret" not in desc
    [note] = [c for c in w.yt.comments["2-1"] if "Merge request" in c["text"]]
    attached = [n.split("-", 1)[1] for n, _ in w.yt.attachments[note["id"]]]
    assert attached == ["404.png", "retry.webm", "theme.png", "odd.png"]
    # The preview is the image alone: a link around it would make the click download the file too.
    assert re.search(r"(?<!\[)!\[404 page, desktop\]\(\w+-404\.png\)\{width=480\}(?!\]\()", note["text"])
    assert "![Theme only](" in note["text"] and "-retry.webm)\n" in note["text"]
    assert re.search(r"\[Open the screenshot: No size in its header\]\(\w+-odd\.png\)", note["text"])
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


def test_media_uploads_the_bytes_it_checked_not_what_the_path_holds_later(tmp_path):
    from arceus_runner import media
    folder = tmp_path / "media"
    folder.mkdir()
    secret = tmp_path / "secrets.env"
    secret.write_text("GITLAB_TOKEN=glpat-secret")
    (folder / "a.png").write_bytes(PNG)
    (folder / "media.json").write_text(json.dumps({"items": [{"file": "a.png", "caption": "A"}]}))
    [item], problems = media.load(folder)
    assert problems == []
    # The agent (or a process it left running) swaps the checked file for a link before the upload.
    (folder / "a.png").unlink()
    (folder / "a.png").symlink_to(secret)
    assert item.data() == PNG
    # And a link in its place at the moment of reading is never followed.
    with pytest.raises(OSError):
        media._read_plain(folder / "a.png", 1024)


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


def test_the_engine_never_gets_claude_check_settings_not_even_from_bulava_env(w):
    """Bulava 1.19.2 removed the Claude checks before Codex: a dispatch names none, and the event says
    nothing of them."""
    (w.tmp / "bulava.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-test\nSUPERVISOR_CLAUDE_CHECKS=1\n"
                                      "SUPERVISOR_CLAUDE_CHECKS_ROUNDS=5\n")
    take(w)
    [d] = w.calls("dispatch")
    assert not [k for k in d["env"] if k.startswith("SUPERVISOR_CLAUDE_CHECKS")]
    [e] = w.events_named("task_dispatched")
    assert not [k for k in e if k.startswith("claude_checks")]


def test_a_check_failure_from_an_older_engine_still_fails_the_task(w):
    """A task started on 1.19.1 can still end with its gate's "independent check failed". That path is
    gone from the runner: it reads as a stop of the review, never as a pass, and nothing is published."""
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "failed", review="failed", state_reason="needs-user")
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(
        review_why="independent check failed (security) and SUPERVISOR_CLAUDE_CHECKS_REQUIRED=1"))
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and task.reason == GATE_STOPPED
    assert w.remote_branch("agent/ACME-1-fix-login-page") is None and w.gl.mrs == []
    assert not [e for e in read_events(w.cfg.paths.events) if e["event"] == "required_check_missed"]


TEAM_BLOCKED = """
## 2026-11-16 17:52:03 — session s-1 — BLOCKED
STATE: BLOCKED
VERDICT: N/A
REVIEW TEAM: 3 of 3 reviewers answered (Contract, Security, State).

— Contract: BLOCKED —
The TL has to confirm on the ticket that the story may go ahead without a ledger row.

— Security: PASS —
Nothing found; the gate holds.

— State: BLOCKED —
Codex: the TL has to agree the superuser gate on the docs.
"""


def _review_stopped(w, note: str, run_id: str = "run-9", last_run: str | None = None) -> None:
    """The engine's record of a review that parked the attempt for a person, with the gate's note in its run folder."""
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "failed", review="failed", state_reason="needs-user",
             outcome={"result": "succeeded_changes", "summary": "Docs closed to everyone but a superuser."})

    def fn(s):
        t = s["tasks"]["ACME-1"]
        t["receipt"].update(run_id=run_id, review_why="")
        t["attempts"][-1]["run_id"] = last_run or run_id
    w.set_engine(fn)
    reports = w.cfg.paths.bulava_state / "runs" / run_id / "reports"
    reports.mkdir(parents=True)
    (reports / "blocked.md").write_text(note)


def test_a_review_that_stops_for_a_person_asks_on_the_ticket_and_a_plain_reply_goes_on(w):
    """GH-914: every reviewer that stopped the work asked for a decision. The ticket gets what they asked as a
    question with the needs-input tag, not a failure; a plain reply from an allowed person starts the next round."""
    _review_stopped(w, "\n## 2026-11-16 17:40:00 — session s-0\nReviewer unreachable once.\n" + TEAM_BLOCKED)
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "needs_input" and task.question_id is None
    assert "agent-needs-input" in w.yt.issue_tags["2-1"] and "agent-failed" not in w.yt.issue_tags["2-1"]
    [q] = [t for t in w.bot_comments() if "[arceus:question:1]" in t]
    assert "@j.doe" in q and "Nothing is published yet" in q
    assert "**Contract:** The TL has to confirm on the ticket" in q
    assert "**State:** the TL has to agree the superuser gate" in q  # the model's label taken off
    assert "Nothing found" not in q and "VERDICT" not in q and "Reviewer unreachable" not in q
    assert "Docs closed to everyone but a superuser." in q
    assert "could not take this ticket" not in "\n".join(w.bot_comments())
    assert w.gl.mrs == [] and w.remote_branch("agent/ACME-1-fix-login-page") is None
    [e] = w.events_named("task_needs_input")
    assert e["review"] is True

    w.yt.comment("2-1", "a.smith", "Go ahead without a ledger row; the superuser gate is agreed.")
    w.tick()
    [send] = w.calls("worker-send")
    assert "--answers" not in send["argv"] and send["argv"][-1].endswith("the superuser gate is agreed.")
    assert w.store.task("ACME-1").state == "dispatched"
    assert "agent-needs-input" not in w.yt.issue_tags["2-1"]


def test_a_park_for_a_person_that_is_not_the_review_s_block_stays_a_failure(w):
    """The gate's other notes in blocked.md (a reviewer it could not reach, rounds without progress) are no
    decision the reviewers asked for: the task fails as before."""
    _review_stopped(w, TEAM_BLOCKED + "\n## 2026-11-16 18:00:00 — session s-2\nReviewer unreachable 5 times.\n")
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and task.reason == GATE_STOPPED
    assert "agent-failed" in w.yt.issue_tags["2-1"] and "agent-needs-input" not in w.yt.issue_tags["2-1"]


STALLED = """
## 2026-11-22 20:31:15 — session s-1 (stalled: 5 findings, no progress for 2 rounds)
STATE: COMPLETE
"""
STALLED_WHY = """STATE: COMPLETE
VERDICT: FAIL
1. [acceptance_failure] health.txt: no version in the answer [repeat]

STILL OPEN FROM THE REVIEW (not confirmed fixed; ids are stable):
T1: [acceptance_failure] health.txt: no version in the answer
T2: [acceptance_failure] Codex: the test does not check the status code
2. [related_improvement] the README could mention the endpoint
3. [pre_existing] the logger is noisy
4. [harness_failure] the verifier could not start node"""


@pytest.mark.parametrize("review_why,note,reason,shown,hidden", [
    ("reviewer unreachable 5x: рецензент (codex) зупинився з кодом 1 at /home/arceus/.codex", "",
     "the reviewer (Codex) could not be reached, so the work was not reviewed", [], ["зупинився", "/home/arceus"]),
    ("uncommitted work after 3 requests to commit it: /srv/ws/acme-platform /srv/ws/acme-theme", "",
     "the agent left work uncommitted after being asked to commit it",
     ["Left uncommitted in: acme-platform, acme-theme."], ["/srv/ws"]),
    ("worker declared: failed", "", "the agent reported that it could not finish the task", [], []),
    (STALLED_WHY, STALLED, "the review kept finding the same problems, with no progress between rounds",
     ["- health.txt: no version in the answer", "- the test does not check the status code"],
     ["[repeat]", "T1:", "VERDICT", "Codex:", "README", "logger", "verifier could not start"]),
    (STALLED_WHY, "", "the review still found problems when its rounds ran out",   # the hard limit: no stalled note
     ["The review's findings still open:", "- health.txt: no version in the answer"], ["README", "T2:"]),
    ("declared 'succeeded_no_change' 3 times while the diff against base is non-empty", "", GATE_STOPPED, [],
     ["declared"]),
    ("STATE: COMPLETE\nVERDICT: PASS\n\n[FINAL CHECKS] Registered with add-check --final and not passed in this run; "
     "nothing is accepted without them.", STALLED,
     "the review accepted the work, but its final checks did not pass or left no readable result, so nothing was "
     "published", [], ["[FINAL CHECKS]", "add-check", "VERDICT"]),
])
def test_each_stop_of_the_review_gate_says_what_it_was(w, review_why, note, reason, shown, hidden):
    """`needs-user` is many different stops of Bulava's gate; the ticket names the one the receipt names, with only
    what the runner picked out of `review_why` (tests/test_real_gate.py runs the same stops through the real gate)."""
    _review_stopped(w, note)
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(review_why=review_why))
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed" and task.reason == reason
    text = "\n".join(w.bot_comments())
    assert f"could not take this ticket to a merge request: {reason}." in text
    assert "Docs closed to everyone but a superuser." in text
    assert all(x in text for x in shown), text
    assert not any(x in text for x in hidden), text
    assert "agent-failed" in w.yt.issue_tags["2-1"] and w.gl.mrs == []


@pytest.mark.parametrize("ledger,churned", [
    ({"stall": 2, "churn": 2}, True),
    ({"stall": 2, "churn": 1}, False),   # one round without progress brought nothing new: the same problems
    ({"stall": 2, "churn": 0}, False),
])
def test_a_review_parked_because_every_fix_brought_a_new_blocker_says_so(w, ledger, churned):
    """Bulava 1.20.5 parks a review whose open count does not fall because each fix opens a new blocker. Its gate note
    reads like any stall (blocked.md "(stalled: …)"); the task's review list says which, and the ticket says it too."""
    _review_stopped(w, STALLED)
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(review_why=STALLED_WHY))
    (w.cfg.paths.bulava_state / f"review-ledger-{task_key('ACME-1')}").write_text(json.dumps({"items": [], **ledger}))
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "failed"
    assert task.reason == ("the review stopped because every fix brought a new blocking problem, so a person has to "
                           "decide how to go on" if churned else
                           "the review kept finding the same problems, with no progress between rounds")
    text = "\n".join(w.bot_comments())
    assert "- health.txt: no version in the answer" in text and "[repeat]" not in text and "README" not in text


def test_a_churn_count_left_in_the_review_list_does_not_rename_another_stop(w):
    _review_stopped(w, "")
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(
        review_why="declared 'succeeded_no_change' 3 times while the diff against base is non-empty"))
    (w.cfg.paths.bulava_state / f"review-ledger-{task_key('ACME-1')}").write_text(
        json.dumps({"items": [], "stall": 2, "churn": 2}))
    w.tick()
    assert w.store.task("ACME-1").reason == GATE_STOPPED


def test_a_review_that_disputes_the_task_s_own_decision_asks_its_author(w):
    """[spec_conflict]: Bulava parks the run as needs_input with its own question, quoting the reviewer's line, which
    goes through the same redaction as any review text."""
    take(w)
    question = {"id": "spec-1a2b3c", "options": ["Keep the task as written", "Change it as the review suggests"],
                "text": "The review disputes a decision the task states explicitly. The agent has not changed it. "
                        "Which way should it go?\nQUOTE: \"compute_sudo=True\" — the token " + GL_TOKEN
                        + " leaks the iteration name"}
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="needs_input", state_reason="spec_conflict", attempt_live=False, question=question))
    w.tick()
    task = w.store.task("ACME-1")
    assert (task.state, task.question_id) == ("needs_input", "spec-1a2b3c")
    [q] = [t for t in w.bot_comments() if "[arceus:question:spec-1a2b3c:1]" in t]
    assert "QUOTE: \"compute_sudo=True\"" in q and GL_TOKEN not in q
    assert "1. Keep the task as written" in q and "2. Change it as the review suggests" in q
    assert "agent-needs-input" in w.yt.issue_tags["2-1"] and "agent-failed" not in w.yt.issue_tags["2-1"]
    w.yt.comment("2-1", "a.smith", "1")
    w.tick()
    [send] = w.calls("worker-send")
    assert send["argv"][send["argv"].index("--answers") + 1] == "spec-1a2b3c"
    assert send["argv"][-1] == "1. Keep the task as written"

    # The next attempt disputes the same words: Bulava gives the question the same id. It is asked again, once, in a
    # comment of its own, and the answer to the first one does not answer it.
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(state="needs_input", attempt_live=False,
                                                       state_reason="spec_conflict", question=question))
    w.tick()
    w.tick()
    assert len([t for t in w.bot_comments() if "[arceus:question:spec-1a2b3c:" in t]) == 2
    assert w.store.task("ACME-1").state == "needs_input" and len(w.calls("worker-send")) == 1
    w.yt.comment("2-1", "a.smith", "2")
    w.tick()
    sends = w.calls("worker-send")
    assert len(sends) == 2 and sends[-1]["argv"][-1] == "2. Change it as the review suggests"
    assert sends[-1]["argv"][sends[-1]["argv"].index("--answers") + 1] == "spec-1a2b3c"


def test_a_blocker_the_agent_filed_is_a_question_not_a_failure(w):
    """Bulava parks an attempt whose agent filed a `blocker` finding as `needs-user`; the agent waits for a person."""
    _review_stopped(w, "")
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(
        review_why="worker blocker finding: need the CI job's checkout SHA or a re-run of pipeline 900"))
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "needs_input" and "agent-needs-input" in w.yt.issue_tags["2-1"]
    [q] = [t for t in w.bot_comments() if "[arceus:question:" in t]
    assert "**What it needs:**" in q and "- need the CI job's checkout SHA or a re-run of pipeline 900" in q
    assert "could not take this ticket" not in "\n".join(w.bot_comments()) and w.gl.mrs == []
    w.yt.comment("2-1", "a.smith", "Re-run it, the runner cache was stale.")
    w.tick()
    [send] = w.calls("worker-send")
    assert send["argv"][-1].endswith("the runner cache was stale.") and w.store.task("ACME-1").state == "dispatched"


def _no_change(w, summary: str = "The endpoint already exists.") -> None:
    """The agent's first attempt changed nothing and said so: the engine banks it unreviewed, as debt."""
    take(w)
    w.settle("ACME-1", "failed", review="debt", state_reason="debt",
             outcome={"result": "succeeded_no_change", "summary": summary})
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(review_why=EMPTY_DIFF_WHY,
                                                                   codex={"status": "unreviewed"}))
    w.tick()


def test_no_code_change_on_a_new_ticket_is_a_decision_for_a_person_not_a_failure(w):
    _no_change(w)
    task = w.store.task("ACME-1")
    assert task.state == "needs_input" and task.no_change_asked
    tags = w.yt.issue_tags["2-1"]
    assert "agent-needs-input" in tags and "agent-failed" not in tags
    [q] = [t for t in w.bot_comments() if "[arceus:question:1]" in t]
    assert "found nothing to change" in q and "The endpoint already exists." in q
    assert "close the ticket (any state the project counts as resolved)" in q and "reply with a comment saying what" in q
    assert "move it out of" not in q
    assert "could not take this ticket" not in "\n".join(w.bot_comments()) and w.gl.mrs == []
    [e] = w.events_named("task_needs_input")
    assert e.get("no_change") is True


def test_a_tracker_failure_after_the_no_change_question_never_posts_it_twice(w):
    take(w)
    w.settle("ACME-1", "failed", review="debt", state_reason="debt",
             outcome={"result": "succeeded_no_change", "summary": "The endpoint already exists."})
    w.set_engine(lambda s: s["tasks"]["ACME-1"]["receipt"].update(review_why=EMPTY_DIFF_WHY,
                                                                   codex={"status": "unreviewed"}))
    w.yt.fail_once[("POST", "/api/issues/2-1/tags")] = 503        # the question is posted, the tag is not
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    w.tick()
    assert w.store.task("ACME-1").state == "needs_input" and "agent-needs-input" in w.yt.issue_tags["2-1"]
    assert len([t for t in w.bot_comments() if "[arceus:question:1]" in t]) == 1


def test_closing_the_ticket_accepts_the_no_change_conclusion_and_ends_the_task_as_done(w):
    _no_change(w)
    w.yt.issue_fields["2-1"]["State"] = {"name": "Done"}                         # resolved, as the tracker says
    w.yt.fail_once[("DELETE", "/api/issues/2-1/tags/t-agent-needs-input")] = 503   # the tracker blinks
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "done" and task.reason == 'no code changes: a person closed the ticket ("Done")'
    assert "agent-needs-input" in w.yt.issue_tags["2-1"]                          # left to the journal
    w.tick()
    assert "agent-needs-input" not in w.yt.issue_tags["2-1"] and "agent-failed" not in w.yt.issue_tags["2-1"]
    w.yt.comment("2-1", "j.doe", "thanks")
    w.tick()
    assert w.calls("worker-send") == [] and w.store.task("ACME-1").state == "done"


@pytest.mark.parametrize("state", ["Code Review", "Blocked", None])
def test_a_ticket_moved_to_an_open_state_or_with_no_state_keeps_waiting_and_a_reply_still_counts(w, state):
    """Only a ticket the tracker counts as resolved accepts the conclusion: another open state (even one the runner
    does not know) or no state at all is not an answer, and the person can still reply."""
    _no_change(w)
    if "Blocked" == state:
        w.yt.states.append("Blocked")
    w.yt.issue_fields["2-1"]["State"] = {"name": state} if state else None
    w.tick()
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "needs_input" and "agent-needs-input" in w.yt.issue_tags["2-1"]
    w.yt.comment("2-1", "j.doe", "It must also report the DB status.")
    w.tick()
    [send] = w.calls("worker-send")
    assert send["argv"][-1].endswith("must also report the DB status.")
    assert w.store.task("ACME-1").state == "dispatched"


def test_a_no_change_conclusion_waits_past_the_answer_deadline_and_a_reply_takes_the_work_on(w):
    _no_change(w)
    w.now = NOW + timedelta(days=30)
    w.tick()
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "needs_input" and "agent-failed" not in w.yt.issue_tags["2-1"]
    [reminder] = [t for t in w.bot_comments() if "[arceus:reminder:" in t]
    assert "close the ticket if you agree" in reminder and "fail" not in reminder
    w.yt.comment("2-1", "j.doe", "It exists, but it must also report the DB status.")
    w.tick()
    [send] = w.calls("worker-send")
    assert send["argv"][-1].endswith("must also report the DB status.")
    assert w.store.task("ACME-1").state == "dispatched"
    w.tick()
    assert "agent-needs-input" not in w.yt.issue_tags["2-1"]


def test_an_ordinary_question_keeps_its_deadline(w):
    """Only the no-change conclusion waits without a deadline; the agent's own question still fails unanswered."""
    _ask(w)
    assert not w.store.task("ACME-1").no_change_asked
    w.now = NOW + timedelta(days=6)
    w.tick()
    assert w.store.task("ACME-1").state == "failed"


def test_an_earlier_attempt_s_block_never_stands_for_this_one(w):
    _review_stopped(w, TEAM_BLOCKED, run_id="run-1", last_run="run-2")
    w.tick()
    assert w.store.task("ACME-1").state == "failed"


def test_a_stop_without_a_question_says_nothing_is_published_and_what_is_needed(w):
    """GH-1227: the agent declared `blocked` with no question in words. The ticket said only "the agent needs an
    answer" over its report, and a person redid the work believing the branch was lost."""
    take(w)
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="blocked", attempt_live=False, question=None,
        outcome={"result": "blocked", "summary": "Upgraded the libraries; checked locally."},
        receipt={"review": "failed", "findings": [
            {"kind": "note", "text": "a side remark"},
            {"kind": "blocker", "text": "Dev smoke can run only after the MR is merged and Dev is rebuilt."}]}))
    w.tick()
    [q] = [t for t in w.bot_comments() if "[arceus:question:1]" in t]
    assert "Nothing is published yet" in q and "Arceus publishes only work that passed review" in q
    assert "- Dev smoke can run only after the MR is merged" in q and "a side remark" not in q
    assert "Upgraded the libraries; checked locally." in q
    assert w.store.task("ACME-1").state == "needs_input"


def test_a_person_asked_by_codex_is_still_codex_s_decision(w):
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "failed", review="failed", state_reason="needs-user")
    w.tick()
    assert w.store.task("ACME-1").reason == GATE_STOPPED


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


# -- the agent's words reach the ticket whole ------------------------------------------------------

def _summary_with_the_ask_at_the_end() -> str:
    """A run summary as agents write them: what was done, how it was checked, and the request last."""
    checks = "\n".join(f"- check {i}: " + "y" * 60 for i in range(60))
    return (f"Three emails are sent now.\n\n### How it was checked\n{checks}\n\n### Limitations\n"
            "- Blocked on the mockups: post the text of Figma frames 12222-10748 and 12223-10758 on the ticket.")


def test_a_blocked_run_without_a_question_asks_with_its_whole_summary_and_screenshots(w):
    take(w)
    _media(w, [{"file": "email.png", "caption": "Admin email"}], {"email.png": PNG})
    summary = _summary_with_the_ask_at_the_end()
    assert len(summary) > 3000  # the old 1500-character cut lost the request at its end
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="blocked", state_reason="needs-user", attempt_live=False, question=None,
        outcome={"result": "blocked", "summary": summary}))
    w.tick()
    assert w.store.task("ACME-1").state == "needs_input"
    [note] = [c for c in w.yt.comments["2-1"] if "[arceus:question:1]" in c["text"]]
    text = note["text"]
    assert text.startswith("@j.doe, the agent needs an answer to continue:")
    assert "post the text of Figma frames 12222-10748 and 12223-10758 on the ticket." in text
    assert "…" not in text and "Part 1" not in text  # one comment, nothing cut
    assert "option's number" not in text  # there are no options to number
    assert text.index("![Admin email](") < text.index("Reply with a comment on this ticket.")
    assert text.endswith("Reply with a comment on this ticket.\n\n[arceus:question:1]")
    assert [n.split("-", 1)[1] for n, _ in w.yt.attachments[note["id"]]] == ["email.png"]


def test_a_long_question_goes_as_parts_in_order_and_is_answered_as_one(w):
    take(w)
    lines = [f"line {i:03d} " + "z" * 100 for i in range(250)]  # about 27 000 characters: three comments
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="needs_input", attempt_live=False,
        question={"id": "q-2", "text": "\n".join(lines), "options": ["Keep", "Drop"]}))
    w.tick()
    parts = [t for t in w.bot_comments() if "needs an answer" in t or "**Part " in t]
    assert len(parts) == 3
    assert [p.count(f"**Part {i} of 3**") for i, p in enumerate(parts, 1)] == [1, 1, 1]
    assert parts[0].startswith("@j.doe, the agent needs an answer to continue:")
    assert parts[0].endswith("[arceus:question:q-2]") and parts[2].endswith("[arceus:part3:question:q-2]")
    assert all("1. Keep" not in p for p in parts[:2]) and "1. Keep\n2. Drop" in parts[2]
    assert "the option's number or your own words." in parts[2]
    posted = "\n".join(parts)
    assert [ln for ln in posted.splitlines() if ln.startswith("line ")] == lines  # whole, in order
    # The task keeps it whole as well: the console's task page shows what the ticket got, not its first 1500 characters.
    kept = json.loads(w.store.task("ACME-1").question)
    assert kept["text"] == "\n".join(lines) and kept["options"] == ["Keep", "Drop"] and kept["id"] == "q-2"
    w.tick()
    assert len([t for t in w.bot_comments() if "needs an answer" in t or "**Part " in t]) == 3  # posted once
    w.yt.comment("2-1", "a.smith", "1")
    w.tick()
    [send] = w.calls("worker-send")
    assert send["argv"][send["argv"].index("--answers") + 1] == "q-2" and send["argv"][-1] == "1. Keep"


def test_long_failure_details_survive_an_outage_whole_in_order_and_keep_the_numbering(w):
    take(w)
    details = "\n".join(f"step {i:03d} " + "d" * 100 for i in range(150))  # about 16 000 characters: two comments
    w.yt.fail_once[("POST", "/api/issues/2-1/comments")] = 503
    w.runner.delivery.fail(w.store.task("ACME-1"), "the review budget ran out", details)
    assert not [t for t in w.bot_comments() if "could not take this ticket" in t]
    w.clock.advance(3600)
    w.tick()
    parts = [t for t in w.bot_comments() if "could not take this ticket" in t or "**Part 2 of 2**" in t]
    assert len(parts) == 2 and "**Part 1 of 2**" in parts[0] and parts[1].endswith("[arceus:part2:failed:n1]")
    assert [ln for ln in "\n".join(parts).splitlines() if ln.startswith("step ")] == details.splitlines()
    assert "/continue" not in parts[0] and "/continue" in parts[1]  # what to do next comes after everything
    assert w.store.count_actions("ACME-1:comment:failed:") == 1  # the parts do not count as failures
    w.runner.delivery.fail(w.store.task("ACME-1"), "a later failure")
    assert w.bot_comments()[-1].endswith("[arceus:failed:n2]")


def test_a_screenshot_shown_with_the_question_is_not_attached_again_when_the_work_is_done(w):
    """The question and the finished work read the same media folder: the ticket gets each file once, the
    result comment brings only what changed since the question, and the MR still shows every screenshot."""
    take(w)
    folder = _media(w, [{"file": "email.png", "caption": "Admin email, draft"}], {"email.png": PNG})
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="needs_input", attempt_live=False,
        question={"id": "q-1", "text": "Which wording do the mockups use?", "options": []}))
    w.tick()
    [q] = [c for c in w.yt.comments["2-1"] if "[arceus:question:q-1]" in c["text"]]
    assert [n.split("-", 1)[1] for n, _ in w.yt.attachments[q["id"]]] == ["email.png"]

    w.yt.comment("2-1", "a.smith", "Use the text from frame 12222")
    w.tick()
    (folder / "final.png").write_bytes(PNG + b"final")  # the email as built after the answer
    (folder / "media.json").write_text(json.dumps({"items": [
        {"file": "email.png", "caption": "Admin email, draft"},
        {"file": "final.png", "caption": "Admin email, final"}]}))
    w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"
    [note] = [c for c in w.yt.comments["2-1"] if "Merge request" in c["text"]]
    assert [n.split("-", 1)[1] for n, _ in w.yt.attachments[note["id"]]] == ["final.png"]
    assert "Admin email, final" in note["text"] and "Admin email, draft" not in note["text"]
    attached = [n.split("-", 1)[1] for files in w.yt.attachments.values() for n, _ in files]
    assert attached == ["email.png", "final.png"]  # each file on the ticket once
    desc = w.gl.mrs[0]["description"]
    assert "![Admin email, draft](/uploads/" in desc and "![Admin email, final](/uploads/" in desc


def _merge_tag(w, who: str) -> None:
    w.yt.issue_tags["2-1"].add("agent-merge")
    w.yt.activities["2-1"].append({"id": "m1", "timestamp": 99, "author": {"login": who},
                                   "added": [{"name": "agent-merge"}], "removed": []})


def test_the_merge_tag_sets_the_reviewed_mr_to_merge_when_green(w):
    take(w)
    _merge_tag(w, "j.doe")
    sha = w.agent_commits()
    w.gl.head_sha = sha
    w.settle("ACME-1", "waiting_review")
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"
    [(iid, body)] = w.gl.merge_requests
    assert iid == w.gl.mrs[0]["iid"] and body["sha"] == sha
    assert body["auto_merge"] is True and body["merge_when_pipeline_succeeds"] is True
    assert any("as soon as its pipeline passes" in t and "j.doe" in t for t in w.bot_comments())
    assert w.events_named("merge_when_green")
    w.tick()
    assert len(w.gl.merge_requests) == 1


def test_no_merge_without_the_tag(w):
    take(w)
    w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    w.tick()
    assert w.store.task("ACME-1").state == "code_review" and w.gl.merge_requests == []


def test_the_merge_tag_from_someone_not_allowed_merges_nothing(w):
    take(w)
    _merge_tag(w, "stranger")
    w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    w.tick()
    assert w.gl.merge_requests == []
    assert any("stranger" in t and "nothing was merged" in t for t in w.bot_comments())
    assert w.events_named("merge_refused")


def test_a_refused_merge_is_said_on_the_ticket(w):
    take(w)
    _merge_tag(w, "j.doe")
    w.gl.head_sha = w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    w.gl.merge_refusal = "Branch cannot be merged"
    w.tick()
    assert w.store.task("ACME-1").state == "code_review" and len(w.gl.merge_requests) == 1
    assert any("not set to merge" in t and "Branch cannot be merged" in t for t in w.bot_comments())
    assert w.events_named("merge_when_green_refused")
    w.tick()
    assert len(w.gl.merge_requests) == 1


def test_an_mr_with_commits_the_review_did_not_see_is_not_merged(w):
    take(w)
    _merge_tag(w, "j.doe")
    w.agent_commits()
    w.gl.head_sha = "f" * 40
    w.settle("ACME-1", "waiting_review")
    w.tick()
    assert w.gl.merge_requests == []
    assert any("commits the review did not see" in t for t in w.bot_comments())


def test_an_mr_into_another_branch_is_not_merged(w):
    take(w)
    _merge_tag(w, "j.doe")
    w.gl.head_sha = w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    real = w.runner.delivery.host.mr_status
    w.runner.delivery.host.mr_status = lambda repo_id, iid: __import__("dataclasses").replace(
        real(repo_id, iid), target="release/1.2")
    w.tick()
    assert w.gl.merge_requests == []
    assert any("not the default branch" in t for t in w.bot_comments())


def test_a_merge_the_host_could_not_take_now_is_tried_again(w):
    take(w)
    _merge_tag(w, "j.doe")
    w.gl.head_sha = w.agent_commits()
    w.settle("ACME-1", "waiting_review")
    host = w.runner.delivery.host
    real = host.merge_when_green
    calls = []

    def unwell_once(*a):
        calls.append(a)
        if len(calls) == 1:
            from arceus_runner.http import ServiceError
            raise ServiceError("gitlab", "503 Service Unavailable", 503)
        return real(*a)
    host.merge_when_green = unwell_once
    w.tick()
    assert w.store.task("ACME-1").state == "code_review" and w.gl.merge_requests == []
    w.tick()
    assert len(w.gl.merge_requests) == 1 and len(calls) == 2


def test_the_example_notes_and_instructions_give_the_agent_no_words_about_a_team():
    """The templates in config/ reach the agent as their loaders read them, the guidance in comments left out."""
    from arceus_runner.texts import project_notes
    from arceus_runner.workspace_run import read_instructions
    examples = Path(__file__).resolve().parents[2] / "config"
    assert not org_words(project_notes(examples / "project-notes.example.md"))
    for name in ("architect-instructions.example.md", "secops-instructions.example.md", "qa-instructions.example.md"):
        text, _ = read_instructions(examples / name, required=False)
        assert not org_words(text or ""), name
