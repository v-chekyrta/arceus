"""A task left waiting loses what can be made again (cleanup.release_after), then comes back through its real
return paths: the runner's own loop, ticket and MR fakes, the fake engine and git. The agent's part (making the
removed things again, or saying it cannot) is played by the test, through the same Docker the cleanup reads."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from arceus_runner.cleanup import Cleanup
from arceus_runner.gitops import Git

from .test_cleanup import FakeDocker
from .test_delivery import World, take
from .test_review import fix_and_settle, later, published, sends

GONE = {"arceus.disposable": "true"}


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])


@pytest.fixture
def w(tmp_path, secrets):
    world = World(tmp_path, secrets, settings={"cleanup": {"mode": "act", "release_after": "3d"}})
    world.docker = FakeDocker()
    with_cleanup(world)
    yield world
    world.events.close()
    world.store.close()


def with_cleanup(w: World) -> None:
    """The runner's own cleanup, on the fake Docker, the runner's engine and the test's clocks."""
    w.runner.cleanup = Cleanup(w.cfg, w.store, w.events, docker=w.docker, git=Git(None, "A", "a@a"),
                               engine=w.runner.engine, now=lambda: w.now, mono=w.clock, procs=lambda roots: [],
                               kill=lambda p: "done")


def agent_makes(w: World, key: str) -> None:
    """What the agent starts for its checks while it holds the workspace: a seeded test database (marked as made
    again by its script), and the reproduction of the reported bug (unmarked: the only copy)."""
    w.docker.add("acme1-db", created=w.now, finished=w.now, labels={"arceus.task": key, **GONE})
    w.docker.add_volume("acme1-testdb", {"arceus.task": key, **GONE}, created=w.now)
    w.docker.add("acme1-repro", created=w.now, finished=w.now, labels={"arceus.task": key})


def removed_in(note: str) -> set[str]:
    """The names a runner note lists, as an agent reads them."""
    listed = re.search(r"removed what can be made again: (.*?)\. Your branch", note).group(1)
    return {n for part in listed.split("; ") for n in part.split(" ", 1)[1].split(", ") if not part[0].isdigit()}


def wait_days(w: World, days: int) -> None:
    """Nobody touches the task; the runner keeps ticking, and its cleanup runs when it is due."""
    for _ in range(days * 2):
        w.now += timedelta(hours=12)
        w.tick()


def test_a_review_round_after_the_release_is_told_what_went_and_the_agent_makes_it_again(w):
    take(w)
    agent_makes(w, "ACME-1")
    sha = w.agent_commits()
    w.settle("ACME-1", "waiting_review", outcome={"summary": "Added the login fix and a test"})
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"

    wait_days(w, 4)
    assert "acme1-db" not in w.docker.cs and "acme1-testdb" not in w.docker.vols  # released
    assert "acme1-repro" in w.docker.cs  # unmarked: kept until the task is over
    assert w.store.task("ACME-1").state == "code_review"
    assert w.remote_branch("agent/ACME-1-fix-login-page") == sha  # the published work is untouched

    w.gl.note(1, "reviewer", "@acme-agent-bot Please rename this function", path="app/login.py", line=12)
    w.tick()
    later(w)
    [send] = sends(w)
    text = send["argv"][-1]
    assert "Please rename this function" in text
    assert text.index("Please rename this function") < text.index("Runner note:")
    assert removed_in(text) == {"acme1-db", "acme1-testdb"}

    # The agent makes again what the note named, under the same names, while it holds the workspace.
    agent_makes(w, "ACME-1")
    w.tick()
    w.now += timedelta(days=4)  # a long round: what it is working with is its own, not a waiting task's
    w.tick()
    assert {"acme1-db", "acme1-repro"} <= set(w.docker.cs) and "acme1-testdb" in w.docker.vols
    fix = fix_and_settle(w)
    assert w.store.task("ACME-1").state == "code_review"
    assert len(w.gl.mrs) == 1 and w.remote_branch("agent/ACME-1-fix-login-page") == fix  # the round is published

    # The next round is told only what went after it began.
    w.gl.note(1, "reviewer", "@acme-agent-bot One more thing", path="app/login.py", line=14)
    w.tick()
    later(w)
    assert "Runner note:" not in sends(w)[-1]["argv"][-1]


def test_an_agent_that_cannot_make_it_again_asks_on_the_ticket_and_the_answer_resumes_it(w):
    published(w)
    agent_makes(w, "ACME-1")
    wait_days(w, 4)
    assert "acme1-db" not in w.docker.cs

    w.gl.note(1, "reviewer", "@acme-agent-bot Please rename this function", path="app/login.py", line=12)
    w.tick()
    later(w)
    assert "declare the outcome blocked" in sends(w)[-1]["argv"][-1]

    # The registry is down: the agent cannot pull postgres to seed its database again, and says so.
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(
        state="blocked", state_reason="needs-user", attempt_live=False, question=None,
        outcome={"result": "blocked", "summary": "The test database cleanup removed (acme1-db) cannot be made "
                                                 "again: pulling postgres:16 fails with 503 from the registry."}))
    w.tick()
    assert w.store.task("ACME-1").state == "needs_input"
    [q] = [c for c in w.bot_comments() if "[arceus:question:" in c]
    assert "pulling postgres:16 fails with 503" in q  # visible to people, on the ticket

    w.yt.comment("2-1", "a.smith", "The registry is back, go on.")
    w.tick()
    answer = sends(w)[-1]["argv"][-1]
    assert "The registry is back, go on." in answer
    assert w.store.task("ACME-1").state == "dispatched"

    agent_makes(w, "ACME-1")  # made again this time
    fix = fix_and_settle(w)
    assert w.store.task("ACME-1").state == "code_review"
    assert w.remote_branch("agent/ACME-1-fix-login-page") == fix
    assert "acme1-db" in w.docker.cs and "acme1-repro" in w.docker.cs
