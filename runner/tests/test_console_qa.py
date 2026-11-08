"""Test runs in the console: the list, a run's report with its media, only the report's own files, starting and
stopping a run by role, and the test instructions."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from arceus_runner.console.store import ConsoleStore  # noqa: E402
from arceus_runner.console.web import Settings, create_app  # noqa: E402
from arceus_runner.db import ReviewStore  # noqa: E402

from tests.conftest import example_raw  # noqa: E402
from tests.test_console_auth import PASSWORD, csrf_of, make_admin, post, signed_in  # noqa: E402

SHOT = "0123456789abcdef.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def person(settings: Settings, email: str, roles: set[str]) -> TestClient:
    with ConsoleStore(settings.db_path) as s:
        token = s.make_link("invite", email, roles, actor="test")
        s.use_link(token, PASSWORD, email.split("@")[0].title())
    return signed_in(settings, email)


@pytest.fixture
def w(tmp_path: Path):
    raw = example_raw()
    reports = tmp_path / "qa" / "runs"
    raw["paths"] = {"db": str(tmp_path / "runner.db"), "secrets": str(tmp_path / "secrets.env")}
    raw["qa"] = {"enabled": True, "reports": str(reports), "instructions": str(tmp_path / "qa" / "instructions.md")}
    raw["mode"] = "live"
    raw["security"] = {"reviewers": ["ivan"]}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    settings = Settings(config_path=config, db_path=tmp_path / "console" / "console.db")
    make_admin(settings)

    store = ReviewStore(tmp_path / "runner.db")
    run = store.add_run("qa", "schedule", "schedule")
    store.update_run(run.id, state="done", outcome="found", started_at="2026-11-08T06:00:00Z",
                     finished_at="2026-11-08T07:40:00Z",
                     result=json.dumps({"summary": "Signing in is broken.", "findings": [{}]}))
    store.close()
    folder = reports / "1"
    (folder / "media").mkdir(parents=True)
    (folder / "media" / SHOT).write_bytes(PNG)
    (folder / "instructions.md").write_text("admin / hunter2\n")
    view = {"n": 1, "outcome": "found", "source": "schedule", "started_at": "2026-11-08T06:00:00Z",
            "finished_at": "2026-11-08T07:40:00Z", "summary": "Signing in is broken.",
            "instructions": {"path": "qa/instructions.md", "sha256": "ab" * 32},
            "tested": [{"what": "Signing in", "result": "problem"}, {"what": "The catalogue", "result": "ok"}],
            "findings": [{"title": "The Sign in button does nothing", "severity": "critical",
                          "steps": ["Open `/login`", "Type the test account", "Press Sign in"], "failed_step": 3,
                          "evidence": [{"file": SHOT, "step": 3, "caption": "Nothing happens", "kind": "image"}],
                          "expected": "The dashboard opens", "actual": "The page stays as it is",
                          "cause": "The form posts to a route that was renamed",
                          "code": [{"repo": "web", "file": "src/login.ts", "line": 12, "sha": "c0ffee" * 6,
                                    "url": "https://gitlab.example.com/web/-/blob/x/src/login.ts#L12"}]}]}
    (folder / "result.json").write_text(json.dumps(view))
    (folder / "report.html").write_text("<p>report</p>")

    class W:
        pass
    x = W()
    x.settings, x.tmp, x.config, x.raw, x.reports = settings, tmp_path, config, raw, reports
    x.instructions = tmp_path / "qa" / "instructions.md"
    return x


def test_the_list_shows_every_run_and_a_queued_one_at_once(w):
    viewer = person(w.settings, "v@example.com", set())
    page = viewer.get("/qa/").text
    assert "Run 1" in page and "1 problem found" in page and "Signing in is broken." in page
    assert 'href="/qa/1/"' in page and "Schedule" in page
    assert "Start a test run" not in page and "Test instructions" not in page  # no role: no buttons
    s = ReviewStore(w.tmp / "runner.db")
    s.add_run("qa", "command", "q@example.com")
    s.close()
    page = viewer.get("/qa/").text
    assert "Run 2" in page and "Queued" in page and "q@example.com" in page


def test_with_qa_off_the_old_reports_stay_and_the_page_says_why_none_start(w):
    w.raw["qa"]["enabled"] = False
    w.config.write_text(yaml.safe_dump(w.raw))
    qa = person(w.settings, "q@example.com", {"qa"})
    page = qa.get("/qa/").text
    assert "off in config.yaml" in page and "Run 1" in page
    assert "Start a test run" not in page
    r = qa.post("/qa/", data={"csrf": csrf_of(page)})
    assert r.status_code == 400 and "nothing was queued" in r.text


def test_a_run_opens_as_its_report_with_its_screenshot(w):
    viewer = person(w.settings, "v@example.com", set())
    page = viewer.get("/qa/1/").text
    for words in ("Platform test, run 1", "The Sign in button does nothing", "Critical", "Press Sign in",
                  "The dashboard opens", "The page stays as it is", "Suspected cause in the code",
                  "https://gitlab.example.com/web/-/blob/x/src/login.ts#L12", "What was checked", "The catalogue"):
        assert words in page, words
    assert f'src="media/{SHOT}"' in page
    r = viewer.get(f"/qa/1/media/{SHOT}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
    assert viewer.get("/qa/7/").status_code == 404


@pytest.mark.parametrize("path", ["/qa/1/instructions.md", "/qa/1/result.json", "/qa/1/report.html",
                                  "/qa/1/media/../result.json", "/qa/1/media/..%2Fresult.json",
                                  "/qa/1/media/%2e%2e%2finstructions.md", "/qa/1/media/notours.png",
                                  "/qa/1/media/0123456789abcdef.sh", "/qa/../config.yaml", "/qa/1/media/"])
def test_only_the_reports_own_files_are_served(w, path):
    viewer = person(w.settings, "v@example.com", set())
    r = viewer.get(path)
    assert r.status_code == 404, (path, r.status_code)
    assert b"hunter2" not in r.content and b"sha256" not in r.content


def test_qa_starts_and_stops_a_run_and_it_records_who_asked(w):
    qa = person(w.settings, "q@example.com", {"qa"})
    r = post(qa, "/qa/", {})
    assert r.status_code == 303 and "Run+2+queued" in r.headers["location"]
    s = ReviewStore.read_only(w.tmp / "runner.db")
    run = s.run("qa-2")
    assert run.state == "pending" and run.source == "command" and run.requested_by == "q@example.com"
    s.close()
    r = post(qa, "/qa/", {})
    assert r.status_code == 400 and "Run 2 is queued: one at a time" in r.text
    r = post(qa, "/qa/2/stop", {}, page="/qa/")
    assert r.status_code == 303
    s = ReviewStore.read_only(w.tmp / "runner.db")
    assert s.run("qa-2").stop_requested == 1
    s.close()
    with ConsoleStore(w.settings.db_path) as cs:
        assert {j["action"] for j in cs.journal()} >= {"qa_run_queued", "qa_run_stop_asked"}


def test_others_cannot_start_or_stop_a_run_by_posting_directly(w):
    for roles in (set(), {"architect"}, {"operator"}):
        c = person(w.settings, f"{'-'.join(sorted(roles)) or 'none'}@example.com", roles)
        token = csrf_of(c.get("/qa/").text)
        assert c.post("/qa/", data={"csrf": token}).status_code == 403
        assert c.post("/qa/1/stop", data={"csrf": token}).status_code == 403
    s = ReviewStore.read_only(w.tmp / "runner.db")
    assert [r.n for r in s.runs("qa")] == [1]
    s.close()


def test_the_instructions_are_edited_by_qa_and_hidden_from_everyone_else(w):
    w.instructions.write_text("<!-- what to test -->\nhttps://staging.example.com, admin / hunter2\n")
    w.instructions.chmod(0o640)
    for roles in (set(), {"architect"}, {"operator"}):
        c = person(w.settings, f"x{'-'.join(sorted(roles))}@example.com", roles)
        r = c.get("/qa/instructions/")
        assert r.status_code == 403 and "hunter2" not in r.text
    qa = person(w.settings, "q@example.com", {"qa"})
    page = qa.get("/qa/instructions/").text
    assert "hunter2" in page and "bytes of instructions" in page
    r = post(qa, "/qa/instructions/", {"text": "https://staging.example.com\r\nadmin / swordfish\r\n",
                                       "sha": re.search(r'name="sha" value="([^"]+)"', page).group(1),
                                       "note": "new password"})
    assert r.status_code == 303
    assert w.instructions.read_text() == "https://staging.example.com\nadmin / swordfish\n"
    assert w.instructions.stat().st_mode & 0o777 == 0o640  # the file keeps its own mode
    assert not list(w.instructions.parent.glob(".*tmp"))  # no temporary file left beside it
    page = qa.get("/qa/instructions/").text
    assert "new password" in page and "Quinn" not in page


def test_a_save_over_a_newer_version_is_refused_with_the_difference(w):
    w.instructions.write_text("first\n")
    qa = person(w.settings, "q@example.com", {"qa"})
    page = qa.get("/qa/instructions/").text
    sha = re.search(r'name="sha" value="([^"]+)"', page).group(1)
    w.instructions.write_text("changed over SSH\n")  # someone else, meanwhile
    r = qa.post("/qa/instructions/", data={"csrf": csrf_of(page), "sha": sha, "text": "mine\n"})
    assert r.status_code == 409 and "Someone changed the file" in r.text
    assert "-changed over SSH" in r.text and "+mine" in r.text
    assert w.instructions.read_text() == "changed over SSH\n"
    # The page now carries the new version's hash: saving again replaces it, knowingly.
    sha2 = re.search(r'name="sha" value="([^"]+)"', r.text).group(1)
    r = qa.post("/qa/instructions/", data={"csrf": csrf_of(r.text), "sha": sha2, "text": "mine\n"})
    assert r.status_code == 303 and w.instructions.read_text() == "mine\n"


def test_every_saved_version_is_kept_and_an_earlier_one_comes_back(w):
    w.instructions.write_text("v1\n")
    admin = signed_in(w.settings, "owner@example.com")
    for text in ("v2\n", "v3\n"):
        page = admin.get("/qa/instructions/").text
        post(admin, "/qa/instructions/", {"text": text, "sha": re.search(r'name="sha" value="([^"]+)"', page).group(1)})
    with ConsoleStore(w.settings.db_path) as cs:
        versions = cs.versions("qa.instructions")
    assert [v["after_text"] for v in versions] == ["v3\n", "v2\n"]
    assert versions[1]["before_text"] == "v1\n" and versions[0]["actor"].startswith("Owner")
    page = admin.get(f"/qa/instructions/?version={versions[1]['id']}").text
    assert "It is not saved yet" in page and ">v2\n</textarea>" in page


def test_the_page_warns_when_the_agent_would_get_nothing(w):
    w.instructions.write_text("<!-- comments only -->\n")
    qa = person(w.settings, "q@example.com", {"qa"})
    page = qa.get("/qa/instructions/").text
    assert "is empty (comments only)" in page and "a run would stop at once" in page

