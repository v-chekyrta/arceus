"""The ticket's tags reach the agent and the reviewers, and a platform the tags name is not left undone.

A ticket tagged `android` and `ios` whose text names no platform: with only the summary and the description
passed on, the agent can extend a web modal, every review passes it, and backend and frontend MRs are opened
for a mobile ticket.
"""
from __future__ import annotations

import pytest

from arceus_runner import platforms
from arceus_runner.config import Config, TrackerTags

from .conftest import example_raw
from .test_delivery import World, git, no_real_tools  # noqa: F401  (fixture)

MAPPED = {"android": ["acme-theme"], "ios": ["acme-platform"]}


@pytest.fixture
def mapped(tmp_path, secrets):
    world = World(tmp_path, secrets, settings={"tracker": {"platforms": MAPPED}})
    yield world
    world.events.close()
    world.store.close()


@pytest.fixture
def plain(tmp_path, secrets):
    world = World(tmp_path, secrets)
    yield world
    world.events.close()
    world.store.close()


def tagged(w: World, *tags: str) -> str:
    db_id = w.yt.add_issue(1, ("a1", "j.doe", "+agent"))
    w.yt.issues[-1]["summary"] = "Label a shape"
    w.yt.issue_tags[db_id] |= set(tags)
    w.tick()
    return db_id


def brief(w: World) -> str:
    [d] = w.calls("dispatch")
    return d["argv"][15]


def commit(w: World, repo: str, name: str = "work.txt") -> str:
    path = w.cfg.paths.workspace / repo
    (path / name).write_text("work\n")
    git(path, "add", name)
    git(path, "commit", "-q", "-m", f"add {name}")
    return git(path, "rev-parse", "HEAD")


def sends(w: World, prefix: str) -> list[str]:
    out = []
    for c in w.calls("worker-send"):
        mid = c["argv"][c["argv"].index("--message-id") + 1]
        if mid.startswith(prefix):
            out.append(c["argv"][-1])
    return out


# -- the pieces ------------------------------------------------------------------------------------

def test_runner_tags_are_not_the_tickets():
    assert platforms.ticket_tags({"agent", "Agent-Research", "android", " ios ", "agent-failed", ""},
                                 TrackerTags()) == ["android", "ios"]


def test_missing_platforms_need_a_change_each():
    assert platforms.missing(["Android", "ios", "bug"], MAPPED, ["acme-platform"]) == {"android": ["acme-theme"]}
    assert platforms.missing(["android", "ios"], MAPPED, ["acme-platform", "acme-theme"]) == {}
    assert platforms.missing(["web"], MAPPED, []) == {}


@pytest.mark.parametrize("bad", [{"agent": ["x"]}, {"ios": []}, {"ios": ["/abs/path"]}, {"iOS": ["a"], "ios": ["b"]},
                                 {"": ["a"]}])
def test_config_refuses_a_bad_platform_map(bad):
    raw = example_raw()
    raw["tracker"]["platforms"] = bad
    with pytest.raises(ValueError):
        Config.model_validate(raw)


# -- the agent's brief -----------------------------------------------------------------------------

def test_brief_names_the_tagged_platforms_and_their_repositories(mapped):
    tagged(mapped, "android", "ios", "sprint-12")
    text = brief(mapped)
    assert "## Tags on the ticket" in text and "`android`, `ios`, `sprint-12`" in text
    assert "- `android`: `acme-theme`" in text and "- `ios`: `acme-platform`" in text
    assert "Each tagged platform needs its own change" in text
    assert "`agent`" not in text  # the runner's own tag says nothing of the work


def test_a_mapped_repository_outside_the_workspace_is_said_so(tmp_path, secrets):
    w = World(tmp_path, secrets, settings={"tracker": {"platforms": {"android": ["acme-infra"]}}})
    try:
        tagged(w, "android")
        assert "`acme-infra` (not in your workspace)" in brief(w)
    finally:
        w.events.close()
        w.store.close()


def test_tags_reach_the_agent_without_a_platform_map(plain):
    tagged(plain, "android", "ios")
    text = brief(plain)
    assert "`android`, `ios`" in text and "when a tag names a platform" in text


def test_no_tags_no_section(plain):
    tagged(plain)
    assert "Tags on the ticket" not in brief(plain)


# -- publishing ------------------------------------------------------------------------------------

def test_opp_346_work_elsewhere_goes_back_to_the_agent_then_to_people(mapped):
    """Tagged android and ios, the agent changed only the ios repository: android is left undone."""
    tagged(mapped, "android", "ios")
    commit(mapped, "acme-platform")
    mapped.settle("ACME-1", "waiting_review")
    mapped.tick()

    # Not published: back to the agent, once, with what is missing.
    assert mapped.gl.mrs == [] and mapped.remote_branch("agent/ACME-1-label-a-shape") is None
    [back] = sends(mapped, "platforms-")
    assert "`android` (`acme-theme`)" in back and "You changed only `acme-platform`" in back
    assert mapped.store.task("ACME-1").state == "dispatched"

    # The agent finishes again without the android work: the ticket comes back to people.
    commit(mapped, "acme-platform", "more.txt")
    mapped.settle("ACME-1", "waiting_review")
    mapped.tick()
    task = mapped.store.task("ACME-1")
    assert task.state == "failed" and "tags name a platform" in task.reason
    assert mapped.gl.mrs == [] and len(sends(mapped, "platforms-")) == 1
    [failure] = [c for c in mapped.bot_comments() if "could not take this ticket" in c]
    assert "`android` (`acme-theme`)" in failure and "take its tag off" in failure


