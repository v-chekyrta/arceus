"""The tracker and the code host as interfaces: the runner's flow with providers it has never heard of,
the old config layout, the database bound to its integration, and nothing in the flow that knows
YouTrack or GitLab."""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from arceus_runner import loop
from arceus_runner.cli import EXIT_CHECK_FAILED, EXIT_CONFIG, EXIT_OK, main
from arceus_runner.config import Config, ConfigError, load_config
from arceus_runner.engine import Engine
from arceus_runner.gitops import Git, GitAuth
from arceus_runner.integrations import build_code_host, build_tracker

from .conftest import EXAMPLE, example_raw
from .memory_providers import MemoryHost, MemoryTracker
from .test_delivery import World, git

SRC = Path(loop.__file__).parent
LEGACY = Path(__file__).parent / "data" / "config.legacy.yaml"  # config.example.yaml before tracker:/code_host:
DEBOUNCE = timedelta(minutes=6)
MEMORY = "tests.memory_providers"


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])


def memory_sections(raw: dict) -> dict:
    """The example config with the in-memory tracker and code host in place of YouTrack and GitLab."""
    t, h = raw["tracker"], raw["code_host"]
    raw["tracker"] = {"type": MEMORY, "bot_user": "arceus-bot", "allowed_users": t["allowed_users"],
                      "tags": t["tags"], "states": t["states"]}
    raw["code_host"] = {"type": MEMORY, "bot_user": "arceus-bot", "git_author": h["git_author"],
                        "exclude_repos": h.get("exclude_repos", [])}
    return raw


class MemoryWorld(World):
    """test_delivery's world (fake Bulava, real git with local origins) on the in-memory providers."""

    def __init__(self, tmp: Path, secrets):
        super().__init__(tmp, secrets)
        self.cfg = Config.model_validate(memory_sections(self.cfg.model_dump(by_alias=True)))
        self.tracker = build_tracker(self.cfg, secrets)
        self.host = build_code_host(self.cfg, secrets)
        assert isinstance(self.tracker, MemoryTracker) and isinstance(self.host, MemoryHost)
        self.tracker.client.clock = self.host.client.clock = self.clock
        self.host.urls = {name: str(origin) for name, origin in self.origins.items()}
        self.runner = self._runner()

    def _runner(self) -> loop.Runner:
        if not hasattr(self, "host"):
            return super()._runner()
        g = Git(GitAuth("https://host.test", "arceus-bot", "t"), "ACME Agent", "bot@example.com")
        return loop.Runner(self.cfg, self.store, self.events, self.tracker, self.host, now=lambda: self.now,
                           mono=self.clock, engine=Engine(self.cfg, extra_env=self.engine_env), git=g,
                           classify=self.screen)

    def ticket(self, key: str = "ACME-1"):
        return self.tracker._ticket(key)

    def bot_says(self, key: str = "ACME-1") -> list[str]:
        return [c.text for c in self.ticket(key).comments if c.author == "arceus-bot"]

    def later(self, delta: timedelta = DEBOUNCE) -> None:
        self.now += delta
        self.tick()


@pytest.fixture
def m(tmp_path, secrets):
    world = MemoryWorld(tmp_path, secrets)
    yield world
    world.events.close()
    world.store.close()


def argv_flag(call: dict, flag: str) -> str:
    return call["argv"][call["argv"].index(flag) + 1]


