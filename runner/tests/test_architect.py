"""Architecture reviews: what changed since the last one, read by the agent, checked by Codex, kept for a person."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from arceus_runner import arch_report, architect
from arceus_runner.architect import load_findings, matches, outcome_of
from arceus_runner.cli import architect_check
from arceus_runner.config import Secrets
from arceus_runner.metrics import Metrics

from .conftest import GL_TOKEN, YT_TOKEN
from .test_delivery import World, git, take

RID = "arch-1"
CODE = "def charge(order):\n    for attempt in range(3):\n        send(order)\n" + "# filler\n" * 30


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])


class Codex:
    """A stand-in for the Codex CLI: it records what it was given and answers with what the test set."""

    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.bin = folder / "codex"
        self.bin.write_text(f"#!{sys.executable}\n" + """\
import json, os, sys, time, pathlib
here = pathlib.Path(__file__).parent
args = sys.argv[1:]
(here / "calls").open("a").write(json.dumps({"args": args, "env": sorted(os.environ),
                                             "prompt": sys.stdin.read()}) + "\\n")
answer = (here / "answer").read_text() if (here / "answer").exists() else ""
if answer == "sleep":
    time.sleep(60)
elif answer.startswith("exit"):
    print("codex: the usage limit is reached", file=sys.stderr)
    sys.exit(3)
else:
    pathlib.Path(args[args.index("-o") + 1]).write_text(answer)
