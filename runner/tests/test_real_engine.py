"""The runner against the real Bulava engine, with a stub instead of Claude.

Runs only where the engine is installed (ARCEUS_REAL_ENGINE=/opt/bulava/engine), as on a
server VM. The worker is `cat`, as in the engine's own test-server-tasks.sh: dispatch.sh,
worker-send.sh, `task show`, tmux, git and the task records are all real. The verdict is
written by the engine's own task_on_terminal, the function its review gate calls.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from .test_delivery import World, git

ENGINE = os.environ.get("ARCEUS_REAL_ENGINE")
pytestmark = pytest.mark.skipif(not ENGINE, reason="set ARCEUS_REAL_ENGINE to the installed engine folder")


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    # The worker here is `cat`: whether Claude is signed in on this machine does not matter.
    monkeypatch.setattr("arceus_runner.loop.engine_doctor", lambda engine: [])


@pytest.fixture
def w(tmp_path, secrets):
    yield from _world(tmp_path, secrets)


def _world(tmp_path, secrets, **env_over):
    stub = tmp_path / "wd-stub"
    stub.write_text("#!/bin/bash\nexec sleep 300\n")
    stub.chmod(0o755)
    (tmp_path / "cc").mkdir()
    (tmp_path / "tmux").mkdir(mode=0o700)
    env = {
        "SUPERVISOR_CLAUDE_CMD": "cat", "SUPERVISOR_REQUIRE_HANDSHAKE": "0", "SUPERVISOR_HANDSHAKE_WAIT": "0",
        "SUPERVISOR_PROMPT_WAIT": "1", "SUPERVISOR_WATCHDOG_CMD": str(stub), "SUPERVISOR_NO_SKILL_PICK": "1",
        "SUPERVISOR_PREFLIGHT_ENABLE": "0", "CLAUDE_CONFIG_DIR": str(tmp_path / "cc"),
        "TMUX_TMPDIR": str(tmp_path / "tmux"), "SUPERVISOR_RELEASE_DELAY": "0",
        **env_over,
    }
    world = World(tmp_path, secrets, engine_dir=Path(ENGINE), engine_env=env)
    yield world
    subprocess.run(["tmux", "-L", "bulava", "kill-server"], env={**os.environ, "TMUX_TMPDIR": env["TMUX_TMPDIR"]},
                   capture_output=True)
    subprocess.run(["pkill", "-f", str(stub)], capture_output=True)
    world.events.close()
    world.store.close()


def settle(w: World, task_id: str, disposition: str, outcome: dict) -> None:
    """End the running attempt the way the engine's gate does: outcome, receipt, task_on_terminal. The run
    is the workspace folder's, where the session starts; its receipt names every repository of the task."""
    engine = w.runner.delivery.engine
    import json
    script = f"""
set -e
. {shlex.quote(ENGINE)}/bin/supervisor-lib.sh
P={shlex.quote(w.store.task(task_id).repo_path)}
I="$(instance_dir "$(slug_for "$P")")"
printf '%s' {shlex.quote(json.dumps(outcome))} > "$I/outcome.json"
mkdir -p "$I/report"
jq -n --argjson r "$(repos_state_json "$I" "$P")" --arg res {shlex.quote(outcome["result"])} \\
  --arg rev {shlex.quote(disposition)} --arg t {shlex.quote(task_id)} \\
  '{{result:$res, review:$rev, repos:$r, task_id:$t}}' > "$I/report/receipt.json"
task_on_terminal "$I" {shlex.quote(disposition)}
"""
    r = subprocess.run(["bash", "-c", script], env=engine.env(), capture_output=True, text=True)
    assert r.returncode == 0, f"the engine's verdict did not go through (exit {r.returncode}): {r.stderr[-2000:]}"


def reviewed(show: dict, repo: str) -> str:
    return next(r["reviewed_sha"] for r in show["repos"] if Path(r["path"]).name == repo)