def test_a_ticket_goes_all_the_way_to_merged_on_providers_the_runner_does_not_know(m):
    c = m.cfg.tracker
    m.tracker.file(1, "Fix login page", queued_by="j.doe")
    m.tick()
    task = m.store.task("ACME-1")
    assert (task.state, task.issue_db_id, task.branch) == ("dispatched", "tkt-0001", "agent/ACME-1-fix-login-page")
    [d] = m.calls("dispatch")
    assert argv_flag(d, "--source") == "tracker" and argv_flag(d, "--author") == "j.doe"
    assert m.ticket().state == c.states.in_progress and m.ticket().assignee == "arceus-bot"
    assert "of organization `acme`" in m.bot_says()[0]
    home = (m.cfg.paths.workspace / "AGENTS.md").read_text()
    assert home.startswith("# Memory Host organization `acme`") and "acme-infra" not in home

    # Published: the ticket comment's response is lost after the tracker stored it, and it is not posted twice.
    sha = m.agent_commits()
    m.settle("ACME-1", "waiting_review", outcome={"summary": "Fixed the login page"})
    m.tracker.lose_response.add("add_comment")
    m.tick()
    m.tick()
    task = m.store.task("ACME-1")
    assert task.state == "code_review" and m.remote_branch(task.branch) == sha
    [pr] = m.host.prs
    assert (pr.repo_id, pr.iid, pr.source, pr.label) == ("00042", "001", task.branch, "agent")
    assert "Ticket: [ACME-1](https://tracker.test/browse/ACME-1)" in pr.description
    assert len([t for t in m.bot_says() if "Merge request:" in t]) == 1
    assert m.ticket().state == c.states.code_review
    m.restart()  # the ids come back from the database as the host gave them, not as 42 and 1
    [stored] = m.store.task_mrs("ACME-1")
    assert (stored.project_id, stored.iid) == ("00042", "001")

    # A review thread that mentions the bot the way this host does; a note without the mention wakes nothing.
    thread = m.host.comment(pr, "reviewer", "[~arceus-bot] please rename the handler")
    m.host.comment(pr, "reviewer", "@arceus-bot is not how this host mentions anyone")
    m.tick()
    m.later()
    [send] = m.calls("worker-send")
    assert argv_flag(send, "--source") == "mr" and argv_flag(send, "--author") == "reviewer"
    assert "please rename the handler" in send["argv"][-1] and "is not how this host" not in send["argv"][-1]
    assert "you cannot post in Memory Host yourself" in send["argv"][-1]
    assert m.ticket().state == c.states.in_progress

    # The fix is published to the same MR; the thread's answer survives a lost response without a second reply.
    fix = m.agent_commits("fix.txt")
    m.settle("ACME-1", "waiting_review", outcome={"summary": "Renamed it"})
    m.host.lose_response.add("reply")
    m.tick()
    m.tick()
    assert m.store.task("ACME-1").state == "code_review" and len(m.host.prs) == 1
    replies = [n for n in pr.threads[thread] if n.author == "arceus-bot"]
    assert len(replies) == 1 and fix[:12] in replies[0].body

    # Red CI on the published commit, with a string run id and no job logs on this host.
    pr.ci = {"id": "00077", "sha": fix, "status": "failed", "url": "https://host.test/runs/77"}
    m.tick()
    m.restart()
    assert m.store.task_mrs("ACME-1")[0].handled_pipeline == "00077"
    m.later()
    ci = m.calls("worker-send")[-1]
    assert argv_flag(ci, "--source") == "ci" and "https://host.test/runs/77" in ci["argv"][-1]
    m.restart()
    m.later()
    assert len([s for s in m.calls("worker-send") if argv_flag(s, "--source") == "ci"]) == 1
    assert [e["pipeline"] for e in m.events_named("review_signal") if e.get("kind") == "ci"] == ["00077"]
    green = m.agent_commits("ci.txt")
    m.settle("ACME-1", "waiting_review", outcome={"summary": "Fixed the test"})
    m.tick()
    pr.ci = {"id": "00078", "sha": green, "status": "success"}
    m.later()
    assert m.store.task("ACME-1").ci_attempts == 0

    # Merged: the task is done.
    pr.state = "merged"
    m.later()
    assert m.store.task("ACME-1").state == "done"
    assert any("All merge requests of this task are merged" in t for t in m.bot_says())


