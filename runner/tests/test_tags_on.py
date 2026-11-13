"""tracker.tags_on: tags the runner sets and takes off at its own steps, next to (or instead of) a state move."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arceus_runner.http import ServiceError

from .test_delivery import World
from .test_research import ask, researched, write_answer
from .test_review import close, later, no_real_tools, published, sends, two_mrs  # noqa: F401  (autouse)

STATES = {"queued": "To Do", "in_progress": "In Progress", "code_review": "Code Review"}
READY = "ready for dev"


def world(tmp_path, secrets, tags_on=None, merged="unset", board=("Testing",)) -> World:
    tracker = {"states": STATES | ({} if merged == "unset" else {"merged": merged})}
    if tags_on is not None:
        tracker["tags_on"] = tags_on
    w = World(tmp_path, secrets, settings={"tracker": tracker})
    w.yt.states += list(board)
    w.yt.tags |= {READY, "in development"}
    return w


@pytest.fixture
def w(tmp_path, secrets):
    world_ = world(tmp_path, secrets, {"merged": {"add": [READY], "remove": ["in development"]}}, merged=None)
    yield world_
    world_.events.close()
    world_.store.close()


def tag_writes(w: World) -> list:
    return [r for r in w.yt.requests if r.method in ("POST", "DELETE") and "/tags" in r.url.path]


def merge_all(w: World) -> None:
    published(w)
    w.yt.issue_tags["2-1"] |= {"in development", "frontend"}
    w.gl.mrs[0]["state"] = "merged"
    later(w)


def test_a_board_without_testing_keeps_the_ticket_in_code_review_and_tags_it(w):
    merge_all(w)
    assert w.store.task("ACME-1").state == "done"
    assert w.yt.issue_fields["2-1"]["State"] == {"name": "Code Review"} and not w.events_named("ticket_moved")
    assert READY in w.yt.issue_tags["2-1"] and "in development" not in w.yt.issue_tags["2-1"]
    assert "frontend" in w.yt.issue_tags["2-1"]                              # a person's own tag stays
    assert {(e["tag"], e["added"]) for e in w.events_named("ticket_tagged")} == {(READY, True),
                                                                                ("in development", False)}


def test_the_tags_come_with_the_move_to_testing_too(tmp_path, secrets):
    w = world(tmp_path, secrets, {"merged": {"add": [READY]}})
    merge_all(w)
    assert w.yt.issue_fields["2-1"]["State"] == {"name": "Testing"} and READY in w.yt.issue_tags["2-1"]


def test_without_tags_on_nothing_is_tagged(tmp_path, secrets):
    w = world(tmp_path, secrets)
    published(w)
    w.yt.issue_tags["2-1"] |= {"in development", "frontend"}
    before, writes = set(w.yt.issue_tags["2-1"]), len(tag_writes(w))
    w.gl.mrs[0]["state"] = "merged"
    later(w)
    assert w.store.task("ACME-1").state == "done" and w.yt.issue_fields["2-1"]["State"] == {"name": "Testing"}
    assert w.yt.issue_tags["2-1"] == before and len(tag_writes(w)) == writes
    assert not w.events_named("ticket_tagged", "ticket_tag_refused")


def test_a_move_the_board_refuses_does_not_keep_the_tags_off(tmp_path, secrets):
    w = world(tmp_path, secrets, {"merged": {"add": [READY]}}, board=())   # no Testing on this board
    merge_all(w)
    assert w.events_named("ticket_move_failed") and READY in w.yt.issue_tags["2-1"]
    assert w.store.task("ACME-1").state == "done"


def test_a_tag_write_that_fails_is_finished_later_and_never_twice(w, monkeypatch):
    published(w)
    w.yt.issue_tags["2-1"].add("in development")
    w.gl.mrs[0]["state"] = "merged"
    yt = w.runner.delivery.tracker
    real = yt.add_tag

    def refuse(db_id, name):
        raise ServiceError("youtrack", "tag write refused for now")

    monkeypatch.setattr(yt, "add_tag", refuse)
    later(w)
    assert w.store.task("ACME-1").state == "done"                             # the task does not wait on a tag
    assert READY not in w.yt.issue_tags["2-1"] and "in development" in w.yt.issue_tags["2-1"]  # kept in order
    assert w.events_named("notify_pending")
    monkeypatch.setattr(yt, "add_tag", real)
    later(w)
    assert READY in w.yt.issue_tags["2-1"] and "in development" not in w.yt.issue_tags["2-1"]
    writes = len(tag_writes(w))
    w.yt.issue_tags["2-1"].discard(READY)                                    # a person takes it off afterwards
    later(w)
    assert READY not in w.yt.issue_tags["2-1"] and len(tag_writes(w)) == writes  # and it stays off
    assert [e["tag"] for e in w.events_named("ticket_tagged")] == [READY, "in development"]


def test_a_tag_youtrack_does_not_have_is_skipped_and_the_rest_goes_on(tmp_path, secrets):
    w = world(tmp_path, secrets, {"merged": {"add": ["no such tag", READY]}}, merged=None)
    merge_all(w)
    assert w.store.task("ACME-1").state == "done" and READY in w.yt.issue_tags["2-1"]
    [refused] = w.events_named("ticket_tag_refused")
    assert refused["tag"] == "no such tag" and "not found" in refused["reason"]
    later(w)
    assert len(w.events_named("ticket_tag_refused")) == 1                    # not tried on every tick


def test_merged_next_to_closed_ends_the_task_without_the_tags(w):
    two_mrs(w)
    close(w, 0)
    w.gl.mrs[1]["state"] = "merged"
    w.tick()
    assert w.store.task("ACME-1").state == "done" and READY not in w.yt.issue_tags.get("2-1", set())
    later(w)
    assert READY not in w.yt.issue_tags.get("2-1", set()) and not w.events_named("ticket_tagged")


def test_every_mr_closed_is_not_merged_and_not_tagged(w):
    two_mrs(w)
    close(w, 0)
    close(w, 1)
    w.tick()
    assert w.store.task("ACME-1").state == "stopped" and READY not in w.yt.issue_tags["2-1"]


def test_an_open_mr_holds_the_tags_back(w):
    two_mrs(w)
    w.gl.mrs[0]["state"] = "merged"
    later(w)
    assert w.store.task("ACME-1").state == "code_review" and READY not in w.yt.issue_tags.get("2-1", set())
    w.gl.mrs[1]["state"] = "merged"
    later(w)
    assert READY in w.yt.issue_tags["2-1"] and sends(w) == []


def test_a_ticket_a_person_moved_on_before_the_merge_gets_no_tags(w):
    published(w)
    w.yt.issue_fields["2-1"]["State"] = {"name": "Done"}
    later(w)
    assert w.store.task("ACME-1").state == "stopped"
    w.gl.mrs[0]["state"] = "merged"
    later(w)
    assert READY not in w.yt.issue_tags.get("2-1", set())


@pytest.mark.parametrize("merged", [None, "unset"])
def test_a_ticket_moved_on_and_merged_between_two_polls_gets_no_tags(tmp_path, secrets, merged):
    w = world(tmp_path, secrets, {"merged": {"add": [READY]}}, merged=merged)
    published(w)
    w.yt.issue_fields["2-1"]["State"] = {"name": "Done"}                     # both before the next poll
    w.gl.mrs[0]["state"] = "merged"
    later(w)
    assert w.store.task("ACME-1").state == "done" and w.yt.issue_fields["2-1"]["State"] == {"name": "Done"}
    assert READY not in w.yt.issue_tags.get("2-1", set()) and not w.events_named("ticket_tagged", "ticket_moved")
    later(w)
    assert READY not in w.yt.issue_tags.get("2-1", set())


def test_a_crash_after_the_runners_own_move_still_tags_on_the_next_poll(tmp_path, secrets, monkeypatch):
    w = world(tmp_path, secrets, {"merged": {"add": [READY]}})               # moves to Testing
    published(w)
    w.gl.mrs[0]["state"] = "merged"
    yt = w.runner.delivery.tracker
    real = yt.set_state

    def moved_then_lost(db_id, state):
        real(db_id, state)
        raise ServiceError("youtrack", "the answer to the move never came")

    monkeypatch.setattr(yt, "set_state", moved_then_lost)
    later(w)
    assert w.yt.issue_fields["2-1"]["State"] == {"name": "Testing"} and w.store.task("ACME-1").state == "code_review"
    monkeypatch.setattr(yt, "set_state", real)
    later(w)                                     # the ticket is in Testing now: the runner's own move, not a person's
    assert w.store.task("ACME-1").state == "done" and READY in w.yt.issue_tags["2-1"]
    assert [e["tag"] for e in w.events_named("ticket_tagged")] == [READY]


def test_a_research_answer_gets_its_own_tags_and_not_the_merge_ones(tmp_path, secrets):
    w = world(tmp_path, secrets, {"merged": {"add": [READY]}, "answered": {"add": ["in development"]}})
    w.yt.tags.add("agent-research")
    db_id = ask(w)
    write_answer(w, "First answer.")
    researched(w)
    w.tick()
    assert w.store.task("ACME-1").state == "done"
    assert "in development" in w.yt.issue_tags[db_id] and READY not in w.yt.issue_tags[db_id]
    w.yt.issue_tags[db_id].discard("in development")
    w.yt.comment(db_id, "j.doe", "/continue and what about the admin login?")
    w.tick()
    write_answer(w, "Second answer.", run=2)
    researched(w)
    w.tick()
    assert "in development" in w.yt.issue_tags[db_id]                       # each answer is a step of its own


@pytest.mark.parametrize("tags_on, says", [
    ({"merged": {"add": ["agent"]}}, "runner's own tags"),
    ({"answered": {"remove": ["agent-failed"]}}, "runner's own tags"),
    ({"merged": {"add": ["x"], "remove": ["x"]}}, "both added and removed"),
    ({"merged": {"add": ["x", "x"]}}, "duplicates"),
    ({"merged": {"add": [""]}}, "must not be empty"),
    ({"merged": {"add": [" x"]}}, "must not be empty or start or end with a space"),
    ({"testing": {"add": ["x"]}}, "Extra inputs are not permitted"),
    ({"merged": {"tags": ["x"]}}, "Extra inputs are not permitted"),
])
def test_a_mistake_in_tags_on_stops_the_config(tmp_path, secrets, tags_on, says):
    with pytest.raises(ValidationError, match=says):
        world(tmp_path, secrets, tags_on)
