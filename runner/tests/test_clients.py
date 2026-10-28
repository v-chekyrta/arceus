from __future__ import annotations

from datetime import date

import httpx

from arceus_runner.checks import _guard, check_tracker
from arceus_runner.gitlab import GitLab, check_gitlab, gitlab_client
from arceus_runner.integrations import build_tracker
from arceus_runner.youtrack import YouTrack, query_value, youtrack_client

from .fakes import FakeGitLab, FakeYouTrack


def yt(cfg, secrets, fake) -> YouTrack:
    return YouTrack(youtrack_client(cfg.tracker, secrets.tracker, cfg.http, transport=httpx.MockTransport(fake)))


def gl(cfg, secrets, fake) -> GitLab:
    return GitLab(gitlab_client(cfg.code_host, secrets.code_host, cfg.http, transport=httpx.MockTransport(fake)))


def check_youtrack(cfg, secrets, fake):
    return check_tracker(cfg, build_tracker(cfg, secrets, transport=httpx.MockTransport(fake)))


def test_more_than_42_issues_are_all_read(cfg, secrets):
    fake = FakeYouTrack()
    for n in range(1, 251):
        fake.add_issue(n, ("a", "j.doe", "+agent"))
    issues = yt(cfg, secrets, fake).queued_issues("ACME", "agent", "State", "To Do")
    assert [i.key for i in issues] == [f"ACME-{n}" for n in range(1, 251)]
    assert len([r for r in fake.requests if r.url.path == "/api/issues"]) == 3
    q = fake.requests[0].url.params["query"]
    assert q == "project: {ACME} tag: {agent} State: {To Do} sort by: created asc"


def test_query_value_refuses_braces():
    assert query_value("In Progress") == "{In Progress}"
    try:
        query_value("a}b")
    except ValueError:
        pass
    else:
        raise AssertionError


def test_tag_setter_follows_cursor_and_last_setter_wins(cfg, secrets):
    fake = FakeYouTrack()
    events = [(f"x{i}", "noise", "+other") for i in range(150)]
    events += [("a1", "j.doe", "+agent"), ("a2", "j.doe", "-agent"), ("a3", "mallory", "+agent")]
    db_id = fake.add_issue(1, *events)
    setter = yt(cfg, secrets, fake).tag_setter(db_id, "agent")
    assert (setter.activity_id, setter.author) == ("a3", "mallory")
    assert len([r for r in fake.requests if r.url.path.endswith("activitiesPage")]) == 2


def test_tag_removed_last_means_no_setter(cfg, secrets):
    fake = FakeYouTrack()
    db_id = fake.add_issue(1, ("a1", "j.doe", "+agent"), ("a2", "a.smith", "-agent"))
    assert yt(cfg, secrets, fake).tag_setter(db_id, "agent") is None


def test_youtrack_check_reports_each_mistake_separately(cfg, secrets):
    fake = FakeYouTrack()
    fake.states = ["Open", "In Progress", "Code Review"]  # queued state "To Do" is missing
    fake.tags.discard("agent-failed")
    fake.users.discard("a.smith")
    results = {c.name: c for c in check_youtrack(cfg, secrets, fake)}
    failed = sorted(n for n, c in results.items() if c.status == "fail")
    assert failed == [
        "security.reviewers.a.smith",  # a reviewer must be a real user too
        "tracker.allowed_users.a.smith",
        "youtrack.states.queued",
        "youtrack.tags.failed",
    ]
    assert "available: Open, In Progress, Code Review" in results["youtrack.states.queued"].detail


def test_youtrack_check_wrong_bot(cfg, secrets):
    results = {c.name: c for c in check_youtrack(cfg, secrets, FakeYouTrack(login="someone"))}
    assert results["youtrack.bot_user"].status == "fail"
    assert "token belongs to 'someone'" in results["youtrack.bot_user"].detail


def test_youtrack_check_auth_failure(cfg, secrets):
    fake = FakeYouTrack()
    fake.fail["/api/users/me"] = 401
    [c] = check_youtrack(cfg, secrets, fake)
    assert (c.name, c.status) == ("youtrack.auth", "fail")
    assert secrets.tracker not in c.detail


def test_gitlab_check(cfg, secrets):
    fake = FakeGitLab()
    fake.repos = ["acme-theme", "acme-infra"]
    fake.token = {"scopes": ["read_api"], "expires_at": "2026-10-30"}
    results = {c.name: c for c in check_gitlab(cfg.code_host, gl(cfg, secrets, fake), 14, today=date(2026, 10, 23))}
    assert results["gitlab.bot_user"].status == "ok"
    assert "gitlab.default_repo" not in results
    assert results["gitlab.token_scopes"].status == "fail"
    assert (results["gitlab.token_expiry"].status, results["gitlab.token_expiry"].detail) == (
        "warn", "expires 2026-10-30 (7 days)")


def test_gitlab_check_expired_token(cfg, secrets):
    fake = FakeGitLab()
    fake.token = {"scopes": ["api"], "expires_at": "2026-10-01"}
    results = {c.name: c for c in check_gitlab(cfg.code_host, gl(cfg, secrets, fake), 14, today=date(2026, 10, 23))}
    assert results["gitlab.token_expiry"].status == "fail"


def test_error_bodies_are_scrubbed_of_our_tokens(cfg, secrets):
    fake = FakeYouTrack()
    fake.fail["/api/admin/projects"] = 422  # the fake echoes the Authorization header
    [result] = _guard("youtrack", lambda: check_youtrack(cfg, secrets, fake))
    assert (result.name, result.status) == ("youtrack.request", "fail")
    text = result.detail
    assert "HTTP 422" in text and secrets.tracker not in text