def test_a_trigger_the_tracker_cannot_attribute_is_never_taken(m):
    m.tracker.file(1, "No history", queued_by=None)
    m.tracker._ticket("ACME-1").tags.add(m.cfg.tracker.tags.queue)  # tagged, and no history says by whom
    m.tracker.file(2, "Nameless", queued_by="")
    m.tracker.file(3, "Not on the list", queued_by="mallory")
    m.tick()
    m.tick()
    assert m.store.tasks() == [] and m.calls("dispatch") == []
    assert [e["task_id"] for e in m.events_named("trigger_unknown")] == ["ACME-1"]
    rows = sqlite3.connect(m.cfg.paths.db).execute(
        "SELECT issue_key, author, accepted FROM trigger_decisions ORDER BY issue_key").fetchall()
    assert rows == [("ACME-2", "", 0), ("ACME-3", "mallory", 0)]
    assert any("set by someone the tracker does not name" in t for t in m.bot_says("ACME-2"))
    assert any("set by mallory" in t for t in m.bot_says("ACME-3"))


@pytest.mark.parametrize("by,taken_by", [("mallory", None), (None, None), ("a.smith", "a.smith")])
def test_an_approval_is_not_inherited_by_whoever_tags_the_ticket_again(m, by, taken_by):
    """j.doe's tag was approved; before the runner got to it the tag was set again. Only the newest tag counts."""
    m.tracker.file(1, "Fix login page", queued_by="j.doe")
    m.runner.poll_tracker()  # approved, not taken yet
    m.tracker.retag("ACME-1", by)
    m.tick()
    m.tick()
    task = m.store.task("ACME-1")
    if taken_by is None:
        assert task is None and m.calls("dispatch") == []
        assert m.events_named("task_not_taken") or m.events_named("trigger_rejected")
    else:
        assert task.tag_author == taken_by and len(m.calls("dispatch")) == 1


def test_the_newest_decision_of_an_issue_decides(m):
    store = m.store
    store.record_trigger("ACME-1", "old", "j.doe", True, 1)
    store.record_trigger("ACME-1", "new", "mallory", False, 2)
    store.record_trigger("ACME-2", "one", "j.doe", True, 3)
    assert [(d.issue_key, d.tag_activity_id) for d in store.accepted_untaken()] == [("ACME-2", "one")]


def test_ids_that_would_run_together_in_a_key_stay_two_signals(m):
    """("x-1", "9") and ("x", "1-9") joined with "-" are one key; escaped, they are two, also after a restart."""
    m.host.repo_ids.update({"acme-platform": "x-1", "acme-theme": "x"})
    m.tracker.file(1, "Fix login page", queued_by="j.doe")
    m.tick()
    m.agent_commits()
    theme = m.cfg.paths.workspace / "acme-theme"
    (theme / "theme.css").write_text("a{}\n")
    git(theme, "add", "theme.css")
    git(theme, "commit", "-q", "-m", "theme")
    m.settle("ACME-1", "waiting_review")
    m.tick()
    prs = {p.repo_id: p for p in m.host.prs}
    assert set(prs) == {"x-1", "x"}
    m.host.comment(prs["x-1"], "reviewer", "[~arceus-bot] platform question", note_id="9")
    m.host.comment(prs["x"], "reviewer", "[~arceus-bot] theme question", note_id="1-9")
    for repo_id, run in (("x-1", "5"), ("x", "1-5")):
        shas = {r.repo: r.published_sha for r in m.store.task_mrs("ACME-1")}
        sha = shas["acme-platform" if repo_id == "x-1" else "acme-theme"]
        prs[repo_id].ci = {"id": run, "sha": sha, "status": "failed", "url": f"https://host.test/runs/{repo_id}/{run}"}
    m.tick()
    rows = sqlite3.connect(m.cfg.paths.db).execute("SELECT id FROM signals ORDER BY id").fetchall()
    # ("x-1", "9") and ("x", "1-9"): both "note-x-1-9" unescaped
    assert {r[0] for r in rows} == {"note-x%2D1-9", "note-x-1%2D9", "pipeline-x%2D1-5", "pipeline-x-1%2D5"}
    m.restart()
    m.later()
    [send] = m.calls("worker-send")
    text = send["argv"][-1]
    assert "platform question" in text and "theme question" in text
    assert "runs/x-1/5" in text and "runs/x/1-5" in text


