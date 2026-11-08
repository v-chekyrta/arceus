"""Architecture reviews in the console: the list, a review's report, decisions on findings by role, who decided and
when, the history of decisions, and two people deciding at once."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("starlette")

from arceus_runner.console.store import ConsoleStore  # noqa: E402
from arceus_runner.console.web import Settings  # noqa: E402
from arceus_runner.db import ReviewStore  # noqa: E402

from tests.conftest import example_raw  # noqa: E402
from tests.test_console_auth import csrf_of, make_admin, post, signed_in  # noqa: E402
from tests.test_console_qa import person  # noqa: E402

FINDING = {"title": "Refunds retry with a wrapper of their own", "lens": "coherence", "kind": "violation",
           "rule": "AGENTS.md of billing: every outgoing call goes through http/client.py", "severity": "medium",
           "what": "refunds/retry.py retries on any exception", "risk": "A rejected refund is sent again",
           "direction": "Call the shared client", "where": [{"repo": "billing", "file": "refunds/retry.py", "line": 14}]}


@pytest.fixture
def w(tmp_path: Path):
    raw = example_raw()
    raw["paths"] = {"db": str(tmp_path / "runner.db"), "secrets": str(tmp_path / "secrets.env")}
    raw["architect"] = {"enabled": True, "reports": str(tmp_path / "arch"),
                        "instructions": str(tmp_path / "arch-instructions.md")}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    settings = Settings(config_path=config, db_path=tmp_path / "console" / "console.db")
    make_admin(settings)
    s = ReviewStore(tmp_path / "runner.db")
    run = s.add_run("arch", "command", "ai")
    s.add_finding(FINDING["title"], json.dumps(FINDING), 1)
    s.add_finding("Tests mock the service they test", json.dumps({**FINDING, "title": "Tests mock the service"}), 1)
    s.update_run(run.id, state="done", outcome="found", started_at="2026-11-07T10:00:00Z",
                 finished_at="2026-11-07T11:00:00Z", snapshot=json.dumps({"repos": []}),
                 result=json.dumps({"summary": "Two findings in billing.", "findings": [FINDING, FINDING],
                                    "ledger": {"findings": [1, 2], "new": [1, 2]},
                                    "second_opinion": {"status": "done", "verdicts": {
                                        "1": {"verdict": "confirmed", "reason": "retry.py:14 does catch Exception"}}}}))
    s.close()

    class W:
        pass
    x = W()
    x.settings, x.db = settings, tmp_path / "runner.db"
    return x


def version_of(page: str, n: int) -> str:
    form = page.split(f'action="/architecture/findings/{n}"', 1)[1]
    return re.search(r'name="version" value="([^"]*)"', form).group(1)


def test_the_list_shows_the_reviews_and_the_findings_strip(w):
    viewer = person(w.settings, "v@example.com", set())
    page = viewer.get("/architecture/").text
    assert "Run 1" in page and "2 findings" in page and "Two findings in billing." in page
    assert "2 open findings" in page and "0 decided" in page
    assert "Start a review" not in page


def test_the_report_shows_codexs_check_apart_from_the_persons_decision(w):
    arch = person(w.settings, "a@example.com", {"architect"})
    page = arch.get("/architecture/1/").text
    assert "Architecture review, run 1" in page and FINDING["title"] in page
    assert "Confirmed on second check" in page and "does catch Exception" in page
    assert "Accept the risk" in page and "False positive" in page
    assert 'action="/architecture/findings/1"' in page and 'name="csrf"' in page


def test_an_architect_decides_and_everyone_sees_who_when_and_why(w):
    arch = person(w.settings, "a@example.com", {"architect"})
    page = arch.get("/architecture/1/").text
    r = arch.post("/architecture/findings/1", data={"csrf": csrf_of(page), "decision": "accepted",
                                                     "note": "Billing is replaced next quarter",
                                                     "version": version_of(page, 1), "back": "/architecture/1/#f1"})
    assert r.status_code == 303 and r.headers["location"].endswith("#f1")
    viewer = person(w.settings, "v@example.com", set())
    page = viewer.get("/architecture/findings/").text
    assert "Risk accepted" in page and "Billing is replaced next quarter" in page
    assert "A &lt;a@example.com&gt;" in page or "a@example.com" in page  # who
    assert "Decisions on it (1)" in page
    assert "Take the decision back" not in page and "Accept the risk" not in page  # a viewer has no buttons
    s = ReviewStore.read_only(w.db)
    f = s.finding(1)
    assert f.decision == "accepted" and "a@example.com" in f.decided_by and f.decided_at
    s.close()
    with ConsoleStore(w.settings.db_path) as cs:
        assert any(j["action"] == "arch_decided" and j["target"] == "finding:1" for j in cs.journal())


def test_a_reason_is_required_to_accept_or_mark_a_false_positive(w):
    arch = person(w.settings, "a@example.com", {"architect"})
    page = arch.get("/architecture/1/").text
    r = arch.post("/architecture/findings/1", data={"csrf": csrf_of(page), "decision": "false_positive", "note": " ",
                                                     "version": version_of(page, 1)})
    assert r.status_code == 400 and "Say why" in r.text
    s = ReviewStore.read_only(w.db)
    assert s.finding(1).decision is None
    s.close()


def test_two_people_deciding_at_once_do_not_overwrite_each_other(w):
    a = person(w.settings, "a@example.com", {"architect"})
    b = signed_in(w.settings, "owner@example.com")  # an admin, who may decide too
    page_a, page_b = a.get("/architecture/findings/").text, b.get("/architecture/findings/").text
    r = a.post("/architecture/findings/1", data={"csrf": csrf_of(page_a), "decision": "accepted", "note": "first",
                                                  "version": version_of(page_a, 1)})
    assert r.status_code == 303
    r = b.post("/architecture/findings/1", data={"csrf": csrf_of(page_b), "decision": "false_positive",
                                                  "note": "second", "version": version_of(page_b, 1)})
    assert r.status_code == 409 and "changed since your page was opened" in r.text and "risk accepted by" in r.text
    s = ReviewStore.read_only(w.db)
    assert (s.finding(1).decision, s.finding(1).decision_note) == ("accepted", "first")
    s.close()
    # Reloaded, the admin sees the decision and takes it back; both stay in the history.
    page_b = b.get("/architecture/findings/").text
    r = b.post("/architecture/findings/1", data={"csrf": csrf_of(page_b), "decision": "open",
                                                  "version": version_of(page_b, 1)})
    assert r.status_code == 303
    page = b.get("/architecture/findings/").text
    assert "Decisions on it (2)" in page and "Taken back" in page


@pytest.mark.parametrize("roles", [set(), {"qa"}, {"operator"}])
def test_people_without_the_architect_role_cannot_decide_by_posting(w, roles):
    c = person(w.settings, f"{'-'.join(sorted(roles)) or 'x'}@example.com", roles)
    page = c.get("/architecture/1/").text
    assert "Accept the risk" not in page and "Nobody has decided on it yet" in page
    r = c.post("/architecture/findings/1", data={"csrf": csrf_of(page), "decision": "accepted", "note": "mine",
                                                  "version": ""})
    assert r.status_code == 403
    assert c.post("/architecture/", data={"csrf": csrf_of(page)}).status_code == 403
    assert c.get("/architecture/instructions/").status_code == 403
    s = ReviewStore.read_only(w.db)
    assert s.finding(1).decision is None and len(s.runs("arch")) == 1
    s.close()


def test_architects_start_and_stop_a_review_and_edit_its_instructions(w):
    arch = person(w.settings, "a@example.com", {"architect"})
    r = post(arch, "/architecture/", {})
    assert r.status_code == 303
    s = ReviewStore.read_only(w.db)
    assert s.run("arch-2").requested_by == "a@example.com"
    s.close()
    assert post(arch, "/architecture/2/stop", {}, page="/architecture/").status_code == 303
    page = arch.get("/architecture/instructions/").text
    assert "Review instructions" in page and "the file does not exist" in page
    sha = re.search(r'name="sha" value="([^"]+)"', page).group(1)
    r = post(arch, "/architecture/instructions/", {"text": "Billing is legacy: leave it alone.", "sha": sha})
    assert r.status_code == 303


def test_the_decision_goes_back_only_to_a_page_of_the_reviews(w):
    arch = person(w.settings, "a@example.com", {"architect"})
    page = arch.get("/architecture/findings/").text
    r = arch.post("/architecture/findings/1", data={"csrf": csrf_of(page), "decision": "accepted", "note": "x",
                                                     "version": version_of(page, 1), "back": "https://evil.example/"})
    assert r.status_code == 303 and r.headers["location"].startswith("/architecture/findings/?")