def test_every_tagged_platform_changed_publishes(mapped):
    tagged(mapped, "android", "ios")
    commit(mapped, "acme-platform")
    commit(mapped, "acme-theme")
    mapped.settle("ACME-1", "waiting_review")
    mapped.tick()
    assert sends(mapped, "platforms-") == []
    assert len(mapped.gl.mrs) == 2 and mapped.store.task("ACME-1").state == "code_review"


def test_the_platform_work_done_after_going_back_publishes(mapped):
    tagged(mapped, "android", "ios")
    commit(mapped, "acme-platform")
    mapped.settle("ACME-1", "waiting_review")
    mapped.tick()
    assert len(sends(mapped, "platforms-")) == 1
    commit(mapped, "acme-theme")
    mapped.settle("ACME-1", "waiting_review")
    mapped.tick()
    assert len(mapped.gl.mrs) == 2 and mapped.store.task("ACME-1").state == "code_review"


def test_untagged_ticket_publishes_as_before(mapped):
    tagged(mapped)
    commit(mapped, "acme-platform")
    mapped.settle("ACME-1", "waiting_review")
    mapped.tick()
    assert len(mapped.gl.mrs) == 1 and sends(mapped, "platforms-") == []


# -- follow-ups ------------------------------------------------------------------------------------

def test_continue_tells_the_agent_the_tags_changed(mapped):
    tagged(mapped, "ios")
    mapped.settle("ACME-1", "failed", review="needs-user", state_reason="needs-user")
    mapped.tick()
    task = mapped.store.task("ACME-1")
    assert task.state == "failed"
    mapped.yt.clock = task.failed_at_ms + 1000
    mapped.yt.issue_tags["2-1"].add("android")
    mapped.yt.comment("2-1", "j.doe", "/continue")
    mapped.tick()
    mapped.tick()
    [text] = sends(mapped, "yt-continue-")
    assert "The ticket's tags changed since you started" in text and "- `android`: `acme-theme`" in text

    # Told once: a later follow-up with the same tags does not repeat it.
    assert mapped.store.action("ACME-1:tags")[1] == '["android", "ios"]'


def test_continue_with_the_same_tags_says_nothing_of_them(mapped):
    tagged(mapped, "ios")
    mapped.settle("ACME-1", "failed", review="needs-user", state_reason="needs-user")
    mapped.tick()
    mapped.yt.clock = mapped.store.task("ACME-1").failed_at_ms + 1000
    mapped.yt.comment("2-1", "j.doe", "/continue")
    mapped.tick()
    mapped.tick()
    [text] = sends(mapped, "yt-continue-")
    assert "tags" not in text


# -- the code reviewer -----------------------------------------------------------------------------

def test_the_code_reviewer_reads_the_tags_as_requirements(tmp_path):
    from arceus_runner.config import Secrets

    from .conftest import GL_TOKEN, YT_TOKEN
    from .test_code_review import ask, publish, reviews

    w = World(tmp_path, Secrets(tracker=YT_TOKEN, code_host=GL_TOKEN, slack=None),
              settings={"review_agent": {"enabled": True}, "tracker": {"platforms": MAPPED}})
    try:
        publish(w)  # no tags yet: published as usual
        w.yt.issue_tags["2-1"] |= {"android", "ios"}
        ask(w)
        [call] = reviews(w)
        text = call["argv"][-1]
        assert "## Tags on the ticket" in text and "- `android`: `acme-theme`" in text
        assert "Report a tagged platform without its change as a blocker" in text
        assert "Do the work there" not in text  # the reviewer checks, it does not do the work
    finally:
        w.events.close()
        w.store.close()


def test_the_doctor_fails_a_platform_whose_repository_the_agent_does_not_get(tmp_path, secrets):
    from arceus_runner.checks import check_platforms

    from .conftest import providers

    # acme-infra is excluded in the test world: a platform mapped to it could never be done.
    w = World(tmp_path, secrets, settings={"tracker": {"platforms": {"android": ["acme-theme"],
                                                                     "ios": ["acme-infra"]}}})
    try:
        _, host = providers(w.cfg, w.secrets, w.yt, w.gl, w.clock)
        got = {c.name: c.status for c in check_platforms(w.cfg, host)}
        assert got == {"tracker.platforms.android": "ok", "tracker.platforms.ios": "fail"}
    finally:
        w.events.close()
        w.store.close()