def test_keys_of_gitlab_numbers_are_the_ones_already_stored():
    from types import SimpleNamespace as NS

    from arceus_runner import review
    from arceus_runner.integrations import key_part
    mr = NS(project_id=12, iid=7)
    assert (review._note_signal(mr, 34), review._pipeline_signal(mr, 900), review._closed_id(mr),
            review._conflict_signal(mr, "ab12")) == ("note-12-34", "pipeline-12-900", "closed-12-7", "conflict-12-ab12")
    assert key_part("4-123", keep_dash=True) == "4-123"  # a YouTrack comment id in a `:` key, as stored before
    pairs = [(NS(project_id="a-b", iid="c"), NS(project_id="a", iid="b-c")),
             (NS(project_id="a:b", iid="c"), NS(project_id="a", iid="b:c"))]
    for one, other in pairs:
        assert review._closed_id(one) != review._closed_id(other)
        assert review._note_signal(one, "c") != review._note_signal(NS(project_id="a"), "b-c")
        assert review._pipeline_signal(one, "c") != review._pipeline_signal(NS(project_id="a"), "b-c")
        assert review._conflict_signal(one, "c") != review._conflict_signal(NS(project_id="a"), "b-c")
    assert key_part("5-plain", keep_dash=True) + ":plain" != key_part("5", keep_dash=True)
    assert ":" not in key_part("5:plain", keep_dash=True)


# -- the runner's flow knows no provider ------------------------------------------------------------

CORE = ["loop.py", "delivery.py", "review.py", "screening.py", "cleanup.py", "checks.py", "metrics.py", "health.py",
        "progress.py", "engine.py", "db.py", "cli.py", "events.py", "gitops.py", "media.py", "replies.py"]


