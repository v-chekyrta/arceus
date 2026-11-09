"""The console's home page: what the factory does now, and nothing shown as fine that is not known."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import yaml

pytest.importorskip("starlette")
from prometheus_client import generate_latest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from arceus_runner.console import status  # noqa: E402
from arceus_runner.console.web import Settings, create_app  # noqa: E402
from arceus_runner.db import ReviewStore  # noqa: E402
from arceus_runner.metrics import Metrics  # noqa: E402

from tests.conftest import example_raw  # noqa: E402
from tests.test_console_auth import make_admin, post, signed_in, register, user_id  # noqa: E402


@pytest.fixture
def world(tmp_path: Path, monkeypatch):
    raw = example_raw()
    raw["paths"] = {"db": str(tmp_path / "runner.db"), "secrets": str(tmp_path / "secrets.env"),
                    "events": str(tmp_path / "events.jsonl")}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    settings = Settings(config_path=config, db_path=tmp_path / "console" / "console.db")
    make_admin(settings)

    class World:
        metrics_text: str | None = None
        db = tmp_path / "runner.db"
        events = tmp_path / "events.jsonl"

    w = World()
    w.settings = settings

    def fake_get(url, timeout=None):
        if w.metrics_text is None:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, text=w.metrics_text, request=httpx.Request("GET", url))
    monkeypatch.setattr(status.httpx, "get", fake_get)
    return w


def metrics(last_tick: float | None = None, health: dict | None = None, can_take: bool = True,
            usage: dict | None = None, mode: str = "live") -> str:
    m = Metrics()
    m.info.labels("0.1.0", mode, "abc").set(1)
    m.last_tick.set(last_tick if last_tick is not None else time.time() - 20)
    for check, value in (health if health is not None else {"disk_free": 0}).items():
        m.health.labels(check).set(value)
    m.can_take.set(1 if can_take else 0)
    for (provider, window), pct in (usage or {}).items():
        m.usage.labels(provider, window).set(pct)
    return generate_latest(m.registry).decode()


def task(store: ReviewStore, key: str, state: str, summary: str = "") -> None:
    store.create_task(task_id=key, issue_db_id=f"db-{key}", state=state, summary=summary, tag_author="ivan",
                      repo="", repo_path="/w", branch=f"agent/{key}", base_branch="main")


def home(w) -> str:
    c = signed_in(w.settings, "owner@example.com")
    r = c.get("/")
    assert r.status_code == 200
    return r.text


def test_working_on_a_ticket_says_so_with_the_queue_people_and_usage(world):
    world.metrics_text = metrics(usage={("claude", "5h"): 42.0, ("claude", "7d"): 91.0, ("codex", "5h"): 10.0})
    s = ReviewStore(world.db)
    task(s, "ACME-12", "dispatched", "Fix the refund retry")
    task(s, "ACME-7", "needs_input", "Which currency?")
    task(s, "ACME-3", "code_review", "Add the export")
    s.record_trigger("ACME-20", "tag-1", "ivan", True, 1)
    s.close()
    page = home(world)
    assert "Working on ACME-12: Fix the refund retry" in page
    assert 'href="https://youtrack.example.com/issue/ACME-12"' in page
    assert "Question for people" in page and "ACME-7" in page
    assert "ACME-20" in page  # the queue
    assert "Running" in page and "last pass" in page and "mode live" in page
    assert "42%" in page and "91%" in page and 'class="meter bad"' in page
    assert "Codex, 7 days" in page and "not read" in page  # a window the engine did not report is not 0%
    assert "Every check passes" in page


def test_a_failing_check_shows_its_reason_however_long_ago_it_began(world):
    """The reason comes from what the runner itself recorded (runner_state), not from the tail of events.jsonl: a
    failure a week old is still explained after megabytes of other events."""
    world.metrics_text = metrics(health={"disk_free": 2, "service.youtrack": 0, "devstack": 1}, can_take=False)
    world.events.write_text("".join(json.dumps({"ts": "2026-11-08T12:00:00Z", "event": "engine_step", "n": i}) + "\n"
                                    for i in range(40000)))  # several MB of ordinary events, no health_changed
    s = ReviewStore(world.db)
    s.record_trigger("ACME-20", "tag-1", "ivan", True, 1)
    s.set_state("health", {"disk_free": {"status": "fail", "detail": "8% free on /", "since": "2026-11-01T09:00:00"},
                           "service.youtrack": {"status": "ok", "detail": "3 queued", "since": None}})
    s.close()
    assert world.events.stat().st_size > 2_000_000
    page = home(world)
    assert "1 queued, but new work is held while a health check fails" in page
    assert "disk_free" in page and "8% free on /" in page and "Since 01 Nov 2026" in page
    assert "takes no new work" in page
    # A check whose reason nobody recorded says so instead of showing nothing.
    assert "devstack" in page and "The runner has not recorded why." in page


def test_the_runner_records_each_checks_status_and_detail_for_the_console(cfg, secrets):
    from arceus_runner.checks import Check
    from tests.test_loop import World
    runner = World(cfg, secrets).runner
    runner._record_health([Check("disk_free", "warn", "22% free"), Check("service.youtrack", "ok", "2 queued")])
    first, _ = runner.store.state("health")
    assert first["disk_free"]["status"] == "warn" and first["disk_free"]["detail"] == "22% free"
    since = first["disk_free"]["since"]
    runner._record_health([Check("disk_free", "warn", "21% free")])
    again, _ = runner.store.state("health")
    assert again["disk_free"]["detail"] == "21% free" and again["disk_free"]["since"] == since  # same status
    runner._record_health([Check("disk_free", "fail", "8% free")])
    assert runner.store.state("health")[0]["disk_free"]["status"] == "fail"


def test_a_runner_that_answers_but_has_not_finished_a_pass_is_unknown_not_fine(world):
    """Its metrics answer from the start, with no pass and no checks yet: nothing about health is known."""
    world.metrics_text = generate_latest(Metrics().registry).decode()  # a fresh runner's metrics, nothing set
    ReviewStore(world.db).close()
    page = home(world)
    assert "The runner is starting" in page and "Starting" in page
    assert "Running" not in page and "Every check passes" not in page and "Idle" not in page
    assert page.count("Unknown until the runner's first pass ends") == 2
    world.metrics_text = metrics(health={})
    page = home(world)
    assert "Unknown: the runner reported no checks" in page and "Every check passes" not in page


def test_a_runner_that_does_not_answer_is_never_shown_as_fine(world):
    world.metrics_text = None
    ReviewStore(world.db).close()
    page = home(world)
    assert "The runner is not answering" in page
    assert "Every check passes" not in page
    assert page.count("Unknown: the runner does not answer") == 2  # health and usage


def test_a_stuck_runner_is_said(world):
    world.metrics_text = metrics(last_tick=time.time() - 3600)
    ReviewStore(world.db).close()
    assert "The runner is stuck" in home(world)


def test_no_runner_database_and_a_broken_config_each_say_so(world):
    world.metrics_text = metrics()
    page = home(world)
    assert "The runner&#39;s state cannot be read" in page and "no runner database" in page
    assert "Idle" not in page
    raw = yaml.safe_load(world.settings.config_path.read_text())
    raw["mode"] = "sometimes"
    world.settings.config_path.write_text(yaml.safe_dump(raw))
    page = home(world)
    assert "config.yaml has errors" in page and "mode" in page


def test_idle_and_observe_are_told_apart(world):
    world.metrics_text = metrics()
    ReviewStore(world.db).close()
    assert "Idle: nothing is queued" in home(world)
    world.metrics_text = metrics(mode="observe")
    assert "Observing: the runner takes no work" in home(world)


def test_the_status_part_refreshes_alone_and_asks_to_sign_in_when_the_session_is_gone(world):
    world.metrics_text = metrics()
    ReviewStore(world.db).close()
    c = signed_in(world.settings, "owner@example.com")
    r = c.get("/live")
    assert r.status_code == 200 and "<html" not in r.text and "Idle" in r.text
    assert TestClient(create_app(world.settings)).get("/live").status_code == 401
    assert 'data-refresh="/live"' in c.get("/").text


def test_grafana_is_told_who_asks_and_never_lets_in_someone_without_a_session(world):
    app = create_app(world.settings)
    assert TestClient(app).get("/_auth/grafana").status_code == 401
    admin = signed_in(world.settings, "owner@example.com")
    r = admin.get("/_auth/grafana")
    assert r.status_code == 200 and r.headers["x-arceus-user"] == "owner@example.com"
    assert r.headers["x-arceus-role"] == "Admin"
    register(TestClient(app, follow_redirects=False), "viewer@example.com", name="Vera")
    post(admin, f"/users/{user_id(world.settings, 'viewer@example.com')}/", {"what": "approve", "role": ["qa"]},
         page="/users/")
    vera = signed_in(world.settings, "viewer@example.com")
    r = vera.get("/_auth/grafana")
    assert r.headers["x-arceus-user"] == "viewer@example.com" and r.headers["x-arceus-role"] == "Viewer"
    assert 'href="/grafana/"' in vera.get("/").text  # Monitoring in the bar


def test_grafana_opens_in_a_new_tab_and_the_footer_names_no_version(world):
    import re as _re
    world.metrics_text = metrics()
    ReviewStore(world.db).close()
    page = home(world)
    grafana = _re.findall(r'<a href="(/grafana/[^"]*)"([^>]*)>(.*?)</a>', page)
    assert len(grafana) == 4  # Monitoring in the bar and the three dashboards
    for href, attrs, text in grafana:
        assert 'target="_blank"' in attrs and 'rel="noopener"' in attrs, href
        assert "(opens in a new tab)" in text, href
    others = [a for a in _re.findall(r'<a href="(/[^"]*)"([^>]*)>', page) if not a[0].startswith("/grafana/")]
    assert others and all("target=" not in attrs for _, attrs in others)  # the console's own pages: same tab
    assert '<footer class="foot">Arceus Console</footer>' in page and "0.1.0" not in page
