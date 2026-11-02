"""A subtask without a description of its own: what it asks is in its parent ticket."""

from __future__ import annotations

import pytest

from .test_delivery import World
from .test_materials import dispatched
from .test_security import queue


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])


@pytest.fixture
def w(tmp_path, secrets):
    world = World(tmp_path, secrets)
    yield world
    world.events.close()
    world.store.close()


def test_a_subtask_without_a_description_gets_its_parents(w):
    w.yt.add_issue(7, ("p", "j.doe", "-agent"))
    w.yt.issues[-1].update(description="The feature: https://docs.google.com/spreadsheets/d/x/edit#gid=1&range=24:24",
                           summary="Feature 3.2")
    queue(w, description="")
    w.yt.issues[-1]["parent"] = {"issues": [{"idReadable": "ACME-7"}]}
    w.tick()
    text = dispatched(w)
    assert "## The parent ticket ACME-7: Feature 3.2" in text and "range=24:24" in text
    asked = [r for r in w.yt.requests if r.url.path == "/api/issues/ACME-1"]
    assert "parent(issues(idReadable))" in asked[-1].url.params["fields"]


def test_a_ticket_with_its_own_description_does_not_get_its_parents(w):
    w.yt.add_issue(7, ("p", "j.doe", "-agent"))
    w.yt.issues[-1]["description"] = "The epic: everything about piloting"
    queue(w, description="Do the thing")
    w.yt.issues[-1]["parent"] = {"issues": [{"idReadable": "ACME-7"}]}
    w.tick()
    assert "parent ticket" not in dispatched(w)
