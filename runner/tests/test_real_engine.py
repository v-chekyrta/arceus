"""The runner against the real Bulava engine, with a stub instead of Claude.

Runs only where the engine is installed (ARCEUS_REAL_ENGINE=/opt/bulava/engine), as on a
server VM. The worker is `cat`, as in the engine's own test-server-tasks.sh: dispatch.sh,
worker-send.sh, `task show`, tmux, git and the task records are all real. The verdict is
written by the engine's own task_on_terminal, the function its review gate calls.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from .test_delivery import World, git

ENGINE = os.environ.get("ARCEUS_REAL_ENGINE")
# Read before any test patches shutil.which (no_real_tools patches the module itself, not a copy of it).
TMUX_DIR = Path(shutil.which("tmux") or "/usr/bin/tmux").parent
pytestmark = pytest.mark.skipif(not ENGINE, reason="set ARCEUS_REAL_ENGINE to the installed engine folder")


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    # The worker here is `cat`: whether Claude is signed in on this machine does not matter.
    monkeypatch.setattr("arceus_runner.loop.engine_doctor", lambda engine: [])
    # On a server the engine is root's; a checkout on a Mac is the developer's own, and that is not under test.
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])


@pytest.fixture
def w(tmp_path, secrets):
    yield from _world(tmp_path, secrets)


def _world(tmp_path, secrets, settings=None, **env_over):
    stub = tmp_path / "wd-stub"
    stub.write_text("#!/bin/bash\nexec sleep 300\n")
    stub.chmod(0o755)
    (tmp_path / "cc").mkdir()
    # tmux's socket path has to fit in 104 bytes on macOS, and pytest's temporary folders there are longer.
    tmux_dir = Path(tempfile.mkdtemp(prefix="arc-tmux-", dir="/tmp"))
    env = {
        "SUPERVISOR_CLAUDE_CMD": "cat", "SUPERVISOR_REQUIRE_HANDSHAKE": "0", "SUPERVISOR_HANDSHAKE_WAIT": "0",
        "SUPERVISOR_PROMPT_WAIT": "1", "SUPERVISOR_WATCHDOG_CMD": str(stub), "SUPERVISOR_NO_SKILL_PICK": "1",
        "SUPERVISOR_PREFLIGHT_ENABLE": "0", "CLAUDE_CONFIG_DIR": str(tmp_path / "cc"),
        "TMUX_TMPDIR": str(tmux_dir), "SUPERVISOR_RELEASE_DELAY": "0",
        # The engine's own PATH (Engine.env) with this machine's tmux folder in front: /opt/homebrew/bin on a Mac.
        "PATH": f"{TMUX_DIR}:/usr/local/bin:/usr/bin:/bin",
        **env_over,
    }
    world = World(tmp_path, secrets, engine_dir=Path(ENGINE), engine_env=env, settings=settings)
    yield world
    subprocess.run(["tmux", "-L", "bulava", "kill-server"], env={**os.environ, "TMUX_TMPDIR": env["TMUX_TMPDIR"]},
                   capture_output=True)
    subprocess.run(["pkill", "-f", str(stub)], capture_output=True)
    shutil.rmtree(tmux_dir, ignore_errors=True)
    world.events.close()
    world.store.close()


def settle(w: World, task_id: str, disposition: str, outcome: dict) -> None:
    """End the running attempt the way the engine's gate does: outcome, receipt, task_on_terminal. The run
    is the workspace folder's, where the session starts; its receipt names every repository of the task."""
    engine = w.runner.delivery.engine
    import json
    task = w.store.task(task_id)
    project = task.repo_path if task else str(w.cfg.paths.workspace)  # a code review runs in the workspace too
    script = f"""
set -e
. {shlex.quote(ENGINE)}/bin/supervisor-lib.sh
P={shlex.quote(project)}
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


def test_a_worker_is_launched_with_no_claude_check_settings(tmp_path, secrets):
    """On a server the workers' tmux server is bulava-engine.service's, started before any task with the
    unit's environment, and a worker's review gate reads the environment it was launched with. Bulava
    1.19.2 removed the Claude checks before Codex: nothing the unit or the runner gives names them."""
    seen = tmp_path / "worker-env"
    worker = tmp_path / "worker.sh"
    worker.write_text(f"#!/bin/bash\nenv > {seen}\nexec cat\n")
    worker.chmod(0o755)
    gen = _world(tmp_path, secrets, SUPERVISOR_CLAUDE_CMD=str(worker))
    world = next(gen)
    try:
        # What the unit starts: the engine's tmux server with the server profile and nothing from a task.
        unit_env = {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"], "LANG": "C.UTF-8",
                    "TMUX_TMPDIR": world.engine_env["TMUX_TMPDIR"], "SUPERVISOR_PROFILE": "server"}
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
        assert not [k for k in env if k.startswith("SUPERVISOR_CLAUDE_CHECKS")], sorted(
            k for k in env if k.startswith("SUPERVISOR_"))
    finally:
        next(gen, None)


def test_a_code_review_runs_as_its_own_engine_task(tmp_path):
    """`/review` on the ticket: the real dispatch.sh takes a task id of its own, the review branch in the
    home, the MR's repository listed for reading only; its research outcome publishes nothing, and the
    workspace is back on the task branch afterwards."""
    import json
    from arceus_runner.config import Secrets
    from .conftest import GL_TOKEN, YT_TOKEN
    secrets = Secrets(tracker=YT_TOKEN, code_host=GL_TOKEN, slack=None, approver="glpat-approver-token-789")
    gen = _world(tmp_path, secrets, settings={"review_agent": {"enabled": True}, "code_host": {
        "approver": {"user": "acme-review-bot", "token_env": "GITLAB_REVIEW_TOKEN"}}})
    w = next(gen)
    try:
        w.gl.tokens["glpat-approver-token-789"] = "acme-review-bot"
        w.yt.add_issue(1, ("a1", "j.doe", "+agent"))
        w.tick()
        sha = w.agent_commits("health.txt")
        settle(w, "ACME-1", "passed", {"schema": 1, "result": "succeeded_changes", "summary": "done",
                                       "task_id": "ACME-1"})
        wait_state(w, "ACME-1", "waiting_review")
        w.tick()
        task = w.store.task("ACME-1")
        assert task.state == "code_review"
        w.gl.mrs[0].update(sha=sha, head_pipeline={"id": 9, "status": "success", "sha": sha})

        w.yt.comment("2-1", "j.doe", "/review")
        w.tick()
        rid = "ACME-1.review-1"
        show = wait_state(w, rid, "running")
        assert show["branch"] == "arceus-review/ACME-1-1", show["branch"]
        assert "Do not ask anyone a question" in show["objective"]
        assert git(w.repo, "rev-parse", "HEAD") == sha and w.runner.delivery.engine.show("ACME-1")["state"] == \
            "waiting_review"                                     # the author's task is untouched

        verdict = w.cfg.paths.workspace / ".arceus" / "reviews" / "ACME-1" / f"{rid}.json"
        verdict.write_text(json.dumps({"summary": "Fine.", "mrs": [{
            "mr": "M1", "verdict": "ready", "requirements_met": True, "build": "passed", "tests": "none",
            "checked": "nothing to build", "ui": "none", "findings": [], "attention": []}]}))
        settle(w, rid, "passed", {"schema": 1, "result": "succeeded_research", "summary": "Fine.", "task_id": rid})
        deadline = time.time() + 30
        while w.store.review(rid).state != "done" and time.time() < deadline:
            w.tick()
            time.sleep(0.3)
        review = w.store.review(rid)
        assert review.state == "done", (review, w.events_named("review_step_failed"))
        assert review.outcome == "approved", json.loads(review.result)
        assert git(w.repo, "symbolic-ref", "--short", "HEAD") == task.branch
        assert w.remote_branch(task.branch) == sha and len(w.gl.mrs) == 1
    finally:
        next(gen, None)


def test_a_test_run_keeps_its_own_review_of_the_report_across_a_continue(tmp_path, secrets):
    """Bulava 1.20: a test run is sent with its report rounds, its own criteria in place of the server's rule
    and acceptance with notes at the limit. The engine copies the criteria when it takes the task and puts the
    budget into the dispatch the review gate reads; a later attempt (`worker-send --relation continue`, what the
    runner sends after a crash) is reviewed by the same budget."""
    import json
    from arceus_runner.qa import REPORT_CRITERIA, QaAgent
    from arceus_runner.screening import trusted
    instructions = tmp_path / "qa" / "instructions.md"
    instructions.parent.mkdir(parents=True)
    instructions.write_text("Platform: https://staging.example.com\nCheck sign-in.\n")
    gen = _world(tmp_path, secrets, settings={"qa": {"enabled": True, "instructions": str(instructions),
                                                     "reports": str(tmp_path / "reports")}})
    w = next(gen)
    try:
        w.store.add_run("qa", "command", "ops", scenario=1)
        w.tick()
        rid = "qa-1"
        assert w.store.run(rid).state == "running", w.events_named("qa_step_failed", "qa_finished")
        engine = w.runner.delivery.engine
        wait_state(w, rid, "running")

        def gates() -> tuple[dict, str]:
            script = f"""