@pytest.mark.parametrize("name", CORE)
def test_the_flow_does_not_reach_into_a_provider(name):
    tree = ast.parse((SRC / name).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in ("arceus_runner.youtrack", "arceus_runner.gitlab"), f"{name}: {node.module}"
        if isinstance(node, ast.Import):
            assert not {a.name for a in node.names} & {"arceus_runner.youtrack", "arceus_runner.gitlab"}, name
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("youtrack", "gitlab", "yt", "gl"), f"{name}:{node.lineno}: .{node.attr}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "detailed_merge_status" not in node.value and "/api/v4" not in node.value, f"{name}:{node.lineno}"


# -- config -----------------------------------------------------------------------------------------


def test_the_config_before_tracker_and_code_host_loads_as_the_same_settings():
    example = load_config(EXAMPLE).model_dump()
    example["paths"]["project_notes"] = None  # a setting newer than that layout
    assert load_config(LEGACY).model_dump() == example


def legacy_problems(tmp_path, change) -> list[str]:
    raw = yaml.safe_load(LEGACY.read_text())
    change(raw)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert not [p for p in e.value.problems if re.search(r"\b(tracker|code_host|bot_user)\b", p)]
    return e.value.problems


def test_errors_in_the_old_layout_name_its_own_keys(tmp_path):
    def two(raw):
        del raw["youtrack"]["states"]["in_progress"]
        raw["gitlab"]["bot_username"] = ""
    assert sorted(legacy_problems(tmp_path, two)) == [
        "gitlab.bot_username: String should have at least 1 character", "youtrack.states.in_progress: required"]
    assert legacy_problems(tmp_path, lambda raw: raw["youtrack"]["allowed_users"].append("agent-runner")) == [
        "youtrack: allowed_users must not contain the bot itself (agent-runner)"]
    [gone] = legacy_problems(tmp_path, lambda raw: raw["gitlab"].update(default_repo="acme-theme"))
    assert gone.startswith("gitlab: gitlab.default_repo is no longer used")


def test_check_says_the_old_layout_still_works_but_is_old(tmp_path, capsys):
    raw = yaml.safe_load(LEGACY.read_text())
    secrets = tmp_path / "secrets.env"
    secrets.write_text("YOUTRACK_TOKEN=a\nGITLAB_TOKEN=b\nSLACK_BOT_TOKEN=c\n")
    secrets.chmod(0o600)
    raw["paths"] = {"secrets": str(secrets)}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert main(["--config", str(path), "check", "--offline"]) == EXIT_OK
    assert "[WARN] config.layout: the youtrack: and gitlab: sections" in capsys.readouterr().out


def test_check_shows_the_project_notes_and_fails_on_a_missing_file(tmp_path, capsys):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("YOUTRACK_TOKEN=a\nGITLAB_TOKEN=b\nSLACK_BOT_TOKEN=c\n")
    secrets.chmod(0o600)
    notes = tmp_path / "project-notes.md"
    raw = example_raw()
    raw["paths"] = {"secrets": str(secrets), "project_notes": str(notes)}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert main(["--config", str(path), "check", "--offline"]) == EXIT_CHECK_FAILED
    assert (f"[FAIL] config.project_notes: paths.project_notes: {notes} does not exist; no task starts until it "
            "is fixed") in capsys.readouterr().out
    notes.write_text("<!-- guidance -->\nUse pnpm.\n")
    main(["--config", str(path), "check", "--offline"])
    assert f"config.project_notes: {notes}: 9 of 8192 bytes" in capsys.readouterr().out


@pytest.mark.parametrize("section,value,expected", [
    ("tracker", "jira", "tracker: type: unknown an issue tracker 'jira'"),
    ("code_host", "json", "code_host: type: module 'json' does not define CODE_HOST, so it cannot be used as a "
                          "code host"),
    ("tracker", "../evil", "tracker: type: '../evil' is not the name of an issue tracker"),
])
def test_a_type_that_is_not_a_provider_is_refused_at_start(tmp_path, section, value, expected):
    raw = example_raw()
    raw[section]["type"] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert any(p.startswith(expected) for p in e.value.problems), e.value.problems


def test_a_section_without_type_or_with_a_key_of_another_provider_is_refused(tmp_path):
    raw = example_raw()
    del raw["tracker"]["type"]
    raw["code_host"]["project"] = "ACME"  # a YouTrack key in the GitLab section
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert sorted(e.value.problems) == ["code_host.project: unknown key (typo?)",
                                       "tracker: type: required: which issue tracker this is, e.g. youtrack"]


def test_a_provider_module_by_path_is_validated_by_its_own_section():
    raw = memory_sections(example_raw())
    raw["tracker"]["base_url"] = "https://tracker.example"
    cfg = Config.model_validate(raw)
    assert type(cfg.tracker).__name__ == "MemoryTrackerConfig" and cfg.tracker.base_url == "https://tracker.example"
    assert cfg.tracker.token_env == "MEMORY_TRACKER_TOKEN" and cfg.code_host.token_env == "MEMORY_HOST_TOKEN"


# -- the database belongs to one integration --------------------------------------------------------


def test_runner_db_refuses_another_tracker_and_rebind_takes_a_new_address(cfg, secrets, monkeypatch, tmp_path,
                                                                          capsys):
    from .fakes import FakeGitLab, FakeYouTrack
    from .conftest import providers

    def built(c, s):
        return providers(c, s, FakeYouTrack(), FakeGitLab())

    monkeypatch.setattr(loop, "build_tracker", lambda c, s: built(c, s)[0])
    monkeypatch.setattr(loop, "build_code_host", lambda c, s: built(c, s)[1])
    assert loop.run(cfg, secrets, once=True) == 0
    moved = cfg.model_copy(update={"tracker": cfg.tracker.model_copy(update={"url": "https://yt.new.example"})})
    with pytest.raises(loop.IntegrationChanged, match="belongs to tracker 'youtrack https://youtrack.example.com "
                                                      "project ACME'"):
        loop.run(moved, secrets, once=True)
    names = [json.loads(x)["event"] for x in cfg.paths.events.read_text().splitlines()]
    assert names.count("runner_started") == 1  # refused before anything ran

    raw = example_raw()
    raw["tracker"]["url"] = "https://yt.new.example"
    raw["metrics"] = {"port": 0}
    raw["paths"] = {k: str(v) for k, v in cfg.paths.model_dump().items()}
    (tmp_path / "secrets.env").write_text(f"YOUTRACK_TOKEN={secrets.tracker}\nGITLAB_TOKEN={secrets.code_host}\n"
                                          "SLACK_BOT_TOKEN=x\n")
    (tmp_path / "secrets.env").chmod(0o600)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert main(["--config", str(path), "rebind", "tracker"]) == EXIT_OK
    assert "-> youtrack https://yt.new.example project ACME" in capsys.readouterr().out
    assert loop.run(moved, secrets, once=True) == 0

    raw["code_host"]["group"] = "clients/other"
    path.write_text(yaml.safe_dump(raw))
    assert main(["--config", str(path), "run", "--once", "--skip-checks"]) == EXIT_CONFIG
    assert "belongs to code_host 'gitlab https://gitlab.example.com group clients/acme'" in capsys.readouterr().err


def test_the_upgrade_keeps_gitlab_numbers_and_stores_any_id_as_given(tmp_path):
    from arceus_runner.db import MIGRATIONS, ReviewStore, _split
    path = tmp_path / "runner.db"
    conn = sqlite3.connect(path)
    for n in range(12):  # the database as the runner before opaque ids left it
        for stmt in _split(MIGRATIONS[n]):
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {n + 1}")
    conn.execute("INSERT INTO task_mrs (task_id, repo, project_id, iid, url, published_sha, handled_pipeline) "
                 "VALUES ('ACME-1', 'acme-platform', 12, 7, 'u', 's', 900)")
    conn.commit()
    conn.close()
    store = ReviewStore(path)
    store.upsert_mr("ACME-2", "acme-theme", "00042", "1e3", "u", "s")
    store.update_mr("ACME-2", "acme-theme", handled_pipeline="00077")
    store.close()
    store = ReviewStore(path)
    try:
        [old] = store.task_mrs("ACME-1")
        [new] = store.task_mrs("ACME-2")
        assert (old.project_id, old.iid, old.handled_pipeline) == (12, 7, 900)
        assert (new.project_id, new.iid, new.handled_pipeline) == ("00042", "1e3", "00077")
    finally:
        store.close()


# -- dashboards -------------------------------------------------------------------------------------


def test_dashboards_read_the_services_by_role_not_by_provider():
    now = json.loads((SRC.parents[2] / "observability" / "grafana" / "dashboards" / "arceus-now.json").read_text())
    exprs = [t.get("expr", "") for p in now["panels"] for t in p.get("targets", [])]
    services = [e for e in exprs if "runner_service_up" in e]
    assert sorted(services) == ['runner_service_up{role="code_host"}', 'runner_service_up{role="tracker"}']
    assert not [e for e in exprs if 'service="youtrack"' in e or 'service="gitlab"' in e]


def test_the_workspace_git_of_the_example_still_names_the_host(m):
    """A provider's label and scope reach the agent's notes and the commit that writes them."""
    m.tracker.file(1, "Fix login page", queued_by="j.doe")
    m.tick()
    assert git(m.cfg.paths.workspace, "log", "-1", "--format=%s") == "Arceus: workspace of Memory Host organization acme"