def wait_state(w: World, task_id: str, state: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        show = w.runner.delivery.engine.show(task_id)
        if show and show["state"] == state:
            return show
        time.sleep(0.3)
    raise AssertionError(f"{task_id} never reached {state}: {show}")


def test_ticket_to_mr_through_the_real_engine(w):
    w.yt.add_issue(1, ("a1", "j.doe", "+agent"))
    w.yt.issues[-1]["summary"] = "Add health endpoint"
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "dispatched", w.events_named("task_failed", "dispatch_busy", "reconcile_failed")

    show = w.runner.delivery.engine.show("ACME-1")
    assert show["state"] == "running" and show["branch"] == task.branch
    assert git(w.repo, "symbolic-ref", "--short", "HEAD") == task.branch  # the engine made the branch
    # The ticket reaches the agent framed as what a person wrote, not as the engine's own instruction.
    assert "<<UNTRUSTED_TRACKER_TEXT source=youtrack author=j.doe>>\nACME-1: Add health endpoint" in show["objective"]

    sha = w.agent_commits("health.txt")
    settle(w, "ACME-1", "passed", {"schema": 1, "result": "succeeded_changes", "summary": "done", "task_id": "ACME-1"})
    show = wait_state(w, "ACME-1", "waiting_review")
    assert show["receipt"]["review"] == "passed" and reviewed(show, "acme-platform") == sha

    w.tick()
    assert w.remote_branch(task.branch) == sha
    assert len(w.gl.mrs) == 1 and w.store.task("ACME-1").state == "code_review"


def test_question_and_answer_through_the_real_engine(w):
    w.yt.add_issue(1, ("a1", "j.doe", "+agent"))
    w.tick()
    settle(w, "ACME-1", "needs-user", {
        "schema": 1, "result": "needs_input", "summary": "which db?", "task_id": "ACME-1",
        "question": {"id": "q-1", "text": "Postgres or SQLite?", "options": ["Postgres", "SQLite"]}})
    wait_state(w, "ACME-1", "needs_input")
    w.tick()
    assert w.store.task("ACME-1").question_id == "q-1"

    w.yt.comment("2-1", "a.smith", "Postgres")
    w.tick()
    show = wait_state(w, "ACME-1", "running")
    assert [a["kind"] for a in show["attempts"]] == ["initial", "continue"]
    assert w.store.task("ACME-1").state == "dispatched"


def test_review_round_through_the_real_engine(w):
    from datetime import timedelta
    w.yt.add_issue(1, ("a1", "j.doe", "+agent"))
    w.tick()
    w.agent_commits("first.txt")
    settle(w, "ACME-1", "passed", {"schema": 1, "result": "succeeded_changes", "summary": "first", "task_id": "ACME-1"})
    wait_state(w, "ACME-1", "waiting_review")
    w.tick()
    task = w.store.task("ACME-1")
    assert task.state == "code_review" and len(w.gl.mrs) == 1

    d = w.gl.note(1, "reviewer", "@acme-agent-bot Rename it", path="first.txt", line=1)
    w.tick()
    w.now += timedelta(minutes=6)
    w.tick()
    show = wait_state(w, "ACME-1", "running")
    assert [a["kind"] for a in show["attempts"]] == ["initial", "continue"]   # the engine took the follow-up
    assert w.store.task("ACME-1").state == "dispatched"

    sha = w.agent_commits("renamed.txt")
    settle(w, "ACME-1", "passed", {"schema": 1, "result": "succeeded_changes", "summary": "renamed",
                                   "task_id": "ACME-1"})
    show = wait_state(w, "ACME-1", "waiting_review")
    assert reviewed(show, "acme-platform") == sha
    w.tick()
    assert w.store.task("ACME-1").state == "code_review"
    assert w.remote_branch(task.branch) == sha and len(w.gl.mrs) == 1
    reply = next(x for x in w.gl.discussions[1] if x["id"] == d)["notes"][-1]
    assert sha[:12] in reply["body"]


def test_check_settings_reach_the_worker_only_when_the_engine_carries_them(tmp_path, secrets):
    """On a server the workers' tmux server is bulava-engine.service's, started before any task with the
    unit's environment, and a worker's review gate reads the environment it was launched with. A check
    setting the runner passes on the dispatch reaches that gate only if the engine puts it on the
    worker's launch line; the probe's `settings.claude_checks` must say which is the case."""
    from arceus_runner import bulava
    seen = tmp_path / "worker-env"
    worker = tmp_path / "worker.sh"
    worker.write_text(f"#!/bin/bash\nenv > {seen}\nexec cat\n")
    worker.chmod(0o755)
    gen = _world(tmp_path, secrets, SUPERVISOR_CLAUDE_CMD=str(worker), SUPERVISOR_CLAUDE_CHECKS_ROUNDS="2")
    world = next(gen)
    try:
        # What the unit starts: the engine's tmux server with the server profile and nothing from a task.
        unit_env = {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"], "LANG": "C.UTF-8",
                    "TMUX_TMPDIR": str(tmp_path / "tmux"), "SUPERVISOR_PROFILE": "server"}
        subprocess.run(["tmux", "-L", "bulava", "new-session", "-d", "-s", "keeper", "sleep infinity"],
                       env=unit_env, check=True)
        world.yt.add_issue(1, ("a1", "j.doe", "+agent"))
        world.tick()
        assert world.store.task("ACME-1").state == "dispatched"
        deadline = time.time() + 30
        while not seen.exists() and time.time() < deadline:
            time.sleep(0.3)
        env = dict(line.split("=", 1) for line in seen.read_text().splitlines() if "=" in line)
        assert env.get("SUPERVISOR_PROFILE") == "server" and env.get("ORCHESTRATOR_RUN_ID")
        carried = bulava.probe(Path(ENGINE)).advertised["settings.claude_checks"]
        assert (env.get("SUPERVISOR_CLAUDE_CHECKS_ROUNDS") == "2") == carried, (carried, sorted(
            k for k in env if k.startswith("SUPERVISOR_")))
    finally:
        next(gen, None)