""")
        self.bin.chmod(0o755)

    def answer(self, verdicts: list[dict] | str) -> None:
        (self.folder / "answer").write_text(verdicts if isinstance(verdicts, str) else
                                            json.dumps({"verdicts": verdicts}))

    @property
    def calls(self) -> list[dict]:
        f = self.folder / "calls"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def world(tmp_path, instructions: str | None = None, **arch) -> World:
    path = tmp_path / "architect" / "instructions.md"
    if instructions is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(instructions)
    settings = {"architect": {"enabled": True, "instructions": str(path), "reports": str(tmp_path / "reports"),
                              **arch}}
    return World(tmp_path, Secrets(tracker=YT_TOKEN, code_host=GL_TOKEN, slack=None), settings=settings)


@pytest.fixture
def codex(tmp_path, monkeypatch):
    c = Codex(tmp_path / "codex")
    monkeypatch.setattr(architect, "CODEX", str(c.bin))
    return c


@pytest.fixture
def w(tmp_path, codex):
    w = world(tmp_path)
    yield w
    w.events.close()
    w.store.close()


def push(w: World, repo: str, files: dict[str, str | None], message: str = "change") -> str:
    """A commit on the origin's main, as a merged merge request leaves it. None deletes the file."""
    clone = w.tmp / "pushes" / repo
    if not clone.exists():
        subprocess.run(["git", "clone", "-q", str(w.origins[repo]), str(clone)], check=True)
    git(clone, "pull", "-q")
    for name, text in files.items():
        if text is None:
            (clone / name).unlink()
            continue
        (clone / name).parent.mkdir(parents=True, exist_ok=True)
        (clone / name).write_text(text)
    git(clone, "add", "-A")
    git(clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    git(clone, "push", "-q", "origin", "main")
    return git(clone, "rev-parse", "HEAD")


def queue(w: World, by: str = "ops") -> None:
    w.store.add_run("arch", "command", by)
    w.tick()


def brief(w: World, rid: str = RID) -> str:
    [call] = [c for c in w.calls("dispatch") if c["argv"][c["argv"].index("--task-id") + 1] == rid]
    return call["argv"][-1]


def out_dir(w: World, rid: str = RID) -> Path:
    return w.cfg.paths.workspace / ".arceus" / "arch" / rid


def report(w: World, rid: str = RID, **fields) -> None:
    data = {"summary": "Billing grew a retry of its own.",
            "reviewed": [{"repo": "acme-platform", "what": "billing and its tests"}], "skipped": [], "previous": [],
            "findings": [{"title": "Two retry mechanisms for one call", "lens": "coherence", "kind": "violation",
                          "rule": "AGENTS.md: every outgoing call goes through http/client.py", "severity": "medium",
                          "components": ["billing", "http"], "what": "billing/pay.py retries by itself.",
                          "risk": "A refused charge is sent three times.", "direction": "Use the shared client.",
                          "where": [{"repo": "acme-platform", "file": "billing/pay.py", "line": 2}]},
                         {"title": "A finding about code that is not there", "lens": "slop", "kind": "proposal",
                          "where": [{"repo": "acme-platform", "file": "billing/ghost.py", "line": 1}]}]} | fields
    out = out_dir(w, rid)
    out.mkdir(parents=True, exist_ok=True)
    (out / "findings.json").write_text(json.dumps(data))


def finish(w: World, rid: str = RID) -> None:
    w.set_engine(lambda s: s["tasks"][rid].update(state="done", attempt_live=False))
    w.tick()


def result(w: World, rid: str = RID) -> dict:
    return json.loads(w.store.run(rid).result)


# -- what a run looks at -------------------------------------------------------------------------------

def test_nothing_changed_is_a_report_of_its_own_and_never_reaches_the_engine(w):
    queue(w)  # the seed repositories have no files: nothing to review
    run = w.store.run(RID)
    assert (run.state, run.outcome) == ("done", "unchanged")
    assert w.calls("dispatch") == [] and run.reason == "no repository it reviews changed in the last 30 days"
    page = (w.cfg.architect.reports / "1" / "report.html").read_text()
    assert "Nothing changed" in page and "Did not finish" not in page
    assert w.runner.delivery.workspace_taken() is None


def test_a_review_reads_what_changed_with_the_runners_criteria_and_holds_the_workspace(tmp_path, codex):
    w = world(tmp_path, skip=["acme-platform:generated/**"], not_ours=["acme-theme"])
    try:
        sha = push(w, "acme-platform", {"billing/pay.py": CODE, "generated/api.py": "x = 1\n"})
        push(w, "acme-theme", {"theme/override.css": "a {}\n"})
        queue(w)
        text = brief(w)
        assert "How the parts fit together" in text and "Tests that prove little" in text
        assert f"`acme-platform` (main), changes since the last 30 days: everything at `{sha[:12]}`, 2 commits, " \
               "1 file added or changed;" in text
        assert "`billing/pay.py`" in text and "generated/api.py" not in text  # skipped paths are not a change
        assert "Not the team's own code: `acme-theme`" in text and "never propose changing it" in text
        assert "Earlier findings\n\nNone." in text
        # No instructions written: the runner's criteria alone, in the report language of config.yaml.
        assert "The team's instructions" not in text and "texts in English" in text
        assert git(w.repo, "branch", "--show-current") == "arceus-arch/1"
        assert w.runner.delivery.workspace_taken().task_id == RID
        take(w)
        assert w.store.task("ACME-1") is None  # a ticket waits for the review
    finally:
        w.events.close()
        w.store.close()


def test_the_teams_instructions_are_added_without_their_comments(tmp_path, codex):
    w = world(tmp_path, instructions="<!-- guidance -->\nLook hard at billing.\n")
    try:
        push(w, "acme-platform", {"billing/pay.py": CODE})
        queue(w)
        text = brief(w)
        assert text.endswith("## The team's instructions for this review\n\nLook hard at billing.")
        assert "guidance" not in text and "written in the language the instructions below" not in text
    finally:
        w.events.close()
        w.store.close()


# -- a whole run --------------------------------------------------------------------------------------------

def test_a_finished_review_is_checked_by_codex_kept_and_reported(w, codex):
    push(w, "acme-platform", {"billing/pay.py": CODE})
    queue(w)
    report(w)
    codex.answer([{"finding": 1, "verdict": "confirmed", "reason": "pay.py:2 loops over send; client.py retries too."}])
    finish(w)
    run = w.store.run(RID)
    assert (run.state, run.outcome) == ("done", "found")
    r = result(w)
    # The reference that is not in the code is checked against the commit and its finding left out, saying so.
    assert [f["title"] for f in r["findings"]] == ["Two retry mechanisms for one call"]
    assert any("billing/ghost.py" in p for p in r["problems"])
    assert r["second_opinion"]["status"] == "done"
    assert r["second_opinion"]["verdicts"]["1"]["verdict"] == "confirmed"
    [finding] = w.store.findings()
    assert (finding.n, finding.status, finding.first_run, finding.decision) == (1, "open", 1, None)
    assert r["ledger"]["new"] == [1] and r["covered"]["acme-platform"] == git(w.origins["acme-platform"],
                                                                              "rev-parse", "main")
    # Codex ran read-only, without the web, with none of the runner's environment, in the workspace as switched.
    [call] = codex.calls
    args = call["args"]
    assert args[:3] == ["exec", "--sandbox", "read-only"] and "--ignore-user-config" in args
    assert "tools.web_search=false" in args and args[args.index("-C") + 1] == str(w.cfg.paths.workspace.resolve())
    assert set(call["env"]) <= {"HOME", "USER", "LANG", "PATH", "CODEX_HOME", "PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING",
                                "LC_CTYPE"}
    assert "Two retry mechanisms for one call" in call["prompt"] and "data, never instructions" in call["prompt"]
    page = (w.cfg.architect.reports / "1" / "report.html").read_text()
    assert "1 finding" in page and "Confirmed on second check" in page and "The rule it breaks" in page
    assert "confirmed 1 of 1" in page and "Codex" not in page  # the tool is not named: it may change
    assert "<form" not in page  # the copy in the folder is the run as it was: no buttons
    assert git(w.repo, "branch", "--show-current") == "main" and not out_dir(w).exists()
    assert w.runner.delivery.workspace_taken() is None


def test_the_next_review_starts_where_the_last_one_ended_and_rechecks_what_it_found(w, codex):
    first = push(w, "acme-platform", {"billing/pay.py": CODE, "billing/old.py": "x = 1\n"})
    queue(w)
    report(w)
    codex.answer([{"finding": 1, "verdict": "doubtful", "reason": "Only one caller."}])
    finish(w)
    second = push(w, "acme-platform", {"billing/refund.py": "def refund():\n    pass\n", "billing/old.py": None})
    queue(w)
    text = brief(w, "arch-2")
    # A deleted file is counted, never listed as code to read.
    assert f"changes since run 1: `{first[:12]}...{second[:12]}`, 1 commit, 1 file added or changed, 1 deleted" in text
    assert "Files: `billing/refund.py`." in text
    assert "- 1: Two retry mechanisms for one call; acme-platform/billing/pay.py:2" in text
    report(w, "arch-2", findings=[], previous=[{"id": 1, "status": "fixed", "note": "pay.py uses the client now"}])
    finish(w, "arch-2")
    run = w.store.run("arch-2")
    assert run.outcome == "clean" and codex.calls[1:] == []  # nothing found: nothing for Codex to check
    found = w.store.finding(1)
    assert (found.status, found.fixed_run) == ("fixed", 2)
    page = (w.cfg.architect.reports / "2" / "report.html").read_text()
    assert "Fixed since the last review" in page and "pay.py uses the client now" in page


def test_a_stopped_review_keeps_nothing_and_the_next_one_compares_with_the_last_complete_one(w, codex):
    first = push(w, "acme-platform", {"billing/pay.py": CODE})
    queue(w)
    report(w, findings=[])
    finish(w)
    push(w, "acme-platform", {"billing/a.py": "a = 1\n"})
    queue(w)
    report(w, "arch-2")
    w.store.update_run("arch-2", stop_requested=1)
    w.tick()
    stopped = w.store.run("arch-2")
    assert stopped.outcome == "stopped" and w.store.findings() == [] and codex.calls == []
    head = push(w, "acme-platform", {"billing/b.py": "b = 1\n"})
    queue(w)
    assert f"changes since run 1: `{first[:12]}...{head[:12]}`" in brief(w, "arch-3")


@pytest.mark.parametrize("theme,outcome", [
    ({}, "partial"),  # never mentioned
    ({"skipped": [{"repo": "acme-theme", "why": "ran out of time"}]}, "found"),  # said it skipped it
])
def test_a_repository_the_agent_did_not_read_is_read_by_the_next_review(w, codex, theme, outcome):
    first = push(w, "acme-platform", {"billing/pay.py": CODE})
    theme_sha = push(w, "acme-theme", {"theme/override.css": "a {}\n"})
    queue(w)
    fields = {"reviewed": [{"repo": "acme-platform", "what": "billing"}], **theme}
    report(w, **fields, **({} if outcome == "found" else {"findings": []}))
    codex.answer([{"finding": 1, "verdict": "confirmed", "reason": "yes"}])
    finish(w)
    run = w.store.run(RID)
    assert run.outcome == outcome
    # The theme was not read: it keeps where its unread changes start, its first commit ("").
    assert result(w)["covered"] == {"acme-platform": first, "acme-theme": ""}
    head = push(w, "acme-platform", {"billing/b.py": "b = 1\n"})
    queue(w)
    text = brief(w, "arch-2")
    assert f"`acme-platform` (main), changes since run 1: `{first[:12]}...{head[:12]}`" in text
    assert f"`acme-theme` (main), changes since its first commit (no review has read it yet): everything at " \
           f"`{theme_sha[:12]}`" in text
    report(w, "arch-2", findings=[], reviewed=[{"repo": "acme-platform", "what": "b"},
                                               {"repo": "acme-theme", "what": "the override"}])
    finish(w, "arch-2")
    assert w.store.run("arch-2").outcome == "clean"
    assert result(w, "arch-2")["covered"] == {"acme-platform": head, "acme-theme": theme_sha}


@pytest.mark.parametrize("theme", [{}, {"skipped": [{"repo": "acme-theme", "why": "ran out of time"}]}])
def test_changes_nobody_read_stay_to_read_however_late_the_next_review_comes(tmp_path, codex, theme):
    # The first look is one day. A week later the theme's commits are older than that window; read by nobody, they
    # must still be read, and not as "nothing changed".
    w = world(tmp_path, first_look="1d")
    try:
        push(w, "acme-platform", {"billing/pay.py": CODE})
        theme_sha = push(w, "acme-theme", {"theme/override.css": "a {}\n"})
        w.now = datetime.now(UTC)  # the commits are made now; the first look covers them
        queue(w)
        report(w, findings=[], reviewed=[{"repo": "acme-platform", "what": "billing"}], **theme)
        finish(w)
        assert w.store.run(RID).outcome == "partial"
        for week in (1, 2):
            w.now += timedelta(days=7)
            queue(w)
            rid = f"arch-{week + 1}"
            text = brief(w, rid)
            assert f"`acme-theme` (main), changes since its first commit (no review has read it yet): everything " \
                   f"at `{theme_sha[:12]}`" in text
            assert "`acme-platform`" in text.split("Unchanged since the last review", 1)[1]
            report(w, rid, findings=[], reviewed=[{"repo": "acme-platform", "what": "nothing new"}], **theme)
            finish(w, rid)
            assert w.store.run(rid).outcome == "partial"
            assert result(w, rid)["covered"]["acme-theme"] == ""
    finally:
        w.events.close()
        w.store.close()


# -- the second opinion --------------------------------------------------------------------------------

OBJECTION = ("STATE: COMPLETE\nVERDICT: FAIL\n\n1. Finding 1 claims three queries; the querysets are lazy and "
             "make one.")


def test_a_report_codex_disputed_at_the_gate_keeps_its_findings_for_the_second_opinion_to_judge(w, codex):
    # staging, run 1: the engine's Codex objected to one claim of one finding twice and settled the attempt as
    # needs-user; the runner dropped all seven findings as an unfinished run.
    push(w, "acme-platform", {"billing/pay.py": CODE})
    queue(w)
    report(w)
    codex.answer([{"finding": 1, "verdict": "doubtful", "reason": "One query, not three."}])
    w.set_engine(lambda s: s["tasks"][RID].update(state="failed", state_reason="needs-user", attempt_live=False,
                                                  receipt={"result": "succeeded_research", "review": "failed",
                                                           "commits": [], "review_why": OBJECTION}))
    w.tick()
    run = w.store.run(RID)
    assert (run.outcome, run.reason) == ("found", None)
    r = result(w)
    assert r["engine_review"] == "1. Finding 1 claims three queries; the querysets are lazy and make one."
    assert "the querysets are lazy" in codex.calls[0]["prompt"]  # the second opinion weighs the objection
    assert r["second_opinion"]["verdicts"]["1"]["verdict"] == "doubtful" and w.store.finding(1) is not None
    page = (w.cfg.architect.reports / "1" / "report.html").read_text()
    assert "report as a whole did not pass review" in page and "Doubtful on second check" in page
    assert "Codex" not in page


def test_needs_user_for_any_other_reason_is_still_an_unfinished_run(w, codex):
    push(w, "acme-platform", {"billing/pay.py": CODE})
    queue(w)
    report(w)
    # Passed the review, but left work uncommitted: the engine wants a person, and that is no finished review.
    w.set_engine(lambda s: s["tasks"][RID].update(state="failed", state_reason="needs-user", attempt_live=False,
                                                  receipt={"result": "succeeded_research", "review": "passed",
                                                           "commits": [], "review_why": ""}))
    w.tick()
    run = w.store.run(RID)
    assert run.outcome == "unfinished" and "needs-user" in run.reason
    assert w.store.findings() == [] and codex.calls == [] and "engine_review" not in result(w)


def test_codex_unavailable_is_said_never_taken_for_agreement(w, codex):
    push(w, "acme-platform", {"billing/pay.py": CODE})
    queue(w)
    report(w)
    codex.answer("exit")
    finish(w)
    r = result(w)
    assert w.store.run(RID).outcome == "found"
    assert r["second_opinion"]["status"] == "unavailable"
    assert r["second_opinion"]["why"] == ("the checker stopped with an error, exit 3 (its own message is in the "
                                          "runner's events, arch_second_opinion_failed)")
    # The tool's own words (its name, the quota) are the operator's, in the events; never in the report.
    [failed] = w.events_named("arch_second_opinion_failed")
    assert failed["exit"] == 3 and "usage limit is reached" in failed["error"]
    page = (w.cfg.architect.reports / "1" / "report.html").read_text()
    assert "did not run: the checker stopped with an error, exit 3" in page
    assert "usage limit" not in page and "Codex" not in page
    assert "on second check" not in page and "confirmed 0" not in page  # no verdict, no count


def test_a_checker_that_cannot_start_is_said_without_its_error(w, codex, monkeypatch):
    monkeypatch.setattr(architect, "CODEX", str(w.tmp / "no-such-checker"))
    push(w, "acme-platform", {"billing/pay.py": CODE})
    queue(w)
    report(w)
    finish(w)
    so = result(w)["second_opinion"]
    assert so["status"] == "unavailable" and so["why"].startswith("the checker could not be started (")
    assert "no-such-checker" not in so["why"]
    [failed] = w.events_named("arch_second_opinion_failed")
    assert failed["step"] == "start" and "no-such-checker" in failed["error"]


def test_a_stop_asked_while_the_findings_are_checked_skips_the_check_and_says_so(w, codex):
    push(w, "acme-platform", {"billing/pay.py": CODE})
    queue(w)
    report(w)
    found = {"summary": "", "reviewed": [{"repo": "acme-platform", "what": "x"}], "skipped": [], "previous": [],
             "findings": [{"title": "t", "where": []}], "problems": [], "fatal": [], "doubts": []}
    # The runner went down inside the check, and a person asked to stop before it came back.
    w.store.update_run(RID, state="reviewing", outcome="found", stop_requested=1,
                       result=json.dumps({**found, "second_opinion": {"status": "asking", "attempts": 1}}))
    w.tick()
    so = result(w)["second_opinion"]
    assert so == {"status": "skipped", "why": "the run was stopped"} and codex.calls == []
    page = (w.cfg.architect.reports / "1" / "report.html").read_text()
    assert "not run: the run was stopped" in page and "confirmed 0" not in page


def test_codex_that_does_not_answer_in_time_is_stopped_with_what_it_started(tmp_path, codex):
    w = world(tmp_path, second_opinion_timeout="1s")
    try:
        push(w, "acme-platform", {"billing/pay.py": CODE})
        queue(w)
        report(w)
        codex.answer("sleep")
        finish(w)
        so = result(w)["second_opinion"]
        assert so["status"] == "unavailable" and so["why"] == "no answer in 1 s"
        assert git(w.repo, "branch", "--show-current") == "main"
    finally:
        w.events.close()
        w.store.close()


def test_a_crash_during_the_second_opinion_asks_again_but_not_forever(w, codex):
    push(w, "acme-platform", {"billing/pay.py": CODE})
    queue(w)
    report(w)
    run = w.store.run(RID)
    found = {"summary": "", "reviewed": [{"repo": "acme-platform", "what": "x"}], "skipped": [], "previous": [],
             "findings": [{"title": "t", "where": []}], "problems": [], "fatal": [], "doubts": []}
    # The runner died twice inside the call: the attempts were counted before each.
    w.store.update_run(run.id, state="reviewing", outcome="found",
                       result=json.dumps({**found, "second_opinion": {"status": "asking", "attempts": 2}}))
    w.tick()
    so = result(w)["second_opinion"]
    assert so["status"] == "unavailable" and "2 attempts" in so["why"] and codex.calls == []
    assert w.store.run(RID).state == "done"


def test_verdicts_that_do_not_name_a_finding_once_are_left_out():
    path = Path(__file__).parent / "_verdicts.json"
    try:
        path.write_text(json.dumps({"verdicts": [
            {"finding": 1, "verdict": "confirmed", "reason": "ok"}, {"finding": 1, "verdict": "rejected", "reason": "x"},
            {"finding": 7, "verdict": "confirmed", "reason": "y"}, {"finding": 2, "verdict": "maybe", "reason": "z"}]}))
        verdicts, why = architect.read_verdicts(path, 2, lambda t: t)
    finally:
        path.unlink()
    assert verdicts == {"1": {"verdict": "confirmed", "reason": "ok"}}
    assert "1 finding got no verdict" in why and why.count("left out") == 3


# -- decisions on the reports page ----------------------------------------------------------------------

def test_a_decision_on_the_page_is_kept_and_told_to_the_next_review(w, codex):
    push(w, "acme-platform", {"billing/pay.py": CODE})
    queue(w)
    report(w)
    codex.answer([{"finding": 1, "verdict": "confirmed", "reason": "yes"}])
    finish(w)
    port, stop = arch_report.serve(w.cfg.architect.reports, "127.0.0.1", 0,
                                   architect.pages(w.cfg, w.runner.code_host), architect.decider(w.cfg, w.events))
    base = f"http://127.0.0.1:{port}"

    def get(path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(base + path) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""

    def post(path: str, fields: dict, headers: dict | None = None) -> int:
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(base + path, data=data, headers=headers or {}, method="POST")
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        status, page = get("/1/")
        assert status == 200 and "Accept the risk" in page and "False positive" in page
        token = page.split('name="token" value="', 1)[1].split('"', 1)[0]
        assert post("/findings/1", {"decision": "accepted", "note": "known", "token": "wrong"}) == 403
        assert post("/findings/1", {"decision": "accepted", "note": "known", "token": token},
                    {"Sec-Fetch-Site": "cross-site"}) == 403
        assert post("/findings/1", {"decision": "accepted", "note": "known", "token": token},
                    {"Origin": "https://evil.example"}) == 403
        assert post("/findings/1", {"decision": "accepted", "note": " ", "token": token}) == 400  # why is required
        assert post("/findings/9", {"decision": "accepted", "note": "x", "token": token}) == 404
        assert post("/findings/1", {"decision": "accepted", "note": "Billing is replaced next quarter",
                                    "token": token, "back": "/1/#f1"}) == 303
        found = w.store.finding(1)
        assert (found.decision, found.decision_note) == ("accepted", "Billing is replaced next quarter")
        assert "Risk accepted" in get("/1/")[1] and "Take the decision back" in get("/findings/")[1]
        assert "0 open findings" in get("/")[1] and "1 decided" in get("/")[1]
        for refused in ("/1/result.json", "/1/instructions.md", "/index.html", "/2/", "/../runner.db"):
            assert get(refused)[0] == 404, refused
    finally:
        stop()
    push(w, "acme-platform", {"billing/refund.py": "def refund():\n    pass\n"})
    queue(w)
    assert "(decided: the risk is accepted, because: Billing is replaced next quarter)" in brief(w, "arch-2")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# -- reading what the agent wrote ----------------------------------------------------------------------

def test_findings_are_read_defensively(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "findings.json").write_text(json.dumps({
        "summary": "s", "reviewed": [{"repo": "a", "what": "w"}, {"repo": "b"}],
        # staging, run 2: three repositories in one entry, and `reason` for `why`.
        "skipped": [{"repo": "c, d", "reason": "out of time"}, {"repo": ""}],
        "previous": [{"id": 3, "status": "fixed"}, {"id": 99, "status": "still"}, {"id": 4, "status": "gone"}],
        "findings": [{"title": "A rule nobody wrote", "kind": "violation", "rule": "", "same_as": 3,
                      "where": [{"repo": "a", "file": "x.py", "line": 1}, "nonsense"]},
                     {"title": "Continues something never told", "same_as": 42},
                     {"no": "title"}, {"title": "three"}, {"title": "four"}]}))
    found, problems = load_findings(out, told={3, 4}, max_findings=3)
    first, second = found["findings"][:2]
    assert (first["kind"], first["rule"], first["same_as"]) == ("proposal", "", 3)  # no rule written: a proposal
    assert first["where"] == [{"repo": "a", "file": "x.py", "line": 1, "note": ""}]
    assert second["same_as"] is None and len(found["findings"]) == 3
    assert found["previous"] == [{"id": 3, "status": "fixed", "note": ""}]
    assert found["reviewed"] == [{"repo": "a", "what": "w"}]
    assert found["skipped"] == [{"repo": "c", "why": "out of time"}, {"repo": "d", "why": "out of time"}]
    assert any("more than the 3 asked for" in p for p in problems)
    assert any("finding 3 has no readable title" in p for p in problems)
    assert sum("`previous` names" in p for p in problems) == 2
    (out / "findings.json").write_text("[1, 2]")
    assert load_findings(out, set(), 7) == ({}, ["the findings file is not a JSON object"])


@pytest.mark.parametrize("found,problems,outcome", [
    ({"reviewed": [], "findings": []}, [], "unfinished"),
    ({"reviewed": [{"repo": "a"}], "findings": [{"title": "t"}]}, ["x"], "found"),
    ({"reviewed": [{"repo": "a"}], "findings": []}, ["x"], "unfinished"),
    ({"reviewed": [{"repo": "a"}], "findings": [], "skipped": [{"repo": "b"}]}, [], "partial"),
    ({"reviewed": [{"repo": "a"}], "findings": []}, [], "clean"),
])
def test_nothing_to_raise_only_when_the_agent_said_what_it_read_and_the_report_holds_whole(found, problems, outcome):
    assert outcome_of(found, problems, [], False)[0] == outcome


def test_patterns_name_a_repository_or_paths_in_it():
    assert matches(["platform/*"], "platform/core")
    assert not matches(["platform/*"], "platform/core/sub")
    assert matches(["core:vendor/**"], "core", "vendor/lib/a.py")
    assert not matches(["core:vendor/**"], "core")  # a path pattern never names the whole repository
    assert matches(["*:*.min.js"], "web", "static/js/app.min.js")  # no / in the path: the file name anywhere
    assert not matches(["core:vendor/**"], "web", "vendor/a.py")


def test_a_report_escapes_what_the_agent_wrote():
    view = {"n": 2, "outcome": "found", "source": "command", "requested_by": "ops",
            "findings": [{"title": "<script>alert(1)</script>", "what": "<b>x</b>", "where": [],
                          "rule": "<img src=x onerror=alert(1)>"}],
            "second_opinion": {"status": "done", "verdicts": {"1": {"verdict": "rejected", "reason": "<i>no</i>"}}}}
    page = arch_report.render_run(view, {}, "acme", ZoneInfo("Europe/Kyiv"))
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "<b>x</b>" not in page and "<img" not in page and "<i>no</i>" not in page and "Not confirmed on second check" in page


# -- the rest of the runner sees it -------------------------------------------------------------------

def test_grafana_sees_the_agent_reviewing_architecture(w):
    push(w, "acme-platform", {"billing/pay.py": CODE})
    queue(w)
    m = Metrics()
    m.update_busy(w.store)
    assert m.registry.get_sample_value("runner_agent_busy") == 3
    m.update(w.runner)
    assert m.registry.get_sample_value("runner_task", {"task_id": RID, "state": "reviewing architecture: running",
                                                       "summary": "Architecture review, run 1"}) == 1


def test_the_check_is_ok_without_instructions_and_fails_on_an_unreadable_file(tmp_path):
    w = world(tmp_path, instructions="<!-- comments only, as setup installs it -->\n")
    try:
        check = architect_check(w.cfg)
        assert check.status == "ok" and "the runner's criteria only" in check.detail
        w.cfg.architect.instructions.write_bytes(b"\xff\xfe")
        assert architect_check(w.cfg).status == "fail"
    finally:
        w.events.close()
        w.store.close()


def test_the_example_instructions_are_comments_only():
    from arceus_runner.workspace_run import read_instructions
    example = Path(__file__).parents[2] / "config" / "architect-instructions.example.md"
    assert read_instructions(example, required=False) == ("", "")


def test_test_runs_recorded_before_the_shared_table_survive_the_migration(tmp_path):
    import sqlite3

    from arceus_runner.db import MIGRATIONS, ReviewStore, _split
    path = tmp_path / "runner.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for n, migration in enumerate(MIGRATIONS[:19], 1):  # the database as the test runs' release left it
        for stmt in _split(migration):
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {n}")
    conn.execute("INSERT INTO qa_runs (id, n, source, requested_by, slot, state, outcome) VALUES "
                 "('qa-1', 1, 'schedule', 'schedule', '2026-11-07T02:00:00+02:00', 'done', 'found')")
    conn.close()
    store = ReviewStore(path)
    try:
        run = store.run("qa-1")
        assert (run.kind, run.n, run.slot, run.outcome) == ("qa", 1, "2026-11-07T02:00:00+02:00", "found")
        assert store.add_run("qa", "schedule", "schedule", slot="2026-11-07T02:00:00+02:00") is None
        assert store.add_run("arch", "schedule", "schedule", slot="2026-11-07T02:00:00+02:00").id == "arch-1"
        assert store.add_run("qa", "command", "ops").id == "qa-2"
    finally:
        store.close()


def test_code_in_the_agents_text_is_set_as_code_and_still_escaped():
    view = {"n": 4, "outcome": "found", "source": "command", "requested_by": "srv-user-7",
            "summary": "Retries live in `billing/retry.py` and api/v1/client.py:40; see and/or the `<b>` tag. "
                       "Before ```x = 1``` and `after`.",
            "findings": [{"title": "Two retries", "what": "`send()` loops; odd ` stays", "where": [],
                          "components": ["billing"]}],
            "second_opinion": {"status": "done", "verdicts": {}}}
    page = arch_report.render_run(view, {}, "acme", ZoneInfo("Europe/Kyiv"))
    assert '<code class="lit">billing/retry.py</code>' in page
    assert '<code class="lit">api/v1/client.py:40</code>' in page
    assert '<code class="lit">&lt;b&gt;</code>' in page and "<b>" not in page
    assert "and/or" in page and '<code class="lit">send()</code> loops; odd ` stays' in page
    # A fenced block is one piece of code, and the inline span after it is another.
    assert 'Before <code class="lit">x = 1</code> and <code class="lit">after</code>.' in page
    # A finding the second check gave no verdict is counted apart, never as not confirmed.
    assert "confirmed 0 of 1, 1 without a verdict" in page and "Not confirmed" not in page
    assert "srv-user-7" not in page  # who typed the command on the server says nothing
