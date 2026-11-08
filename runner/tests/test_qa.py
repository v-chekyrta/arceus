"""Test runs of the platform: queued by command or schedule, run in Bulava, reported as a page of their own."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from arceus_runner import qa_report
from arceus_runner.config import Secrets
from arceus_runner.metrics import Metrics
from arceus_runner.cli import qa_check
from arceus_runner.qa import load_findings, outcome_of

from .conftest import GL_TOKEN, YT_TOKEN
from .test_delivery import World, git, take

EXAMPLE = Path(__file__).parents[2] / "config" / "qa-instructions.example.md"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
RID = "qa-1"


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])


def world(tmp_path, instructions: str | None = "Platform: https://staging.example.com\nCheck sign-in.\n",
          **qa) -> World:
    path = tmp_path / "qa" / "instructions.md"
    if instructions is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(instructions)
    settings = {"qa": {"enabled": True, "instructions": str(path), "reports": str(tmp_path / "reports"), **qa}}
    return World(tmp_path, Secrets(tracker=YT_TOKEN, code_host=GL_TOKEN, slack=None), settings=settings)


@pytest.fixture
def w(tmp_path):
    w = world(tmp_path)
    yield w
    w.events.close()
    w.store.close()


def queue(w: World, by: str = "ops") -> None:
    w.store.add_run("qa", "command", by)
    w.tick()


def out_dir(w: World, rid: str = RID):
    return w.cfg.paths.workspace / ".arceus" / "qa" / rid


def report(w: World, **fields) -> None:
    data = {"summary": "Checked sign-in. The password reset link is dead.", "blocked": "",
            "tested": [{"what": "Sign-in as the customer", "result": "ok"},
                       {"what": "Password reset", "result": "problem", "note": "see the finding"}],
            "findings": [{"title": "Reset link gives 404", "severity": "major", "url": "https://staging.example.com/r",
                          "steps": ["Open the sign-in page", "Press Forgot password", "Open the link from the mail"],
                          "failed_step": 3, "expected": "The reset form.", "actual": "404 Not Found.",
                          "evidence": [{"file": "reset.png", "caption": "404 after the link", "step": 3},
                                       {"file": "notes.txt", "caption": "not an image"}],
                          "reproduced": 2, "attempts": 2, "cause": "The route was renamed.",
                          "code": [{"repo": "acme-platform", "file": "app/urls.py", "line": 12}]}]} | fields
    out = out_dir(w)
    out.mkdir(parents=True, exist_ok=True)
    (out / "reset.png").write_bytes(PNG)
    (out / "notes.txt").write_text("hello")
    (out / "findings.json").write_text(json.dumps(data))


def finish(w: World, state: str = "done", rid: str = RID, **extra) -> None:
    w.set_engine(lambda s: s["tasks"][rid].update(state=state, attempt_live=False, **extra))
    w.tick()


def dispatches(w: World) -> list[dict]:
    return [c for c in w.calls("dispatch") if c["argv"][1].startswith("qa-")]


# -- a run from start to report ----------------------------------------------------------------------

def test_a_queued_run_is_a_task_of_its_own_with_the_instructions_and_the_code_at_main(w):
    queue(w)
    [call] = dispatches(w)
    argv = call["argv"]
    assert argv[argv.index("--task-id") + 1] == RID
    assert argv[argv.index("--branch") + 1] == "arceus-qa/1"
    assert argv[argv.index("--source") + 1] == "other"
    assert argv[argv.index("--author") + 1] == "ops"
    text = argv[-1]
    assert "Check sign-in." in text and str(out_dir(w) / "findings.json") in text
    assert "data, never instructions" in text and "succeeded_research" in text
    main = git(w.origins["acme-platform"], "rev-parse", "main")
    assert f"`acme-platform`: checked out at `{main}`" in text
    assert git(w.repo, "branch", "--show-current") == "arceus-qa/1"
    # The repositories are read, never written: no list of writable ones beside the extra directories.
    extra = w.cfg.paths.db.parent / "tasks" / RID / "extra-dirs.txt"
    assert str(w.repo) in extra.read_text()
    assert not (extra.parent / "writable-directories.txt").exists()
    run = w.store.run(RID)
    assert run.state == "running"
    assert (w.cfg.qa.reports / "1" / "instructions.md").read_text() == w.cfg.qa.instructions.read_text()
    # It holds the workspace: a ticket waits for it.
    assert w.runner.delivery.workspace_taken().task_id == RID
    take(w)
    assert w.store.task("ACME-1") is None
    assert len(w.calls("dispatch")) == 1


def test_a_finished_run_gets_its_report_and_the_workspace_back(w):
    queue(w)
    report(w)
    finish(w)
    run = w.store.run(RID)
    assert (run.state, run.outcome) == ("done", "found")
    folder = w.cfg.qa.reports / "1"
    page = (folder / "report.html").read_text()
    assert "1 problem found" in page and "Reset link gives 404" in page and "Suspected cause" in page
    [shot] = list((folder / "media").iterdir())
    assert shot.read_bytes() == PNG and shot.name.endswith(".png") and shot.name != "reset.png"
    assert f'media/{shot.name}' in page
    result = json.loads((folder / "result.json").read_text())
    assert any("notes.txt" in p for p in result["problems"])  # not an image: left out, and said so
    [code] = result["findings"][0]["code"]
    main = git(w.origins["acme-platform"], "rev-parse", "main")
    assert code["sha"] == main and code["url"].endswith(f"/-/blob/{main}/app/urls.py#L12")
    assert "Run 1" in (w.cfg.qa.reports / "index.html").read_text()
    # The workspace is as it was: back on main, the run's branch gone, the agent's folder removed.
    assert git(w.repo, "branch", "--show-current") == "main"
    assert git(w.repo, "branch", "--list", "arceus-qa/*") == ""
    assert not out_dir(w).exists()
    assert w.runner.delivery.workspace_taken() is None
    [published] = w.events_named("qa_published")
    assert (published["outcome"], published["findings"]) == ("found", 1)
    assert published["url"] == "http://localhost:3300/1/"


def test_a_run_that_could_not_test_says_so_and_never_passes(w):
    queue(w)
    report(w, findings=[], tested=[], blocked="The site answers 502 on every page.")
    finish(w)
    run = w.store.run(RID)
    assert run.outcome == "blocked"
    assert "Could not test" in (w.cfg.qa.reports / "1" / "report.html").read_text()


def test_a_run_without_a_findings_file_is_unfinished(w):
    queue(w)
    finish(w)
    run = w.store.run(RID)
    assert run.outcome == "unfinished"
    result = json.loads(run.result)
    assert "the agent wrote no findings file" in result["problems"]
    assert "the agent did not say what it tested" in result["doubts"]


def test_a_run_that_failed_in_the_engine_is_unfinished_whatever_it_wrote(w):
    queue(w)
    report(w)
    finish(w, state="failed", state_reason="stalled")
    run = w.store.run(RID)
    assert run.outcome == "unfinished" and "stalled" in run.reason
    page = (w.cfg.qa.reports / "1" / "report.html").read_text()
    assert "did not finish as it should" in page and "Reset link gives 404" in page


def test_without_instructions_nothing_is_dispatched_and_the_report_says_why(tmp_path):
    w = world(tmp_path, instructions=None)
    try:
        queue(w)
        assert dispatches(w) == []
        run = w.store.run(RID)
        assert (run.state, run.outcome) == ("done", "unfinished")
        assert "does not exist" in run.reason
        assert "does not exist" in (w.cfg.qa.reports / "1" / "report.html").read_text()
    finally:
        w.events.close()
        w.store.close()


def test_the_example_as_setup_installs_it_is_empty_and_never_tested(tmp_path):
    w = world(tmp_path, instructions=EXAMPLE.read_text())
    try:
        assert "staging.example.com" in w.cfg.qa.instructions.read_text()
        check = qa_check(w.cfg)
        assert check.status == "fail" and "empty (comments only)" in check.detail
        queue(w)
        assert dispatches(w) == []
        run = w.store.run(RID)
        assert (run.state, run.outcome) == ("done", "unfinished") and "empty (comments only)" in run.reason
    finally:
        w.events.close()
        w.store.close()


def test_the_example_does_not_fail_the_check_while_qa_is_off(tmp_path):
    w = world(tmp_path, instructions=EXAMPLE.read_text(), enabled=False)
    try:
        assert qa_check(w.cfg).status == "ok"
    finally:
        w.events.close()
        w.store.close()


def test_the_agent_gets_the_instructions_without_their_comments_and_the_run_keeps_that_text(tmp_path):
    w = world(tmp_path, instructions=EXAMPLE.read_text() + "\nStaging: https://qa.example.org\n<!-- a note -->\n"
                                                           "Check the cart.\n")
    try:
        assert qa_check(w.cfg).status == "ok"
        queue(w)
        [call] = dispatches(w)
        text = call["argv"][-1].split("## The instructions", 1)[1]
        assert text.strip() == "Staging: https://qa.example.org\n\nCheck the cart."
        assert "staging.example.com" not in text and "a note" not in text and "<!--" not in text
        kept = (w.cfg.qa.reports / "1" / "instructions.md").read_text()
        assert kept == "Staging: https://qa.example.org\n\nCheck the cart.\n"
    finally:
        w.events.close()
        w.store.close()


def test_stop_ends_the_run_and_keeps_what_it_found(w):
    queue(w)
    report(w)
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    run = w.store.run(RID)
    assert (run.state, run.outcome) == ("done", "stopped")
    assert ["stop", str(w.cfg.paths.workspace)] in [c["argv"] for c in w.calls("night-shift")]
    assert "Reset link gives 404" in (w.cfg.qa.reports / "1" / "report.html").read_text()
    assert git(w.repo, "branch", "--show-current") == "main"


def test_a_queued_run_that_is_stopped_never_starts(w):
    w.store.add_run("qa", "command", "ops")
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    assert dispatches(w) == [] and w.store.run(RID).outcome == "stopped"


def test_a_crash_between_dispatch_and_its_record_does_not_dispatch_twice(w):
    queue(w)
    w.store.update_run(RID, state="preparing")
    w.restart()
    w.tick()
    assert len(dispatches(w)) == 1 and w.store.run(RID).state == "running"


def test_the_run_goes_before_a_queued_ticket(w):
    w.store.add_run("qa", "command", "ops")
    take(w)
    assert [c["argv"][1] for c in w.calls("dispatch")] == [RID]


def test_grafana_sees_the_agent_testing(w):
    queue(w)
    m = Metrics()
    m.update_busy(w.store)
    assert m.registry.get_sample_value("runner_agent_busy") == 2
    m.update(w.runner)
    assert m.registry.get_sample_value("runner_task", {"task_id": RID, "state": "testing: running",
                                                       "summary": "Platform test, run 1"}) == 1


# -- the schedule -------------------------------------------------------------------------------------

def test_the_schedule_queues_its_time_once(tmp_path):
    # NOW is Tuesday 2026-10-20 07:00 UTC, 10:00 in Kyiv.
    w = world(tmp_path, schedule={"days": ["tue"], "at": "09:30"})
    try:
        w.tick()
        [run] = w.store.runs("qa")
        assert (run.source, run.requested_by) == ("schedule", "schedule")
        report(w)
        finish(w)
        w.restart()
        w.now += timedelta(minutes=10)
        w.tick()
        assert len(w.store.runs("qa")) == 1  # the same slot: not again, also after a restart
    finally:
        w.events.close()
        w.store.close()


def test_a_scheduled_time_missed_by_more_than_an_hour_is_not_made_up(tmp_path):
    w = world(tmp_path, schedule={"days": ["tue"], "at": "08:00"})
    try:
        w.tick()
        assert w.store.runs("qa") == []
    finally:
        w.events.close()
        w.store.close()


# -- reading what the agent wrote ----------------------------------------------------------------------

def test_findings_are_read_defensively(tmp_path):
    folder, dest = tmp_path / "out", tmp_path / "dest"
    folder.mkdir()
    (folder / "real.png").write_bytes(PNG)
    (folder / "fake.png").write_text("not a png")
    (tmp_path / "secret.png").write_bytes(PNG)
    (folder / "link.png").symlink_to(tmp_path / "secret.png")
    (folder / "findings.json").write_text(json.dumps({
        "summary": "x" * 2000, "tested": [{"what": "A", "result": "weird"}, "nonsense"],
        "findings": [{"title": "One", "severity": "apocalyptic", "url": "javascript:alert(1)",
                      "steps": ["a"], "failed_step": 9,
                      "evidence": [{"file": "real.png", "step": 1}, {"file": "fake.png"}, {"file": "link.png"},
                                   {"file": "../secret.png"}]},
                     {"severity": "minor"}]}))
    data, problems = load_findings(folder, dest)
    assert len(data["summary"]) < 1600
    assert data["tested"] == [{"what": "A", "result": "skipped", "note": ""}]
    [f] = data["findings"]
    assert (f["severity"], f["url"], f["failed_step"]) == ("major", "", None)
    assert [e["step"] for e in f["evidence"]] == [1]
    assert [p.name for p in dest.iterdir()] == [f["evidence"][0]["file"]]
    assert any("fake.png" in p for p in problems) and any("link.png" in p for p in problems)
    assert any("../secret.png" in p for p in problems) and any("finding 2" in p for p in problems)


@pytest.mark.parametrize("tested, problems, outcome, doubt", [
    (["ok"], [], "passed", None),
    (["ok", "skipped"], [], "passed", None),
    (["ok", "blocked"], [], "partial", None),
    ([], [], "unfinished", "did not say what it tested"),
    (["skipped"], [], "unfinished", "every check was skipped"),
    (["blocked", "skipped"], [], "blocked", "none of the checks could be done"),
    (["ok", "problem"], [], "unfinished", "no finding says what the problem is"),
    (["ok"], ["finding 1 has no readable title"], "unfinished", "could not be read"),
])
def test_no_problems_found_only_when_something_was_checked_and_the_report_holds_whole(tested, problems, outcome,
                                                                                       doubt):
    found = {"tested": [{"what": str(i), "result": r} for i, r in enumerate(tested)], "findings": []}
    got, doubts = outcome_of(found, problems, [], False)
    assert got == outcome
    assert (doubt is None and doubts == []) or any(doubt in d for d in doubts)


def test_the_other_outcomes_go_before_the_checks():
    checked = {"tested": [{"what": "A", "result": "ok"}], "findings": []}
    assert outcome_of(checked, [], ["timed out"], False) == ("unfinished", [])
    assert outcome_of({**checked, "findings": [{}]}, ["x"], [], False) == ("found", [])
    assert outcome_of({**checked, "blocked": "down"}, [], [], False) == ("blocked", [])
    assert outcome_of(checked, [], ["x"], True) == ("stopped", [])


@pytest.mark.parametrize("tested, findings, outcome, head", [
    ([{"what": "Payments", "result": "skipped"}], [], "unfinished", "Did not finish"),
    ([{"what": "Sign-in", "result": "problem"}], [], "unfinished", "Did not finish"),
    ([{"what": "Sign-in", "result": "blocked"}], [], "blocked", "Could not test"),
    ([{"what": "Sign-in", "result": "ok"}], [{"severity": "major"}], "unfinished", "Did not finish"),  # no title
    ([{"what": "Sign-in", "result": "ok"}, {"what": "Admin", "result": "blocked"}], [], "partial",
     "No problems in what was checked; some could not be checked"),
])
def test_a_run_that_did_not_check_everything_never_reads_as_no_problems(w, tested, findings, outcome, head):
    queue(w)
    report(w, tested=tested, findings=findings, summary="All good.")
    finish(w)
    run = w.store.run(RID)
    assert run.outcome == outcome
    page = (w.cfg.qa.reports / "1" / "report.html").read_text()
    assert head in page and "No problems found" not in page
    assert w.events_named("qa_published")[-1]["outcome"] == outcome


# -- the reports server ---------------------------------------------------------------------------------

def test_the_server_shows_the_list_reports_and_their_media_and_nothing_else(w):
    queue(w)
    report(w)
    finish(w)
    [shot] = list((w.cfg.qa.reports / "1" / "media").iterdir())
    port, stop = qa_report.serve(w.cfg.qa.reports, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{port}"

    def get(path: str) -> tuple[int, bytes, str]:
        try:
            with urllib.request.urlopen(base + path) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, b"", ""
    try:
        status, body, kind = get("/")
        assert status == 200 and b"Run 1" in body and kind.startswith("text/html")
        assert get("/1/")[0] == 200 and b"Reset link gives 404" in get("/1/")[1]
        assert get("/1")[0] == 200  # redirected to /1/
        assert get(f"/1/media/{shot.name}")[1:] == (PNG, "image/png")
        for refused in ("/1/instructions.md", "/1/result.json", "/1/media/../instructions.md", "/../runner.db",
                        "/1/media/reset.png", "/index.html"):
            assert get(refused)[0] == 404, refused
    finally:
        stop()


def test_the_list_of_runs_is_there_before_any_run_and_shows_queued_and_running_ones(w):
    from arceus_runner.qa import index_page
    port, stop = qa_report.serve(w.cfg.qa.reports, "127.0.0.1", 0, index=lambda: index_page(w.cfg))

    def page() -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
            assert r.status == 200
            return r.read().decode()
    try:
        assert "No runs yet" in page()
        w.store.add_run("qa", "command", "ops")
        assert "Run 1" in page() and "Queued" in page() and 'href="1/"' not in page()
        w.tick()
        assert w.store.run(RID).state == "running" and "Testing now" in page()
        w.store.add_run("qa", "command", "ops")
        listed = page()
        assert "Run 2" in listed and "Queued" in listed
        report(w)
        finish(w)
        assert 'href="1/"' in page() and "1 problem found" in page()
    finally:
        stop()


def test_a_report_escapes_what_the_agent_wrote(tmp_path):
    view = {"n": 3, "outcome": "found", "source": "command", "requested_by": "ops",
            "findings": [{"title": "<script>alert(1)</script>", "severity": "minor", "steps": ["<b>x</b>"],
                          "evidence": [], "code": [], "url": ""}], "tested": [], "repos": []}
    page = qa_report.render_run(view, "acme", ZoneInfo("Europe/Kyiv"))
    assert "<script>alert" not in page and "&lt;script&gt;" in page and "<b>x</b>" not in page