set -e
. {shlex.quote(ENGINE)}/bin/supervisor-lib.sh
I="$(instance_dir "$(slug_for {shlex.quote(str(w.cfg.paths.workspace))})")"
jq -c '.pipeline_gates' "$I/dispatch.json"
cat "$I/report-criteria.md"
"""
            r = subprocess.run(["bash", "-c", script], env=engine.env(), capture_output=True, text=True)
            assert r.returncode == 0, r.stderr[-2000:]
            first, _, rest = r.stdout.partition("\n")
            return json.loads(first), rest

        g, criteria = gates()
        assert (g["reportReview"], g["reportMaxRounds"], g["reportAtLimit"]) == (True, QaAgent.REPORT_ROUNDS, "accept")
        assert criteria.strip() == REPORT_CRITERIA.strip()

        # What the review reads, found and read by the engine's own code as review-gate.sh does it: the findings
        # as they are at each round, whether the summary names the link (as the brief asks) or not.
        findings = w.cfg.paths.workspace.resolve() / ".arceus" / "qa" / rid / "findings.json"
        tool = [sys.executable, f"{ENGINE}/bin/pipeline-tool.py"]

        def reviewer_reads(summary: str) -> str:
            since = json.loads(subprocess.run(["bash", "-c", f". {shlex.quote(ENGINE)}/bin/supervisor-lib.sh; "
                f'jq -c . "$(instance_dir "$(slug_for {shlex.quote(str(w.cfg.paths.workspace))})")/dispatch.json"'],
                env=engine.env(), capture_output=True, text=True, check=True).stdout)["at"]
            found = subprocess.run([*tool, "find-report", "--cwd", str(w.cfg.paths.workspace), "--summary", summary,
                                    "--since", since], capture_output=True, text=True).stdout.strip()
            assert found, "the engine found no report"
            return subprocess.run([*tool, "report-text", found, "--max", "60000"], capture_output=True,
                                  text=True, check=True).stdout

        findings.write_text(json.dumps({"summary": "First draft.", "tested": [], "findings": []}))
        assert "First draft." in reviewer_reads(f"artifacts/{rid}/findings.txt Checked sign-in.")
        findings.write_text(json.dumps({"summary": "Corrected after the objections.", "tested": [], "findings": []}))
        assert "Corrected after the objections." in reviewer_reads(f"artifacts/{rid}/findings.txt Checked sign-in.")
        assert "Corrected after the objections." in reviewer_reads("Checked sign-in.")
        # The engine's own copy, not the runner's file: editing that later changes nothing for this run.
        assert Path(g["reportCriteria"]).name == "report-criteria.md"
        assert Path(g["reportCriteria"]).parent != w.cfg.paths.db.parent / "tasks" / rid

        settle(w, rid, "needs-user", {"schema": 1, "result": "needs_input", "summary": "?", "task_id": rid,
                                      "question": {"id": "q-1", "text": "?", "options": ["a"]}})
        wait_state(w, rid, "needs_input")
        reply = engine.send(task_id=rid, message_id="resume-qa-1-1", source="other", author="ops",
                            repo=Path(w.cfg.paths.workspace), text="Continue.", clearance=trusted("Continue."),
                            extra_dirs=w.cfg.paths.db.parent / "tasks" / rid / "extra-dirs.txt")
        assert reply.accepted, reply.why()
        show = wait_state(w, rid, "running")
        assert [a["kind"] for a in show["attempts"]] == ["initial", "continue"]
        g, criteria = gates()
        assert (g["reportReview"], g["reportMaxRounds"], g["reportAtLimit"]) == (True, QaAgent.REPORT_ROUNDS, "accept")
        assert criteria.strip() == REPORT_CRITERIA.strip()
    finally:
        next(gen, None)


def test_a_report_longer_than_the_review_reads_is_reviewed_part_by_part(tmp_path, secrets):
    """The engine's review reads at most 60 000 characters of one file. A report past that comes in parts: when the
    attempt that reviewed the first ends `done`, the runner asks for another attempt naming the second, and the
    engine's own find-report and report-text give the reviewer that part whole, to its last line."""
    import json
    from arceus_runner.qa import QaAgent
    instructions = tmp_path / "qa" / "instructions.md"
    instructions.parent.mkdir(parents=True)
    instructions.write_text("Platform: https://staging.example.com\nCheck everything.\n")
    gen = _world(tmp_path, secrets, settings={"qa": {"enabled": True, "instructions": str(instructions),
                                                     "reports": str(tmp_path / "reports")}})
    w = next(gen)
    try:
        w.store.add_run("qa", "command", "ops", scenario=1)
        w.tick()
        rid = "qa-1"
        engine = w.runner.delivery.engine
        wait_state(w, rid, "running")
        ws = w.cfg.paths.workspace.resolve()
        out = ws / ".arceus" / "qa" / rid

        def checks(prefix: str, n: int) -> list[dict]:
            return [{"what": f"{prefix} check {i}: " + "a" * 200, "result": "ok", "note": "b" * 100} for i in range(n)]

        first = {"summary": "Checked 220 things.", "blocked": "", "parts": 2, "checks": 220, "findings_count": 1,
                 "findings": [{"title": "Export fails", "severity": "major", "steps": ["Open export"],
                               "expected": "A file.", "actual": "500."}], "tested": checks("first", 110)}
        marker = "THE LAST LINE OF PART TWO: objection bait"
        second = {"tested": checks("second", 109) + [{"what": marker, "result": "ok"}]}
        (out / "findings.json").write_text(json.dumps(first, separators=(",", ":")))
        (out / "findings-2.json").write_text(json.dumps(second, separators=(",", ":")))
        whole = (out / "findings.json").read_text() + (out / "findings-2.json").read_text()
        assert len(whole) > 60_000 and len((out / "findings-2.json").read_text()) < 40_000

        tool = [sys.executable, f"{ENGINE}/bin/pipeline-tool.py"]

        def dispatch_record() -> dict:
            r = subprocess.run(["bash", "-c", f". {shlex.quote(ENGINE)}/bin/supervisor-lib.sh; "
                                f'cat "$(instance_dir "$(slug_for {shlex.quote(str(w.cfg.paths.workspace))})")/dispatch.json"'],
                               env=engine.env(), capture_output=True, text=True, check=True)
            return json.loads(r.stdout)

        def reviewer_reads(summary: str) -> str:
            found = subprocess.run([*tool, "find-report", "--cwd", str(w.cfg.paths.workspace), "--summary", summary,
                                    "--since", dispatch_record()["at"]], capture_output=True, text=True).stdout.strip()
            assert found, "the engine found no report"
            return subprocess.run([*tool, "report-text", found, "--max", "60000"], capture_output=True,
                                  text=True, check=True).stdout

        assert "first check 109" in reviewer_reads(f"artifacts/{rid}/findings.txt Checked 220 things.")
        settle(w, rid, "passed", {"schema": 1, "result": "succeeded_research", "summary": "x", "task_id": rid})
        wait_state(w, rid, "done")
        w.tick()                                   # the runner asks for the second part
        show = wait_state(w, rid, "running")
        assert [a["kind"] for a in show["attempts"]] == ["initial", "continue"]
        rec = dispatch_record()
        gates = rec["pipeline_gates"]
        assert (gates["reportMaxRounds"], gates["reportAtLimit"]) == (QaAgent.REPORT_ROUNDS, "accept")
        assert f"artifacts/{rid}/findings-2.txt" in rec["task"]
        text = reviewer_reads(f"artifacts/{rid}/findings-2.txt Checked 220 things.")
        assert text.rstrip().endswith(json.dumps(second, separators=(",", ":"))[-60:].rstrip()) and marker in text
        assert "second check 0" in text

        settle(w, rid, "passed", {"schema": 1, "result": "succeeded_research", "summary": "x", "task_id": rid})
        wait_state(w, rid, "done")
        deadline = time.time() + 30
        while w.store.run(rid).state != "done" and time.time() < deadline:
            w.tick()
            time.sleep(0.3)
        run = w.store.run(rid)
        result = json.loads(run.result)
        assert (run.state, run.outcome) == ("done", "found"), result
        assert len(result["tested"]) == 220 and result["problems"] == [] and "engine_disputed" not in result
        assert not (ws / "artifacts" / rid).exists()
    finally:
        next(gen, None)
