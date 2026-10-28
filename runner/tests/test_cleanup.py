"""Cleanup (cleanup.py): who owns a leftover, when a pass runs, and that nothing a task still needs goes."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arceus_runner import loop
from arceus_runner.checks import gitlab_client, youtrack_client
from arceus_runner.cleanup import (AdviceUnavailable, ClaudeAdvisor, Cleanup, DockerError, Item, Proc, Situation,
                                   kill_process, remove_tree, scan_processes)
from arceus_runner.config import Config
from arceus_runner.db import ReviewStore
from arceus_runner.engine import EngineError
from arceus_runner.events import EventLog
from arceus_runner.gitops import Git
from arceus_runner.metrics import Metrics

from .conftest import FakeClock, example_raw, read_events
from .fakes import FakeGitLab, FakeYouTrack

T0 = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class FakeDocker:
    def __init__(self):
        self.cs: dict[str, dict] = {}
        self.vols: dict[str, dict] = {}
        self.nets: dict[str, dict] = {}
        self.dangling = 0
        self.imgs: dict[str, dict] = {}
        self.deadline = None
        self.calls: list[tuple] = []
        self.fail: set[str] = set()  # ids whose removal fails

    def available(self) -> bool:
        return True

    def add(self, cid: str, *, status: str = "exited", created: datetime, finished: datetime | None = None,
            labels: dict | None = None, binds: tuple = (), volumes: tuple = (), networks: tuple = (),
            image: str = "sha256:postgres") -> None:
        self.cs[cid] = {
            "Id": cid, "Name": f"/{cid}", "Created": iso(created), "Image": image,
            "State": {"Status": status, "FinishedAt": iso(finished) if finished else "0001-01-01T00:00:00Z"},
            "Config": {"Labels": labels or {}, "Image": "postgres:16"},
            "Mounts": [{"Type": "bind", "Source": b} for b in binds] + [{"Type": "volume", "Name": v} for v in volumes],
            "NetworkSettings": {"Networks": {n: {} for n in networks}},
        }

    def add_volume(self, name: str, labels: dict | None = None, created: datetime = T0 - timedelta(days=1)) -> None:
        self.vols[name] = {"Name": name, "Labels": labels or {}, "CreatedAt": iso(created)}

    def network(self, nid: str) -> dict | None:
        return copy.deepcopy(self.nets.get(nid))

    def volume(self, name: str) -> dict | None:
        return copy.deepcopy(self.vols.get(name))

    def add_network(self, nid: str, name: str, created: datetime, labels: dict | None = None,
                    running: tuple = ()) -> None:
        self.nets[nid] = {"Id": nid, "Name": name, "Created": iso(created), "Labels": labels or {},
                          "Containers": {cid: {} for cid in running}}

    def containers(self) -> list[dict]:
        return copy.deepcopy(list(self.cs.values()))

    def container(self, cid: str) -> dict | None:
        return copy.deepcopy(self.cs.get(cid))

    def volumes(self) -> list[dict]:
        return copy.deepcopy(list(self.vols.values()))

    def networks(self) -> list[dict]:
        return copy.deepcopy(list(self.nets.values()))

    def dangling_images(self) -> int:
        return self.dangling

    def add_image(self, iid: str, tags: tuple = (), labels: dict | None = None, size: int = 1 << 30) -> None:
        self.imgs[iid] = {"Id": iid, "RepoTags": list(tags), "Config": {"Labels": labels or {}}, "Size": size,
                          "Created": iso(T0 - timedelta(days=2))}

    def images(self) -> list[dict]:
        return copy.deepcopy(list(self.imgs.values()))

    df_rows: dict = {}  # what `docker system df` would say, by type
    sizes: dict = {}  # what `docker system df -v` would say: {"volumes": {name: bytes}, "images": {id: (own, total)}}

    def usage(self) -> dict:
        return copy.deepcopy(self.sizes)

    def disk_usage(self) -> dict:
        return copy.deepcopy(self.df_rows)

    def remove_image(self, iid: str) -> None:
        self.calls.append(("image rm", iid))
        if any(c.get("Image") == iid for c in self.cs.values()):
            raise DockerError("docker image rm: conflict: unable to delete (must be forced) - image is being used")
        del self.imgs[iid]

    def stop(self, cid: str) -> None:
        self.calls.append(("stop", cid))
        self.cs[cid]["State"]["Status"] = "exited"

    def remove_container(self, cid: str, volumes: bool = True) -> None:
        self.calls.append(("rm", cid) if volumes else ("rm (volumes stay)", cid))
        for n in self.nets.values():
            n["Containers"].pop(cid, None)
        if cid in self.fail:
            raise DockerError("docker rm: device or resource busy")
        if self.cs[cid]["State"]["Status"] == "running":
            raise DockerError("docker rm: cannot remove a running container")
        mounts = self.cs.pop(cid)["Mounts"]
        if volumes:  # `rm -v`: its anonymous volumes that no other container uses go with it, as Docker does
            for m in mounts:
                v = self.vols.get(m.get("Name"))
                if v and "com.docker.volume.anonymous" in v["Labels"] and not any(
                        om.get("Name") == v["Name"] for c in self.cs.values() for om in c["Mounts"]):
                    del self.vols[v["Name"]]

    def remove_network(self, nid: str) -> None:
        self.calls.append(("network rm", nid))
        if nid in self.fail:
            raise DockerError("docker network rm: error while removing network")
        del self.nets[nid]

    def remove_volume(self, name: str) -> None:
        self.calls.append(("volume rm", name))
        if any(m.get("Name") == name for c in self.cs.values() for m in c["Mounts"]):
            raise DockerError("docker volume rm: volume is in use")
        del self.vols[name]



class FakeEngine:
    def __init__(self):
        self.shows: dict[str, dict | None] = {}
        self.broken = False

    def show(self, task_id: str) -> dict | None:
        if self.broken:
            raise EngineError("task show: exit 2")
        return self.shows.get(task_id)


def config(tmp: Path, mode: str = "live", cleanup_mode: str = "act", **cleanup) -> Config:
    raw = example_raw()
    raw.pop("slack")
    raw["mode"] = mode
    raw["health"].pop("devstack_check", None)
    raw["cleanup"] = {"mode": cleanup_mode, **cleanup}
    raw["metrics"] = {"port": 0}
    raw["paths"] = {"workspace": str(tmp / "workspace"), "bulava": str(tmp / "bulava"),
                    "bulava_state": str(tmp / "state"), "secrets": str(tmp / "secrets.env"),
                    "db": str(tmp / "runner.db"), "events": str(tmp / "events.jsonl")}
    return Config.model_validate(raw)


class World:
    def __init__(self, tmp: Path, **kw):
        self.tmp = tmp
        self.cfg = config(tmp, **kw)
        self.ws = self.cfg.paths.workspace
        self.ws.mkdir(parents=True, exist_ok=True)
        self.now = T0
        self.mono = FakeClock()
        self.docker = FakeDocker()
        self.engine = FakeEngine()
        self.procs: list[Proc] = []
        self.killed: list[int] = []
        self.advice = None
        self.store = ReviewStore(self.cfg.paths.db)
        self.events = EventLog(self.cfg.paths.events, self.cfg.project)
        self.cleanup = self.make()

    def make(self) -> Cleanup:
        def kill(p: Proc) -> str:
            self.killed.append(p.pid)
            return "done"
        return Cleanup(self.cfg, self.store, self.events, docker=self.docker,
                       git=Git(None, "ACME Agent", "bot@example.com"), engine=self.engine,
                       advise=(lambda ctx: self.advice(ctx)) if self.advice else None,
                       now=lambda: self.now, mono=self.mono, procs=lambda roots: self.procs, kill=kill)

    def task(self, key: str, state: str, *, branch: str | None = None, updated: datetime | None = None,
             failed_at: datetime | None = None, repo: str = "") -> None:
        self.store.create_task(task_id=key, issue_db_id=f"2-{key}", state=state, tag_author="j.doe", repo="",
                               repo_path=str(self.ws), branch=branch if branch is not None else f"agent/{key}-work",
                               base_branch="main", extra_repos="[]")
        fields = {"failed_at_ms": int(failed_at.timestamp() * 1000)} if failed_at else {}
        if fields:
            self.store.update_task(key, **fields)
        self.store.conn.execute("UPDATE tasks SET updated_at = ? WHERE task_id = ?", (iso(updated or self.now), key))

    def hold(self, key: str | None, at: datetime) -> None:
        """`key` holds the workspace from `at`, and the runner's ticks have seen it hold it until now."""
        self.store.hold_workspace(key, iso(at))
        if key:
            self.store.hold_workspace(key, iso(self.now), iso(self.now))

    def events_named(self, *names) -> list[dict]:
        return [e for e in read_events(self.cfg.paths.events) if e["event"] in names]

    def items(self, result: dict) -> dict[str, tuple[str, str]]:
        """name → (action, result) of everything the pass planned."""
        return {i["name"]: (i["action"], i["result"]) for i in result["planned"]}

    def unknown(self, result: dict) -> set[str]:
        return {i["name"] for i in result["unknown"]}


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.events.close()
    w.store.close()


def compose(ws: Path, project: str, repo: str = "web") -> dict:
    return {"com.docker.compose.project": project, "com.docker.compose.project.working_dir": str(ws / repo)}


# -- who owns what ------------------------------------------------------------------------------


def test_only_containers_of_finished_tasks_go(world):
    old = T0 - timedelta(hours=3)
    world.task("ACME-1", "done", updated=T0 - timedelta(hours=1))
    world.task("ACME-2", "code_review")
    world.task("ACME-3", "failed", failed_at=T0 - timedelta(days=1))   # `/continue` still works
    world.task("ACME-4", "failed", failed_at=T0 - timedelta(days=30))  # past the continue window
    d = world.docker
    d.add("done-exited", created=old, finished=old, labels={"arceus.task": "ACME-1"})
    d.add("done-running", status="running", created=old, labels={"arceus.task": "ACME-1"})
    d.add("done-just-exited", created=old, finished=T0 - timedelta(minutes=5), labels={"arceus.task": "ACME-1"})
    d.add("review-exited", created=old, finished=old, labels={"arceus.task": "ACME-2"})
    d.add("resumable-exited", created=old, finished=old, labels={"arceus.task": "ACME-3"})
    d.add("old-failure-exited", created=old, finished=old, labels={"arceus.task": "ACME-4"})
    d.add("never-ours", created=old, finished=old, labels={"arceus.task": "ACME-99"})
    d.add("grafana", status="running", created=old, labels={"com.docker.compose.project": "arceus-observability"})
    d.add("kept", created=old, finished=old, labels={"arceus.task": "ACME-1", "arceus.keep": "true"})
    d.add("other-project", created=old, finished=old, binds=("/srv/other",))

    result = world.cleanup.run("full", "manual")

    assert result["status"] == "succeeded"
    assert world.items(result) == {"done-exited": ("remove", "done"), "done-running": ("stop", "done"),
                                   "old-failure-exited": ("remove", "done")}
    # Everything nobody owns is listed for the person who cleans by hand; a waiting task's things are not.
    assert world.unknown(result) == {"never-ours", "other-project"}
    assert set(d.cs) == {"done-just-exited", "review-exited", "resumable-exited", "never-ours", "grafana", "kept",
                         "other-project"}


def test_a_container_is_put_down_to_the_task_that_held_the_workspace_when_it_was_made(world):
    ws = world.ws
    world.task("ACME-1", "done")
    world.task("ACME-2", "needs_input")
    world.hold("ACME-1", T0 - timedelta(hours=10))
    world.hold("ACME-2", T0 - timedelta(hours=6))
    world.hold(None, T0 - timedelta(hours=4))
    d = world.docker
    d.add("verify-run", created=T0 - timedelta(hours=9), finished=T0 - timedelta(hours=8),
          labels={**compose(ws, "web"), "com.docker.compose.oneoff": "True"}, networks=("web_default",))
    # A compose service made then and stopped since: a dev stack the agent recreated looks exactly like this.
    d.add("web-db", created=T0 - timedelta(hours=9), finished=T0 - timedelta(hours=8),
          labels={**compose(ws, "web"), "com.docker.compose.oneoff": "False"}, networks=("web_default",))
    d.add("bind-run", created=T0 - timedelta(hours=9), finished=T0 - timedelta(hours=8), binds=(str(ws / "web"),))
    d.add("dev-stack", status="running", created=T0 - timedelta(hours=9), labels=compose(ws, "web"))
    d.add("before-any-task", created=T0 - timedelta(days=3), finished=T0 - timedelta(days=2), labels=compose(ws, "api"),
          networks=("api_default",))
    d.add("waiting-task-db", created=T0 - timedelta(hours=5), finished=T0 - timedelta(hours=5), labels=compose(ws, "api"))
    d.add_network("n1", "web_default", T0 - timedelta(hours=9), {"com.docker.compose.project": "web"})
    d.add_network("n2", "api_default", T0 - timedelta(days=3), {"com.docker.compose.project": "api"})
    d.add_network("n3", "made-by-hand", T0 - timedelta(hours=9))
    d.add_network("n4", "web_extra", T0 - timedelta(days=3), {"com.docker.compose.project": "web"})  # no owner
    # Another team's compose project, made while ACME-1 held the workspace: the time alone proves nothing.
    d.add_network("n5", "otherteam_default", T0 - timedelta(hours=9), {"com.docker.compose.project": "otherteam"})
    d.add("unlabelled-run", status="running", created=T0 - timedelta(hours=9))  # `docker run` with no label
    d.add("someone-else", created=T0 - timedelta(days=3), finished=T0 - timedelta(days=3))

    result = world.cleanup.run("full", "manual")

    assert world.items(result) == {"verify-run": ("remove", "done"), "bind-run": ("remove", "done")}
    # A running container or a compose service put down to a task by time alone may be the dev stack.
    assert world.unknown(result) == {"dev-stack", "web-db", "before-any-task", "unlabelled-run", "someone-else",
                                     "web_extra", "made-by-hand", "otherteam_default"}
    assert {"dev-stack", "web-db", "before-any-task", "waiting-task-db", "unlabelled-run", "someone-else"} <= set(d.cs)
    assert "web-db" in world.docker.cs and not any(c == ("rm", "web-db") for c in d.calls)
    # web_default and api_default still have a stopped container on them; n3 is nobody's.
    assert set(d.nets) == {"n1", "n2", "n3", "n4", "n5"}


def test_named_volumes_are_caches_and_go_only_with_a_finished_tasks_label(world):
    world.task("ACME-1", "done")
    world.task("ACME-2", "code_review")
    d = world.docker
    d.add_volume("web_node_modules", {"com.docker.compose.project": "web"})
    d.add_volume("acme1-testdb", {"arceus.task": "ACME-1"})
    d.add_volume("acme1-in-use", {"arceus.task": "ACME-1"})
    d.add_volume("acme2-testdb", {"arceus.task": "ACME-2"})
    d.add_volume("a" * 64)
    d.add_volume("b" * 64, {"com.docker.volume.anonymous": ""})  # what Docker 23+ puts on the ones it names
    d.add_volume("c" * 64, {"com.docker.volume.anonymous": ""})
    d.add_volume("pip-cache")
    d.add("someone", status="running", created=T0, volumes=("acme1-in-use", "c" * 64, "pip-cache"),
          binds=("/srv/x",))

    result = world.cleanup.run("full", "manual")

    assert world.items(result) == {"acme1-testdb": ("remove", "done")}
    # Unused ones nobody labelled are listed (a cache or test data: a person decides); used ones are not.
    assert world.unknown(result) == {"2 anonymous volumes", "web_node_modules", "someone"}
    assert set(d.vols) == {"web_node_modules", "acme1-in-use", "acme2-testdb", "a" * 64, "b" * 64, "c" * 64,
                           "pip-cache"}


def test_protected_compose_project_is_never_touched(tmp_path):
    w = World(tmp_path, protect={"compose_projects": ["omnis-dev"]})
    w.task("ACME-1", "done")
    w.hold("ACME-1", T0 - timedelta(hours=10))
    w.docker.add("lms", created=T0 - timedelta(hours=9), finished=T0 - timedelta(hours=9),
                 labels={**compose(w.ws, "omnis-dev"), "arceus.task": "ACME-1"})
    w.docker.add_network("n", "omnis-dev_default", T0 - timedelta(hours=9), {"com.docker.compose.project": "omnis-dev"})
    result = w.cleanup.run("full", "manual")
    assert result["planned"] == [] and result["unknown"] == []
    assert w.docker.calls == []


def test_report_mode_changes_nothing(tmp_path):
    w = World(tmp_path, mode="observe")  # the runner's observe mode is always report, whatever cleanup.mode says
    w.task("ACME-1", "done")
    w.docker.add("done-exited", created=T0 - timedelta(hours=3), finished=T0 - timedelta(hours=3),
                 labels={"arceus.task": "ACME-1"})
    w.docker.dangling = 4
    result = w.cleanup.run("full", "manual")
    assert result["mode"] == "report" and result["status"] == "succeeded"
    assert w.items(result) == {"done-exited": ("remove", "planned")}
    assert w.unknown(result) == {"4 untagged images"}
    assert w.docker.calls == []
    assert [e["result"] for e in w.events_named("cleanup_item")] == ["planned"]


# -- the workspace ------------------------------------------------------------------------------


def make_repo(w: World, name: str) -> Path:
    origin = w.tmp / "origin" / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo = w.ws / name
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@t")
    (repo / ".gitignore").write_text("node_modules/\n")
    (repo / "app.py").write_text("print(1)\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "push", "-q", "origin", "main")
    return repo


def test_leftovers_on_a_finished_tasks_branch_are_stashed_never_deleted(world):
    world.task("ACME-1", "done")
    world.task("ACME-2", "needs_input")
    web, api, docs = make_repo(world, "web"), make_repo(world, "api"), make_repo(world, "docs")
    git(web, "checkout", "-q", "-b", "agent/ACME-1-work")
    (web / "app.py").write_text("print(2)\n")
    (web / "test-output.json").write_text("{}")
    (web / "node_modules").mkdir()
    (web / "node_modules" / "cache.js").write_text("x")
    git(api, "checkout", "-q", "-b", "agent/ACME-2-work")
    (api / "half-done.py").write_text("x")  # the task waits for an answer: its work stays
    (docs / "notes.txt").write_text("a person's?")  # on main: nobody's, so only reported

    result = world.cleanup.run("full", "manual")

    assert world.items(result) == {"web": ("stash", "done")}
    assert world.unknown(result) == {"docs"}
    assert git(web, "status", "--porcelain") == ""
    assert (web / "node_modules" / "cache.js").exists()  # ignored files (caches, builds) are not leftovers
    assert "arceus-cleanup" in git(web, "stash", "list")
    assert git(web, "show", "stash@{0}^3:test-output.json") == "{}"  # the untracked file is in the stash
    assert (api / "half-done.py").exists() and (docs / "notes.txt").exists()


def test_stash_all_names_only_a_stash_it_made(world):
    repo = make_repo(world, "web")
    g = Git(None, "A", "a@a")
    (repo / "left.txt").write_text("x")
    first = g.stash_all(repo, "one")
    assert first and git(repo, "rev-parse", "refs/stash") == first
    assert g.stash_all(repo, "nothing left") is None  # git makes no stash; the newest is the older one
    assert git(repo, "stash", "list").count("\n") == 0


def test_branches_of_finished_tasks_go_when_gitlab_has_their_commits(world):
    world.task("ACME-1", "done")
    world.task("ACME-2", "done")
    world.task("ACME-3", "failed", failed_at=T0 - timedelta(days=40))
    world.task("ACME-4", "code_review")
    web = make_repo(world, "web")
    for key, push in (("ACME-1", True), ("ACME-2", False), ("ACME-3", False), ("ACME-4", True)):
        git(web, "checkout", "-q", "-b", f"agent/{key}-work", "main")
        (web / f"{key}.txt").write_text(key)
        git(web, "add", ".")
        git(web, "commit", "-q", "-m", key)
        if push:
            git(web, "push", "-q", "origin", f"agent/{key}-work")
    git(web, "checkout", "-q", "main")
    # ACME-2 was squash-merged: GitLab has none of its commits, but the branch is exactly what was published.
    world.store.upsert_mr("ACME-2", "web", 1, 7, "https://gitlab/mr/7", git(web, "rev-parse", "agent/ACME-2-work"))
    world.store.update_mr("ACME-2", "web", state="merged")

    result = world.cleanup.run("full", "manual")

    assert world.items(result) == {"web: agent/ACME-1-work": ("delete", "done"), "web: agent/ACME-2-work": ("delete", "done")}
    left = git(web, "for-each-ref", "--format=%(refname:short)", "refs/heads/agent")
    assert left.split() == ["agent/ACME-3-work", "agent/ACME-4-work"]  # unpushed work of a failure, a live MR


def test_a_branch_gitlab_dropped_since_the_last_fetch_stays(world):
    """The plan reads GitLab as last fetched. Before the delete the runner fetches with --prune: a branch deleted
    there meanwhile (its commits nowhere else) keeps the local one; so does one checked out in another worktree."""
    world.task("ACME-1", "done")
    world.task("ACME-2", "done")
    web = make_repo(world, "web")
    for key in ("ACME-1", "ACME-2"):
        git(web, "checkout", "-q", "-b", f"agent/{key}-work", "main")
        (web / f"{key}.txt").write_text(key)
        git(web, "add", ".")
        git(web, "commit", "-q", "-m", key)
        git(web, "push", "-q", "origin", f"agent/{key}-work")
    git(web, "checkout", "-q", "main")
    git(web, "worktree", "add", "-q", str(world.tmp / "wt"), "agent/ACME-2-work")
    items, _ = world.cleanup.plan("full")
    assert {i.name for i in items if i.kind == "branch"} == {"web: agent/ACME-1-work"}  # ACME-2's is in a worktree
    origin = world.tmp / "origin" / "web.git"
    git(origin, "branch", "-D", "agent/ACME-1-work")  # deleted on GitLab after the plan read it

    world.cleanup.execute("r1", items, [], deadline=world.mono() + 600)

    branch = next(i for i in items if i.kind == "branch")
    assert branch.result == "kept" and branch.detail["kept"] == "GitLab no longer has its commits"
    assert git(web, "rev-parse", "--verify", "agent/ACME-1-work")


def test_media_of_finished_tasks_goes_after_its_retention(world):
    world.task("ACME-1", "done", updated=T0 - timedelta(days=20))
    world.task("ACME-2", "done", updated=T0 - timedelta(days=2))
    world.task("ACME-3", "code_review")
    base = world.ws / ".arceus"
    for kind, key in (("media", "ACME-1"), ("media", "ACME-2"), ("media", "ACME-3"), ("notes", "ACME-1"),
                      ("media", "ACME-77")):
        (base / kind / key).mkdir(parents=True)
        (base / kind / key / "shot.png").write_bytes(b"x" * 100)
    elsewhere = world.tmp / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious").write_text("x")
    (base / "media" / "ACME-1-link").symlink_to(elsewhere)
    (base / "scratch").mkdir()

    result = world.cleanup.run("full", "manual")

    assert world.items(result) == {".arceus/media/ACME-1": ("delete", "done")}
    assert world.unknown(result) == {".arceus/media/ACME-77", ".arceus/scratch"}
    assert not (base / "media" / "ACME-1").exists()
    assert (base / "notes" / "ACME-1").exists()  # notes stay 90 days: reviewers ask weeks later
    assert (elsewhere / "precious").exists()


def test_notes_stay_90_days_after_the_task_is_over(world):
    world.task("ACME-1", "done", updated=T0 - timedelta(days=91))
    world.task("ACME-2", "done", updated=T0 - timedelta(days=89))
    world.task("ACME-3", "failed", failed_at=T0 - timedelta(days=80))  # over 14 days after the failure: 66 days ago
    base = world.ws / ".arceus" / "notes"
    for key in ("ACME-1", "ACME-2", "ACME-3"):
        (base / key).mkdir(parents=True)
        (base / key / "decisions.md").write_text("why")

    result = world.cleanup.run("full", "manual")

    assert world.items(result) == {".arceus/notes/ACME-1": ("delete", "done")}
    assert sorted(p.name for p in base.iterdir()) == ["ACME-2", "ACME-3"]


def test_the_report_says_what_removing_by_hand_would_free_and_whose_the_name_says(world, capsys):
    """Sizes from `docker system df -v`: an image frees only the layers no other image shares. A name that spells
    a task is a hint for the person, never a reason to remove anything."""
    from arceus_runner.cli import _print_cleanup
    world.task("EDCMP-7173", "code_review")
    world.task("EDCMP-717", "done")  # its key is a prefix of the other's: no hint from edcmp7173-*
    d = world.docker
    d.add("edcmp7173-mysql", status="running", created=T0 - timedelta(days=2), binds=("/srv/x",))
    d.add_volume("edcmp7173-mysql")
    d.add_volume("e" * 64, {"com.docker.volume.anonymous": ""})
    d.add_volume("f" * 64, {"com.docker.volume.anonymous": ""})
    d.add_network("n1", "edcmp7173", T0 - timedelta(days=2))
    d.add_image("sha256:xmodule", ("edcamp-xmodule-js:latest",), size=5_090_000_000)
    d.add_image("sha256:node", ("node:24",), size=1_650_000_000)
    d.add_image("sha256:mysql", ("mysql:8.0",), size=1_100_000_000)
    d.cs["edcmp7173-mysql"]["Image"] = "sha256:mysql"
    d.sizes = {"volumes": {"edcmp7173-mysql": 281_000_000, "e" * 64: 300_000_000, "f" * 64: 4_000},
               "images": {"sha256:xmodule": (594_000_000, 5_090_000_000), "sha256:node": (1_650_000_000, 1_650_000_000)}}
    d.df_rows = {"Images": {"Type": "Images", "Size": "7.84GB", "Reclaimable": "2.244GB (28%)"}}

    result = world.cleanup.run("full", "manual")

    why = {i["name"]: i["why"] for i in result["unknown"]}
    assert "5.1 GB, 594.0 MB of it its own" in why["edcamp-xmodule-js:latest"]
    assert "; 1.6 GB, made" in why["node:24"]  # nothing shared worth saying
    # The biggest own part first; what removing all of them frees is Docker's count, a shared layer once.
    assert [i["name"] for i in result["unknown"] if i["kind"] == "image"] == \
        ["node:24", "edcamp-xmodule-js:latest", "2 images above"]
    assert why["2 images above"].startswith("Docker counts 2.244GB in images no container uses")
    assert "281.0 MB" in why["edcmp7173-mysql"] and "300.0 MB" in why["2 anonymous volumes"]
    for name in ("edcmp7173-mysql", "edcmp7173"):
        assert why[name].endswith("the name says EDCMP-7173 (waiting)"), why[name]
    assert "EDCMP-717 " not in " ".join(why.values()) and "mysql:8.0" not in why
    assert result["images_in_use"] == 1 and world.items(result) == {}
    _print_cleanup(result["planned"], result["unknown"], result["errors"], result["images_in_use"])
    out = capsys.readouterr().out
    assert "about 2.8 GB in all" in out and "Images that containers use (1) are not listed." in out


def test_an_accepted_continue_keeps_everything_however_long_it_waits_to_be_sent(world):
    """`/continue` came inside the window, but the workspace stayed busy past it: delivery still sends it, so the
    task's containers, volumes and media are not garbage, before or after a restart."""
    world.task("ACME-1", "failed", failed_at=T0 - timedelta(days=30))
    world.store.action_intent("yt-continue-c1", "ACME-1", "continue", '{"author": "j.doe", "comment": "c1"}')
    d = world.docker
    d.add("acme1-db", created=T0 - timedelta(days=31), finished=T0 - timedelta(days=30),
          labels={"arceus.task": "ACME-1"})
    d.add_volume("acme1-testdb", {"arceus.task": "ACME-1"})
    (world.ws / ".arceus" / "media" / "ACME-1").mkdir(parents=True)

    for cleanup in (world.cleanup, world.make()):
        result = cleanup.run("full", "manual")
        assert world.items(result) == {} and "acme1-db" in d.cs and "acme1-testdb" in d.vols
    assert (world.ws / ".arceus" / "media" / "ACME-1").exists()
    assert world.store.pending_continues()  # still there for delivery to send

    world.store.action_done("yt-continue-c1", "stale")  # it went, or another way took the task on
    assert set(world.items(world.cleanup.run("full", "manual"))) == {"acme1-db", "acme1-testdb",
                                                                      ".arceus/media/ACME-1"}


def test_a_folder_too_big_for_the_pass_is_cut_at_its_deadline(world, monkeypatch):
    """Sizing and removing a folder answer to the pass's budget: the walk stops, the removal stops at the next
    entry once the budget is gone, and the pass ends partial with the folder skipped, to go on the next pass."""
    world.task("ACME-1", "done", updated=T0 - timedelta(days=200))
    media = world.ws / ".arceus" / "media" / "ACME-1"
    media.mkdir(parents=True)
    for n in range(3):
        (media / f"shot{n}.png").write_bytes(b"x")
    real_unlink = os.unlink

    def slow_unlink(name, *, dir_fd=None):
        world.mono.advance(700)  # one file takes the whole budget
        real_unlink(name, dir_fd=dir_fd)
    monkeypatch.setattr("arceus_runner.cleanup.os.unlink", slow_unlink)

    result = world.cleanup.run("full", "manual")

    assert result["status"] == "partial" and "out of time" in result["errors"]
    assert world.items(result) == {".arceus/media/ACME-1": ("delete", "skipped")}
    assert media.is_dir() and len(list(media.iterdir())) == 2  # the rest stays under its own name

    world.cleanup._deadline = world.mono() - 1  # a walk that runs past the deadline stops too
    from arceus_runner.cleanup import _size
    with pytest.raises(TimeoutError):
        _size(media, world.cleanup._out_of_time)


def test_what_was_made_while_the_runner_was_down_is_nobodys(world):
    """A hold left open by a crash ends where a tick last saw it; on a change the old hold ends at the tick's start
    and the new one starts at its end: what was made in the gaps is only reported."""
    world.task("ACME-1", "done")
    world.task("ACME-2", "done")
    world.store.hold_workspace("ACME-1", iso(T0 - timedelta(hours=10)))
    world.store.hold_workspace("ACME-1", iso(T0 - timedelta(hours=9)), iso(T0 - timedelta(hours=9)))  # a tick
    # The runner was down from T0-9h; started again, it closes the hold where it was last seen.
    world.cleanup.recover()
    world.store.hold_workspace("ACME-2", iso(T0 - timedelta(hours=6)), iso(T0 - timedelta(hours=5, minutes=58)))
    world.store.hold_workspace(None, iso(T0 - timedelta(hours=4)))
    d = world.docker
    for name, made in (("acme1-db", 9.5), ("in-the-gap", 7), ("mid-tick", 5.99), ("acme2-db", 5)):
        d.add(name, created=T0 - timedelta(hours=made), finished=T0 - timedelta(hours=3), binds=(str(world.ws),))

    result = world.cleanup.run("full", "manual")

    assert world.items(result) == {"acme1-db": ("remove", "done"), "acme2-db": ("remove", "done")}
    assert {"in-the-gap", "mid-tick"} <= world.unknown(result) and {"in-the-gap", "mid-tick"} <= set(d.cs)


def test_a_newer_network_of_a_compose_project_is_not_put_down_to_its_old_task(world):
    """A network goes by when it was made. That ACME-1 labelled every container of the project proves nothing
    about a network made outside every hold, even while ACME-1's container is still there."""
    world.task("ACME-1", "done")
    world.task("ACME-2", "needs_input")
    world.hold("ACME-1", T0 - timedelta(days=4))
    world.hold(None, T0 - timedelta(days=2))
    world.hold("ACME-2", T0 - timedelta(hours=10))
    world.hold(None, T0 - timedelta(hours=8))
    e2e = {"com.docker.compose.project": "e2e"}
    d = world.docker
    d.add("e2e-db", status="running", created=T0 - timedelta(days=3), labels={**e2e, "arceus.task": "ACME-1"},
          binds=("/srv/x",))
    d.add_network("n-old", "e2e_default", T0 - timedelta(days=3), e2e)  # while ACME-1 held the workspace
    d.add_network("n-new", "e2e_new", T0 - timedelta(days=1), e2e)      # outside every hold
    d.add_network("n-b", "e2e_b", T0 - timedelta(hours=9), e2e)         # while ACME-2 held it: ACME-2 waits

    result = world.cleanup.run("full", "manual")

    assert world.items(result) == {"e2e-db": ("stop", "done"), "e2e_default": ("remove", "done")}
    assert set(d.nets) == {"n-new", "n-b"} and "e2e_new" in world.unknown(result)


def test_a_holder_that_finishes_in_a_tick_holds_nothing_made_during_that_tick(world):
    """A task that finished somewhere in this tick: its hold ends at the tick's start, not when cleanup looks, so
    what someone made in the tick before the pass is nobody's."""
    world.task("ACME-1", "done")
    tick = T0 - timedelta(hours=1)
    world.store.hold_workspace("ACME-1", iso(T0 - timedelta(hours=5)))
    world.store.hold_workspace("ACME-1", iso(tick), iso(tick))  # the last tick that saw ACME-1 work
    world.now = tick + timedelta(minutes=1)
    world.docker.add("made-in-the-tick", created=tick + timedelta(seconds=30), finished=tick + timedelta(seconds=40),
                     binds=(str(world.ws),))
    world.docker.add("acme1-db", created=T0 - timedelta(hours=4), finished=T0 - timedelta(hours=3),
                     binds=(str(world.ws),))

    world.cleanup.maybe_run(Situation(None, False, False, since=tick))  # the first free moment: a full pass

    assert world.store.workspace_holds()[-1][2] == iso(tick).replace("Z", ".000Z")
    assert runs(world) == [("daily", "full", "succeeded")]
    assert "acme1-db" not in world.docker.cs and "made-in-the-tick" in world.docker.cs


def test_sizing_a_big_folder_stops_in_the_middle_of_it(tmp_path):
    from arceus_runner.cleanup import _size
    big = tmp_path / "media" / "ACME-1"
    big.mkdir(parents=True)
    for n in range(600):
        (big / f"{n}.png").write_bytes(b"x")
    calls = {"n": 0}

    def out_of_time() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # gone after the first look, in the middle of one folder
    with pytest.raises(TimeoutError):
        _size(big, out_of_time)


def test_looking_through_a_huge_workspace_stops_at_the_deadline(world):
    for n in range(600):
        (world.ws / "group" / f"sub{n}").mkdir(parents=True)
    looks = {"n": 0}

    def mono() -> float:
        looks["n"] += 1
        return 0.0 if looks["n"] < 20 else 1e9  # the budget runs out partway through the listing
    world.cleanup.mono = mono
    world.cleanup._deadline = 10.0
    with pytest.raises(TimeoutError):  # finding the repositories under `group`: 600 folders, cut short
        world.cleanup._workspace_extras()


def test_a_folder_with_a_file_system_mounted_on_it_stays(tmp_path, monkeypatch):
    media = tmp_path / "media"
    (media / "ACME-1").mkdir(parents=True)
    real_stat = os.stat
    monkeypatch.setattr("arceus_runner.cleanup.os.stat", lambda p, *a, **k: os.stat_result(
        (*real_stat(p)[:2], real_stat(p).st_dev + 1, *real_stat(p)[3:])) if str(p) == str(media) else real_stat(p))
    monkeypatch.setattr("arceus_runner.cleanup.mount_points", lambda: [])  # macOS: no mount table to read
    it = Item("folder", str(media / "ACME-1"), "ACME-1", "delete", "")
    assert remove_tree(media / "ACME-1", media, it) == "kept" and it.detail["kept"] == "a file system is mounted on it"
    assert (media / "ACME-1").exists()


def test_what_lies_in_the_workspace_beside_the_repositories_is_listed_never_touched(world):
    make_repo(world, "web")
    make_repo(world, "frontend/app")  # a subgroup's folder holds repositories: not listed
    for name in ("AGENTS.md", "CLAUDE.md", ".gitignore"):
        (world.ws / name).write_text("x")
    (world.ws / ".arceus" / "media").mkdir(parents=True)
    (world.ws / "artifacts" / "2026-10-25-run").mkdir(parents=True)
    (world.ws / "artifacts" / "2026-10-25-run" / "report.html").write_bytes(b"x" * 2048)
    (world.ws / "dump.sql").write_text("x")

    result = world.cleanup.run("full", "manual")

    assert world.unknown(result) == {"artifacts", "dump.sql"}
    assert "2.0 KB" in next(i["why"] for i in result["unknown"] if i["name"] == "artifacts")
    assert (world.ws / "artifacts" / "2026-10-25-run" / "report.html").exists() and (world.ws / "dump.sql").exists()


def test_remove_tree_refuses_a_folder_that_moved_under_a_link(tmp_path):
    real = tmp_path / "ws" / ".arceus" / "media"
    real.mkdir(parents=True)
    outside = tmp_path / "outside" / "ACME-1"
    outside.mkdir(parents=True)
    (outside / "precious").write_text("x")
    (tmp_path / "ws" / ".arceus" / "evil").symlink_to(tmp_path / "outside")
    assert remove_tree(tmp_path / "ws" / ".arceus" / "evil" / "ACME-1", real) == "kept"
    assert (outside / "precious").exists()
    assert remove_tree(real / "ACME-404", real) == "gone"


def test_orphaned_dev_servers_of_finished_tasks_are_stopped(world):
    world.task("ACME-1", "done")
    world.hold("ACME-1", T0 - timedelta(hours=10))
    world.hold(None, T0 - timedelta(hours=4))
    ws = str(world.ws / "web")
    world.procs = [
        Proc(200, 1, T0 - timedelta(hours=9), ws, "node vite --port 5173", session=False),
        Proc(201, 2, T0 - timedelta(hours=9), ws, "claude", session=True),
        Proc(300, 3, T0 - timedelta(days=5), ws, "python manage.py runserver", session=False),
    ]
    result = world.cleanup.run("full", "manual")
    assert world.items(result) == {"node vite --port 5173 (pid 200)": ("kill", "done")}
    assert world.unknown(result) == {"python manage.py runserver (pid 300)"}
    assert world.killed == [200]


def test_scan_finds_orphans_and_spares_live_sessions(tmp_path):
    proc, ws = tmp_path / "proc", tmp_path / "ws"
    (ws / "web").mkdir(parents=True)
    proc.mkdir()
    (proc / "stat").write_text("cpu 1 2 3\nbtime 1700000000\n")

    def add(pid: int, ppid: int, comm: str, cwd: Path, cmd: str) -> None:
        d = proc / str(pid)
        d.mkdir()
        fields = ["S", str(ppid)] + ["0"] * 17 + ["500"]  # field 22 (start ticks) is the 20th after the comm
        (d / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")
        (d / "cwd").symlink_to(cwd)
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode())

    add(1, 0, "systemd", Path("/"), "/sbin/init")
    add(100, 1, "tmux: server", tmp_path, "tmux -L bulava")
    add(101, 100, "claude", ws, "claude")
    add(102, 101, "node", ws / "web", "npm run dev")      # its session is alive
    add(200, 1, "node", ws / "web", "npm run dev")        # its session is gone
    add(300, 1, "node", tmp_path, "npm run dev")          # not in the workspace
    found = {p.pid: p.session for p in scan_processes((str(ws),), proc)}
    assert found == {101: True, 102: True, 200: False}
    for pid in range(1000, 1300):  # a busy host, and the pass's time is gone
        add(pid, 1, "node", tmp_path, "sleep")
    with pytest.raises(TimeoutError):
        scan_processes((str(ws),), proc, out_of_time=lambda: True)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="pidfd is Linux only")
def test_kill_process_signals_exactly_that_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        stat = Path(f"/proc/{child.pid}/stat").read_text()
        ticks = int(stat[stat.rindex(")") + 2:].split()[19])
        assert kill_process(Proc(child.pid, ticks + 1, T0, "/", "sleep", False)) == "gone"  # another process
        assert child.poll() is None
        assert kill_process(Proc(child.pid, ticks, T0, "/", "sleep", False)) == "done"
        assert child.wait(timeout=10) != 0
    finally:
        child.kill()


# -- when ---------------------------------------------------------------------------------------


def runs(w: World) -> list[tuple[str, str, str]]:
    return [(r["trigger"], r["scope"], r["status"]) for r in reversed(w.store.cleanup_runs())]


FREE = Situation(None, False, False)


def test_first_free_moment_runs_a_full_pass_then_idle_passes_keep_their_distance(world):
    world.cleanup.maybe_run(Situation(None, True, False))  # work waiting: an overdue pass still goes first
    assert runs(world) == [("daily", "full", "succeeded")]
    world.now += timedelta(hours=1)
    world.cleanup.maybe_run(FREE)
    world.now += timedelta(hours=1)
    world.cleanup.maybe_run(FREE)
    assert len(runs(world)) == 1  # idle long enough, but min_interval (6h) has not passed
    world.now += timedelta(hours=5)
    world.cleanup.maybe_run(Situation(None, True, False))  # a ticket waits: not idle
    world.now += timedelta(minutes=2)
    world.cleanup.maybe_run(FREE)  # free again from here
    world.now += timedelta(minutes=14)
    world.cleanup.maybe_run(FREE)
    assert len(runs(world)) == 1  # not yet idle_after (15m)
    world.now += timedelta(minutes=1)
    world.cleanup.maybe_run(FREE)
    assert runs(world)[-1] == ("idle", "full", "succeeded")


def test_the_daily_pass_survives_a_restart(world):
    world.cleanup.maybe_run(FREE)
    world.now += timedelta(hours=2)
    world.cleanup = world.make()
    world.cleanup.recover()
    world.cleanup.maybe_run(Situation(None, True, False))
    assert len(runs(world)) == 1


def test_a_busy_day_gets_an_independent_pass_that_spares_the_running_task(world):
    world.task("ACME-1", "done")
    world.task("ACME-3", "dispatched")
    holder = world.store.task("ACME-3")
    world.hold("ACME-3", T0 - timedelta(hours=30))
    old = T0 - timedelta(hours=26)
    d = world.docker
    d.add("old-labelled", created=old, finished=old, labels={"arceus.task": "ACME-1"})
    d.add("old-compose", created=old, finished=old, labels={**compose(world.ws, "web"), "arceus.task": "ACME-1"})
    d.add("running-task", status="running", created=old, labels={"arceus.task": "ACME-3"})
    d.add("running-task-db", created=old, finished=old, labels=compose(world.ws, "web"))
    web = make_repo(world, "web")
    git(web, "checkout", "-q", "-b", "agent/ACME-1-work")
    (web / "left.txt").write_text("x")
    d.dangling = 3  # the running task may be about to run one of them by id

    world.cleanup.maybe_run(Situation(holder, False, False))
    assert not any(c[0] == "image rm" for c in d.calls)

    assert runs(world) == [("daily", "independent", "succeeded")]
    assert set(d.cs) == {"old-compose", "running-task", "running-task-db"}  # compose ones wait for a free workspace
    assert (web / "left.txt").exists()
    finished = world.events_named("cleanup_finished")[-1]
    assert "free workspace" in finished["deferred"]
    world.now += timedelta(hours=1)
    world.cleanup.maybe_run(Situation(holder, False, False))
    assert len(runs(world)) == 1
    # The task is over: the first free moment makes the full pass that is overdue.
    world.store.update_task("ACME-3", state="done")
    world.cleanup.maybe_run(Situation(None, True, False))
    assert runs(world)[-1] == ("daily", "full", "succeeded")
    assert "old-compose" not in d.cs and not (web / "left.txt").exists()


def test_low_disk_brings_a_pass_forward_but_not_on_every_tick(world):
    world.cleanup.maybe_run(FREE)
    world.now += timedelta(minutes=30)
    low = Situation(None, False, True)
    world.cleanup.maybe_run(low)
    assert len(runs(world)) == 1  # retry_after (1h) since the last pass
    world.now += timedelta(minutes=31)
    world.cleanup.maybe_run(low)
    world.cleanup.maybe_run(low)
    assert [t for t, _, _ in runs(world)] == ["daily", "disk"]


def test_errors_make_a_partial_pass_that_is_retried_with_a_growing_pause(world):
    world.task("ACME-1", "done")
    old = T0 - timedelta(hours=3)
    world.docker.add("stuck", created=old, finished=old, labels={"arceus.task": "ACME-1"})
    world.docker.add("fine", created=old, finished=old, labels={"arceus.task": "ACME-1"})
    world.docker.fail = {"stuck"}
    world.cleanup.maybe_run(FREE)
    assert runs(world) == [("daily", "full", "partial")]
    assert "fine" not in world.docker.cs  # one failure does not stop the rest
    world.now += timedelta(minutes=59)
    world.cleanup.maybe_run(FREE)
    assert len(runs(world)) == 1
    world.now += timedelta(minutes=2)
    world.cleanup.maybe_run(FREE)
    assert runs(world)[-1] == ("daily", "full", "partial")  # still overdue: no pass has succeeded yet
    world.now += timedelta(minutes=90)
    world.cleanup.maybe_run(FREE)
    assert len(runs(world)) == 2  # the pause doubled to 2h
    world.docker.fail = set()
    world.now += timedelta(minutes=31)
    world.cleanup.maybe_run(FREE)
    assert runs(world)[-1] == ("daily", "full", "succeeded")
    # A later failure is retried once its pause is over, even with work waiting and nothing else due.
    world.docker.add("stuck2", created=old, finished=old, labels={"arceus.task": "ACME-1"})
    world.docker.fail = {"stuck2"}
    world.now += timedelta(hours=7)
    world.cleanup.maybe_run(Situation(None, False, True))  # low disk
    assert runs(world)[-1] == ("disk", "full", "partial")
    world.docker.fail = set()
    world.now += timedelta(minutes=61)
    world.cleanup.maybe_run(Situation(None, True, False))
    assert runs(world)[-1] == ("retry", "full", "succeeded")
    assert world.cleanup.last_runs()["ok:full"]["status"] == "succeeded"


def test_a_live_engine_session_of_a_waiting_task_defers_the_full_pass(world):
    world.task("ACME-2", "needs_input")
    world.task("ACME-1", "done")
    web = make_repo(world, "web")
    git(web, "checkout", "-q", "-b", "agent/ACME-1-work")
    (web / "left.txt").write_text("x")
    world.engine.shows["ACME-2"] = {"state": "needs_input", "attempt_live": True}

    world.cleanup.maybe_run(FREE)

    assert runs(world) == [("daily", "independent", "succeeded")]
    assert (web / "left.txt").exists()
    assert world.events_named("cleanup_deferred")[0]["reason"] == "ACME-2: its engine session is live (needs_input)"
    world.engine.shows["ACME-2"] = {"state": "needs_input", "attempt_live": False}
    world.now += timedelta(minutes=5)
    world.cleanup.maybe_run(FREE)
    assert len(runs(world)) == 1  # asked again only after a while
    world.now += timedelta(minutes=6)
    world.cleanup.maybe_run(FREE)
    assert runs(world)[-1] == ("daily", "full", "succeeded")
    assert not (web / "left.txt").exists()


def test_an_engine_that_does_not_answer_counts_as_busy(world):
    world.task("ACME-2", "code_review")
    world.engine.broken = True
    world.cleanup.maybe_run(FREE)
    assert runs(world) == [("daily", "independent", "succeeded")]
    assert "did not answer" in world.events_named("cleanup_deferred")[0]["reason"]


def test_a_pass_cut_short_is_never_counted_as_done(world):
    world.store.cleanup_started("x-full", "idle", "full", "act", iso(T0 - timedelta(hours=1)))
    world.cleanup.recover()
    assert runs(world) == [("idle", "full", "interrupted")]
    assert world.events_named("cleanup_interrupted")[0]["run"] == "x-full"
    world.cleanup.maybe_run(FREE)
    assert runs(world)[-1] == ("daily", "full", "succeeded")


def test_the_pass_has_a_time_budget(world):
    world.task("ACME-1", "done")
    old = T0 - timedelta(hours=3)
    for n in range(3):
        world.docker.add(f"c{n}", created=old, finished=old, labels={"arceus.task": "ACME-1"})
    real_rm = world.docker.remove_container

    def slow_rm(cid, volumes=True):
        world.mono.advance(400)
        real_rm(cid, volumes)
    world.docker.remove_container = slow_rm
    result = world.cleanup.run("full", "manual")
    assert result["status"] == "partial" and "out of time" in result["errors"]
    assert sorted(r for _, r in world.items(result).values()) == ["done", "done", "skipped"]


# -- Claude's advice ----------------------------------------------------------------------------


def test_advice_is_shown_and_never_removes_anything(tmp_path):
    w = World(tmp_path, llm="advise")
    seen = []

    def advise(ctx):
        seen.append(ctx)
        return [(0, "garbage", "looks like a test database"), (5, "garbage", "out of range")]
    w.advice = advise
    w.cleanup = w.make()
    w.docker.add("mystery", status="running", created=T0 - timedelta(days=2), labels=compose(w.ws, "web"))
    result = w.cleanup.run("full", "manual")
    assert result["unknown"] == [{"kind": "container", "name": "mystery", "why": "running; uses the workspace but was "
                                  f"made outside every task's hold, {T0 - timedelta(days=2):%Y-%m-%d}; image postgres:16",
                                  "advice": {"verdict": "garbage", "reason": "looks like a test database"}}]
    assert w.docker.calls == [] and "mystery" in w.docker.cs
    assert seen[0]["items"][0]["name"] == "mystery" and "tasks" in seen[0]


def test_unavailable_advice_does_not_fail_the_pass(tmp_path):
    w = World(tmp_path, llm="advise")

    def advise(ctx):
        raise AdviceUnavailable("claude exit 1: not signed in")
    w.advice = advise
    w.cleanup = w.make()
    w.docker.add("mystery", created=T0 - timedelta(days=2), finished=T0 - timedelta(days=2), labels=compose(w.ws, "web"))
    assert w.cleanup.run("full", "manual")["status"] == "succeeded"
    assert w.events_named("cleanup_advice_unavailable")[0]["reason"] == "claude exit 1: not signed in"


def test_the_advisor_runs_claude_without_tools():
    cmd = ClaudeAdvisor({}, "sonnet").command()
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet"


# -- inside the runner --------------------------------------------------------------------------


def test_runner_tick_runs_cleanup_and_metrics_show_it(tmp_path, secrets, monkeypatch):
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    w = World(tmp_path, mode="observe")
    clock = FakeClock()
    yc = youtrack_client(w.cfg, secrets, transport=__import__("httpx").MockTransport(FakeYouTrack()))
    gc = gitlab_client(w.cfg, secrets, transport=__import__("httpx").MockTransport(FakeGitLab()))
    yc.clock = gc.clock = clock
    metrics = Metrics()
    runner = loop.Runner(w.cfg, w.store, w.events, yc, gc, now=lambda: w.now, mono=clock, metrics=metrics,
                         cleanup=w.cleanup)
    runner.tick()
    assert [e["event"] for e in w.events_named("cleanup_started", "cleanup_finished")] == \
        ["cleanup_started", "cleanup_finished"]
    assert metrics.registry.get_sample_value("runner_cleanup_last_success_seconds", {"scope": "full"}) == T0.timestamp()
    assert metrics.registry.get_sample_value("runner_cleanup_last_run_seconds",
                                             {"status": "succeeded", "mode": "report"}) == T0.timestamp()


def test_runner_tells_cleanup_who_holds_the_workspace(tmp_path, secrets):
    w = World(tmp_path)
    w.task("ACME-3", "dispatched")
    runner = loop.Runner.__new__(loop.Runner)
    runner.cleanup, runner.store, runner.delivery, runner.events = w.cleanup, w.store, None, w.events
    runner.health = {"disk_free": "fail"}
    runner.clean_up(False)
    assert runs(w) == [("daily", "independent", "succeeded")]
    assert w.store.workspace_holds() == [("ACME-3", iso(T0).replace("Z", ".000Z"), iso(T0).replace("Z", ".000Z"))]


def test_an_overdue_pass_goes_before_the_next_ticket_and_lets_it_start(tmp_path, secrets, monkeypatch):
    """End to end with the fake engine: what ACME-1 left (a stopped container, an untracked file that would make
    ACME-2's start refuse a dirty tree) is cleared in the first free moment, before ACME-2 is taken."""
    from arceus_runner.config import CleanupConfig

    from .test_delivery import World as DeliveryWorld
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])
    w = DeliveryWorld(tmp_path, secrets)
    docker = FakeDocker()
    cfg = w.cfg.model_copy(update={"cleanup": CleanupConfig(mode="act")})
    w.runner.cleanup = Cleanup(cfg, w.store, w.events, docker=docker, git=Git(None, "A", "a@a"),
                               engine=w.runner.engine, now=lambda: w.now, procs=lambda roots: [])
    try:
        w.yt.add_issue(1, ("a1", "j.doe", "+agent"))
        w.tick()
        assert w.store.task("ACME-1").state == "dispatched"
        start = w.now
        docker.add("acme-platform-db-run-1", created=start + timedelta(minutes=30),
                   finished=start + timedelta(minutes=40),
                   labels={**compose(w.cfg.paths.workspace, "acme-platform", "acme-platform"),
                           "com.docker.compose.oneoff": "True"})
        (w.repo / "debug.log").write_text("left behind\n")
        w.now = start + timedelta(hours=1)
        w.settle("ACME-1", "done")
        w.tick()
        assert w.store.task("ACME-1").state == "done"
        assert "acme-platform-db-run-1" in docker.cs  # the last pass was an hour ago: not due yet

        w.yt.add_issue(2, ("b1", "j.doe", "+agent"))
        w.now = start + timedelta(hours=26)
        w.tick()

        names = [e["event"] for e in read_events(w.cfg.paths.events)
                 if e["event"] in ("cleanup_finished", "task_claimed") and e.get("task_id", "ACME-2") == "ACME-2"]
        assert names[-2:] == ["cleanup_finished", "task_claimed"]
        assert w.store.task("ACME-2").state == "dispatched"
        assert "acme-platform-db-run-1" not in docker.cs
        assert not (w.repo / "debug.log").exists()
        assert "debug.log" in git(w.repo, "show", "--name-only", "--format=", "stash@{0}^3")
    finally:
        w.events.close()
        w.store.close()


@pytest.mark.skipif(not os.environ.get("ARCEUS_REAL_DOCKER"), reason="set ARCEUS_REAL_DOCKER=1 on a host with Docker")
def test_real_docker_removes_only_what_a_finished_task_labelled(tmp_path):
    """Against the real daemon: the inspect formats are what the rules expect. Only objects this test makes are
    labelled for its tasks, and images are not pruned, so nothing else on the host is touched."""
    from arceus_runner.cleanup import Docker
    import uuid
    tag = uuid.uuid4().hex[:8].upper()
    done, waiting = f"ZZ{tag}-1", f"ZZ{tag}-2"
    w = World(tmp_path)
    w.docker = Docker()
    w.docker.dangling_images = lambda: 0
    w.cleanup = w.make()
    w.task(done, "done")
    w.task(waiting, "code_review")

    def run(*args: str) -> str:
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()

    made = {"container": [], "volume": [], "network": []}
    try:
        for key in (done, waiting):
            made["volume"].append(run("volume", "create", "--label", f"arceus.task={key}", f"zz-{key.lower()}"))
            made["network"].append(run("network", "create", "--label", f"arceus.task={key}", f"zz-{key.lower()}"))
            made["container"].append(run("create", "--label", f"arceus.task={key}", "busybox:1.36", "true"))
        w.now = datetime.now(UTC) + timedelta(hours=1)
        result = w.cleanup.run("full", "manual")
        ours = {i["name"]: (i["action"], i["result"]) for i in result["planned"] if tag.lower() in i["name"].lower()
                or i["name"] in made["container"]}
        assert result["status"] == "succeeded", result["errors"]
        left = run("ps", "-aq", "--no-trunc", "--filter", f"label=arceus.task={waiting}")
        assert left == made["container"][1]
        assert run("ps", "-aq", "--filter", f"label=arceus.task={done}") == ""
        assert run("volume", "ls", "-q", "--filter", f"label=arceus.task={done}") == ""
        assert run("network", "ls", "-q", "--filter", f"label=arceus.task={done}") == ""
        assert run("volume", "ls", "-q", "--filter", f"label=arceus.task={waiting}") == f"zz-{waiting.lower()}"
        assert ours, result["planned"]
    finally:
        for kind, ids in made.items():
            for i in ids:
                subprocess.run(["docker", *(["rm", "-f"] if kind == "container" else [kind, "rm"]), i],
                               capture_output=True)
        w.events.close()
        w.store.close()


def test_cli_plans_without_changing_anything_and_acts_when_asked(tmp_path, monkeypatch, capsys):
    import yaml

    from arceus_runner import cli
    w = World(tmp_path)
    w.task("ACME-1", "done")
    old = datetime.now(UTC) - timedelta(hours=3)
    w.docker.add("done-exited", created=old, finished=old, labels={"arceus.task": "ACME-1"})
    raw = example_raw()
    raw.pop("slack")
    raw["health"].pop("devstack_check", None)
    raw["mode"] = "live"
    raw["paths"] = {k: str(v) for k, v in w.cfg.paths.model_dump().items()}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    monkeypatch.setattr("arceus_runner.loop.Docker", lambda: w.docker)
    args = ["--config", str(tmp_path / "config.yaml"), "cleanup"]

    assert cli.main(args + ["--last"]) == 0
    assert "no cleanup pass yet" in capsys.readouterr().out
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "remove  container done-exited: exited, made by ACME-1, which is over" in out
    assert w.docker.calls == []

    assert cli.main(args + ["--act"]) == 0
    assert w.docker.calls == [("rm", "done-exited")]
    capsys.readouterr()
    assert cli.main(args + ["--last"]) == 0
    out = capsys.readouterr().out
    assert "succeeded (manual, full, act)" in out and "[done]" in out


def test_cleanup_config_reads_a_bare_off_and_refuses_unknown_keys(tmp_path):
    assert config(tmp_path, llm=False).cleanup.llm == "off"
    with pytest.raises(Exception, match="unknown|extra"):
        config(tmp_path, keep_everything=True)


def test_a_container_holding_a_protected_volume_stays(tmp_path):
    w = World(tmp_path, protect={"volumes": ["c" * 64]})
    w.task("ACME-1", "done")
    old = T0 - timedelta(hours=3)
    w.docker.add_volume("c" * 64)
    w.docker.add("db", created=old, finished=old, labels={"arceus.task": "ACME-1"}, volumes=("c" * 64,))
    result = w.cleanup.run("full", "manual")
    assert result["planned"] == [] and "db" in w.docker.cs  # `docker rm -v` would take the volume with it


def test_remove_tree_refuses_a_folder_with_a_mount_inside(tmp_path, monkeypatch):
    media = tmp_path / ".arceus" / "media"
    (media / "ACME-1" / "data").mkdir(parents=True)
    monkeypatch.setattr("arceus_runner.cleanup.mount_points", lambda: ["/", str(media / "ACME-1" / "data")])
    assert remove_tree(media / "ACME-1", media) == "kept"
    assert (media / "ACME-1" / "data").exists()
    monkeypatch.setattr("arceus_runner.cleanup.mount_points", lambda: ["/", str(media / "ACME-2")])
    assert remove_tree(media / "ACME-1", media) == "done"
    assert list(media.iterdir()) == []  # nothing left behind under another name


def test_mount_points_reads_mountinfo(tmp_path):
    from arceus_runner.cleanup import mount_points
    f = tmp_path / "mountinfo"
    f.write_text("22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
                 "40 22 8:1 /srv/data /home/ai/arceus/workspace/.arceus/media/X\\040Y rw - ext4 /dev/sda1 rw\n")
    assert mount_points(f) == ["/", "/home/ai/arceus/workspace/.arceus/media/X Y"]


def test_cli_reads_without_writing(tmp_path, monkeypatch, capsys):
    import sqlite3

    import yaml

    from arceus_runner import cli
    raw = example_raw()
    raw.pop("slack")
    raw["health"].pop("devstack_check", None)
    raw["paths"] = {"workspace": str(tmp_path / "ws"), "db": str(tmp_path / "runner.db"),
                    "events": str(tmp_path / "events.jsonl")}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    args = ["--config", str(tmp_path / "config.yaml"), "cleanup"]
    assert cli.main(args) == 1 and "no runner database" in capsys.readouterr().err
    assert not (tmp_path / "runner.db").exists()
    old = sqlite3.connect(tmp_path / "runner.db")
    old.execute("PRAGMA user_version = 9")
    old.close()
    assert cli.main(args + ["--last"]) == 1 and "schema v9" in capsys.readouterr().err
    assert sqlite3.connect(tmp_path / "runner.db").execute("PRAGMA user_version").fetchone()[0] == 9


def test_a_full_pass_with_errors_is_retried_even_after_an_independent_one_succeeds(world):
    world.task("ACME-1", "done")
    world.cleanup.maybe_run(FREE)
    world.task("ACME-3", "dispatched")
    old = T0 - timedelta(hours=3)
    # A compose container: only a full pass removes it.
    world.docker.add("stuck", created=old, finished=old, labels={**compose(world.ws, "web"), "arceus.task": "ACME-1"})
    world.docker.fail = {"stuck"}
    world.now += timedelta(hours=7)
    world.cleanup.maybe_run(Situation(None, False, True))
    assert runs(world)[-1] == ("disk", "full", "partial")
    world.docker.fail = set()
    world.now += timedelta(minutes=65)
    world.cleanup.maybe_run(Situation(world.store.task("ACME-3"), False, True))  # busy, still low on disk
    assert runs(world)[-1] == ("disk", "independent", "succeeded")
    assert "stuck" in world.docker.cs
    world.now += timedelta(minutes=10)
    world.cleanup.maybe_run(Situation(None, True, False))
    assert runs(world)[-1] == ("retry", "full", "succeeded")
    assert "stuck" not in world.docker.cs


def test_an_unreadable_mount_table_on_linux_keeps_the_folder(tmp_path, monkeypatch):
    media = tmp_path / ".arceus" / "media"
    (media / "ACME-1").mkdir(parents=True)
    monkeypatch.setattr("arceus_runner.cleanup.mount_points", lambda: None)
    assert remove_tree(media / "ACME-1", media) == "kept"


def test_read_only_store_opens_a_path_with_uri_characters(tmp_path):
    path = tmp_path / "team #1?x" / "runner.db"
    ReviewStore(path).close()
    store = ReviewStore.read_only(path)
    assert store.cleanup_runs() == []
    store.close()


def test_only_images_labelled_for_a_finished_task_go(world):
    world.task("ACME-1", "done")
    world.task("ACME-2", "code_review")
    d = world.docker
    d.add_image("sha256:a1", ("acme1-e2e:latest",), {"arceus.task": "ACME-1"})
    d.add_image("sha256:a1used", ("acme1-db:latest",), {"arceus.task": "ACME-1"})
    d.add_image("sha256:a2", ("acme2-e2e:latest",), {"arceus.task": "ACME-2"})  # its task waits: kept
    d.add_image("sha256:other", ("otherteam/api:1",))                            # built here, nobody's
    d.add_image("sha256:py", ("python:3.12",))
    d.add_image("sha256:node", ("node:24",), size=2 << 30)
    d.add_image("sha256:untagged-a1", (), {"arceus.task": "ACME-1"})  # `docker build --label` without -t
    d.add("runs-it", status="running", created=T0, binds=("/srv/x",), image="sha256:a1used")
    d.dangling = 5
    d.df_rows = {"Build Cache": {"Type": "Build Cache", "Size": "11.38GB", "Reclaimable": "3.179GB"}}
    result = world.cleanup.run("full", "manual")
    assert world.items(result) == {"acme1-e2e:latest": ("remove", "done"), "untagged-a1": ("remove", "done")}
    assert world.unknown(result) == {"5 untagged images", "otherteam/api:1", "python:3.12", "node:24", "build cache",
                                     "runs-it"}
    images = [i for i in result["unknown"] if i["kind"] == "image" and ":" in i["name"]]
    assert images[0]["name"] == "node:24" and "2.1 GB" in images[0]["why"]  # the biggest first
    assert set(d.imgs) == {"sha256:a1used", "sha256:a2", "sha256:other", "sha256:py", "sha256:node"}
    assert ("image rm", "sha256:other") not in d.calls


def test_a_hung_docker_command_is_cut_at_the_pass_deadline(tmp_path):
    """The real Docker wrapper: a command that never answers ends when the pass's budget does, not after its
    own timeout, and the pass ends partial with what was left marked skipped."""
    import time

    from arceus_runner.cleanup import Docker
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    d = Docker(binary=str(fake), timeout=60)
    d.deadline = time.monotonic() + 1
    started = time.monotonic()
    with pytest.raises(DockerError, match="out of time"):
        d.containers()
    assert time.monotonic() - started < 5
    with pytest.raises(DockerError, match="out of time"):
        d.containers()  # past the deadline: not even started


def test_a_command_cut_at_the_deadline_leaves_the_rest_skipped(world):
    world.task("ACME-1", "done")
    old = T0 - timedelta(hours=3)
    for n in range(3):
        world.docker.add(f"c{n}", created=old, finished=old, labels={"arceus.task": "ACME-1"})

    def hung(cid, volumes=True):
        world.mono.advance(700)
        raise DockerError("docker rm: no answer in 12s, out of time")
    world.docker.remove_container = hung
    result = world.cleanup.run("full", "manual")
    assert result["status"] == "partial" and result["errors"] == ["out of time"]
    assert sorted(r for _, r in world.items(result).values()) == ["skipped", "skipped", "skipped"]


def test_planning_stops_when_the_budget_is_gone(world):
    real = world.docker.containers

    def slow():
        world.mono.advance(700)
        return real()
    world.docker.containers = slow
    (world.ws / ".arceus" / "media" / "ACME-404").mkdir(parents=True)
    result = world.cleanup.run("full", "manual")
    assert result["status"] == "partial"
    assert result["errors"] == ["out of time before folders was looked at"]
    assert result["unknown"] == []  # .arceus was never read


def test_the_pass_gives_its_deadline_to_docker_and_git(world):
    seen = []
    real = world.docker.containers

    def look():
        seen.append((world.docker.deadline, world.cleanup.git.deadline))
        return real()
    world.docker.containers = look
    world.cleanup.run("full", "manual")
    assert seen[0][0] is not None and seen[0][0] == seen[0][1]
    assert world.docker.deadline is None and world.cleanup.git.deadline is None


def test_a_volume_replaced_between_plan_and_removal_stays(world):
    """Volumes are removed by name: a same-named volume made after the plan (another task's, protected) is not
    the one that was planned, even before any container mounts it."""
    world.task("ACME-1", "done")
    world.task("ACME-3", "dispatched")
    d = world.docker
    d.add_volume("testdb", {"arceus.task": "ACME-1"})
    d.add_volume("cache", {"arceus.task": "ACME-1"})
    d.add_volume("late-keep", {"arceus.task": "ACME-1"})
    d.add_volume("late-mount", {"arceus.task": "ACME-1"})
    items, _ = world.cleanup.plan("full")
    assert {i.name for i in items if i.action == "remove"} == {"testdb", "cache", "late-keep", "late-mount"}
    del d.vols["testdb"]
    d.add_volume("testdb", {"arceus.task": "ACME-3", "arceus.keep": "true"}, created=T0)
    d.vols["late-keep"]["Labels"]["arceus.keep"] = "true"
    d.add("new-user", status="created", created=T0, binds=("/srv/x",), volumes=("late-mount",))
    world.cleanup.execute("manual", items, errors := [], deadline=world.mono() + 600)
    result = {i.name: (i.result, i.detail.get("kept")) for i in items}
    assert result == {"testdb": ("kept", "a different volume has this name now"), "cache": ("done", None),
                      "late-keep": ("kept", "a different volume has this name now"),
                      "late-mount": ("kept", "a container uses it")}
    assert set(d.vols) == {"testdb", "late-keep", "late-mount"} and errors == []
    assert ("volume rm", "testdb") not in d.calls


def test_protection_added_after_the_plan_is_honoured(world):
    world.task("ACME-1", "done")
    world.hold("ACME-1", T0 - timedelta(hours=10))
    world.hold(None, T0 - timedelta(hours=4))
    old = T0 - timedelta(hours=9)
    d = world.docker
    d.add("db", created=old, finished=old, labels={"arceus.task": "ACME-1"})
    d.add("web-1", created=old, finished=old, labels={**compose(world.ws, "web"), "com.docker.compose.oneoff": "True"})
    d.add_network("n1", "web_default", old, {"com.docker.compose.project": "web"})
    items, _ = world.cleanup.plan("full")
    assert {i.name for i in items if i.action == "remove"} == {"db", "web-1", "web_default"}
    d.cs["db"]["Config"]["Labels"]["arceus.keep"] = "true"
    d.nets["n1"]["Labels"]["arceus.keep"] = "true"
    world.cleanup.c = world.cleanup.c.model_copy(update={"protect": world.cleanup.c.protect.model_copy(
        update={"compose_projects": ["web"]})})
    world.cleanup.execute("manual", items, [], deadline=world.mono() + 600)
    assert {i.name: i.result for i in items} == {"db": "kept", "web-1": "kept", "web_default": "kept"}
    assert d.calls == []


def test_a_finished_tasks_running_container_and_its_compose_network_both_go(world):
    """The network is in use by the very container this pass stops: it goes right after it. Another project's
    networks, empty or not, stay."""
    world.task("ACME-1", "done")
    made = T0 - timedelta(days=2)  # while ACME-1 held the workspace; its label ties the container to it too
    world.hold("ACME-1", made - timedelta(hours=1))
    world.hold(None, made + timedelta(hours=1))
    e2e = {"com.docker.compose.project": "acme1-e2e", "com.docker.compose.project.working_dir": "/tmp/e2e"}
    d = world.docker
    d.add("e2e-app", status="running", created=made, labels={**e2e, "arceus.task": "ACME-1"},
          networks=("acme1-e2e_default",))
    d.add("other-app", status="running", created=made, labels={"com.docker.compose.project": "otherteam"},
          networks=("otherteam_default",))
    d.add_network("n1", "acme1-e2e_default", made, {"com.docker.compose.project": "acme1-e2e"}, running=("e2e-app",))
    d.add_network("n2", "otherteam_default", made, {"com.docker.compose.project": "otherteam"}, running=("other-app",))
    d.add_network("n3", "otherteam_idle", made, {"com.docker.compose.project": "otherteam"})
    result = world.cleanup.run("full", "manual")
    assert world.items(result) == {"e2e-app": ("stop", "done"), "acme1-e2e_default": ("remove", "done")}
    assert d.calls == [("stop", "e2e-app"), ("rm", "e2e-app"), ("network rm", "n1")]
    assert set(d.nets) == {"n2", "n3"} and "other-app" in d.cs


def test_a_network_left_after_its_containers_went_still_goes_on_a_later_pass(world):
    world.task("ACME-1", "done")
    made = T0 - timedelta(days=2)
    world.hold("ACME-1", made - timedelta(hours=1))
    world.hold(None, made + timedelta(hours=1))
    e2e = {"com.docker.compose.project": "acme1-e2e"}
    d = world.docker
    d.add("e2e-db", created=made, finished=made, labels={**e2e, "arceus.task": "ACME-1"},
          networks=("acme1-e2e_default",))
    d.add_network("n1", "acme1-e2e_default", made, e2e)
    d.fail = {"n1"}
    assert world.cleanup.run("full", "manual")["status"] == "partial"
    assert "e2e-db" not in d.cs and "n1" in d.nets
    d.fail = set()
    world.cleanup = world.make()  # a restart in between: the project is remembered in the database
    result = world.cleanup.run("full", "manual")
    assert world.items(result) == {"acme1-e2e_default": ("remove", "done")} and d.nets == {}


# -- what goes with a container, observe, a swapped folder, the whole inventory ----------------------------------


ANON = {"com.docker.volume.anonymous": ""}


def test_rm_v_never_takes_a_volume_a_waiting_task_or_another_container_has(world):
    old = T0 - timedelta(hours=3)
    world.task("ACME-1", "done")
    world.task("ACME-2", "needs_input")
    d = world.docker
    labelled, shared, own = "a" * 64, "b" * 64, "c" * 64
    d.add_volume(labelled, {**ANON, "arceus.task": "ACME-2"})
    d.add_volume(shared, dict(ANON))
    d.add_volume(own, dict(ANON))
    d.add("done-labelled", created=old, finished=old, labels={"arceus.task": "ACME-1"}, volumes=(labelled,))
    d.add("done-shared", created=old, finished=old, labels={"arceus.task": "ACME-1"}, volumes=(shared,))
    d.add("waiting", created=old, finished=old, labels={"arceus.task": "ACME-2"}, volumes=(shared,))
    d.add("done-own", created=old, finished=old, labels={"arceus.task": "ACME-1"}, volumes=(own,))

    result = world.cleanup.run("full", "manual")

    assert result["status"] == "succeeded"
    assert sorted(d.calls) == [("rm", "done-own"), ("rm (volumes stay)", "done-labelled"),
                               ("rm (volumes stay)", "done-shared")]
    assert labelled in d.vols and shared in d.vols and own not in d.vols
    assert "waiting" in d.cs


def test_a_volume_protected_after_the_plan_keeps_its_container(world):
    old = T0 - timedelta(hours=3)
    world.task("ACME-1", "done")
    vol = "d" * 64
    world.docker.add_volume(vol, dict(ANON))
    world.docker.add("done", created=old, finished=old, labels={"arceus.task": "ACME-1"}, volumes=(vol,))
    items, errors = world.cleanup.plan("full")
    assert errors == [] and [(i.name, i.action) for i in items if i.action != "report"] == [("done", "remove")]
    world.docker.vols[vol]["Labels"]["arceus.keep"] = "true"  # protected between the plan and the removal
    world.store.cleanup_started("r1", "manual", "full", "act", iso(T0))
    world.cleanup.execute("r1", items, errors, deadline=world.mono() + 600)
    done = next(i for i in items if i.name == "done")
    assert done.result == "kept" and done.detail["kept"] == "a volume it uses is protected now"
    assert world.docker.calls == [] and "done" in world.docker.cs and vol in world.docker.vols


def test_observe_removes_nothing_even_when_a_person_asks_for_act(tmp_path, monkeypatch, capsys):
    import yaml

    from arceus_runner import cli
    w = World(tmp_path, mode="observe")
    w.task("ACME-1", "done")
    old = T0 - timedelta(hours=3)
    w.docker.add("done-exited", created=old, finished=old, labels={"arceus.task": "ACME-1"})
    # The pass itself: report whatever its mode was set to.
    w.cleanup.mode = "act"
    monkeypatch.setattr(w.cleanup, "execute", lambda *a, **k: pytest.fail("observe must never execute"))
    result = w.cleanup.run("full", "manual")
    assert result["mode"] == "report" and w.docker.calls == [] and w.killed == []
    # And `cleanup --act` refuses before it takes the lock.
    raw = example_raw()
    raw.pop("slack")
    raw["health"].pop("devstack_check", None)
    raw["mode"] = "observe"
    raw["paths"] = {k: str(v) for k, v in w.cfg.paths.model_dump().items()}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    monkeypatch.setattr("arceus_runner.loop.Docker", lambda: w.docker)
    assert cli.main(["--config", str(tmp_path / "config.yaml"), "cleanup", "--act"]) == 1
    assert "mode: observe" in capsys.readouterr().err and w.docker.calls == []
    w.events.close()
    w.store.close()


def test_a_folder_swapped_in_after_the_checks_is_kept_with_what_is_in_it(tmp_path, monkeypatch):
    media = tmp_path / ".arceus" / "media"
    (media / "ACME-1").mkdir(parents=True)
    (media / "ACME-1" / "old.png").write_text("leftover")

    def swap() -> list[str]:  # runs among the checks: the planned folder moves away, another takes its name
        (media / "ACME-1").rename(media / "elsewhere")
        (media / "ACME-1").mkdir()
        (media / "ACME-1" / "precious.txt").write_text("not the planned folder")
        return ["/"]
    monkeypatch.setattr("arceus_runner.cleanup.mount_points", swap)
    it = Item("folder", str(media / "ACME-1"), ".arceus/media/ACME-1", "delete", "")
    assert remove_tree(media / "ACME-1", media, it) == "kept"
    assert it.detail["kept"] == "another folder took its place while it was checked"
    assert (media / "ACME-1" / "precious.txt").read_text() == "not the planned folder"
    assert (media / "elsewhere" / "old.png").exists()
    assert sorted(p.name for p in media.iterdir()) == ["ACME-1", "elsewhere"]


def test_the_last_pass_shows_every_item_and_the_whole_size_however_many(world, capsys):
    from arceus_runner.cli import _last_items, _print_cleanup
    old = T0 - timedelta(hours=3)
    world.task("ACME-1", "done")
    d = world.docker
    for n in range(120):
        d.add(f"done-{n:03}", created=old, finished=old, labels={"arceus.task": "ACME-1"})
    for n in range(250):
        d.add_volume(f"cache-{n:03}")
    d.sizes = {"volumes": {f"cache-{n:03}": 1000**2 for n in range(250)}}

    result = world.cleanup.run("full", "manual")

    assert len(result["unknown"]) == 200 and result["unknown_total"] == 250
    assert len(result["planned"]) == 100 and result["planned_total"] == 120
    assert result["unknown_bytes"] == 250 * 1000**2
    planned, unknown = _last_items(world.store, result["run"], result)
    assert len(planned) == 120 and {p["result"] for p in planned} == {"done"}
    assert len(unknown) == 250 and sum(u["bytes"] for u in unknown) == 250 * 1000**2
    _print_cleanup(planned, unknown, [], None, result["unknown_bytes"])
    out = capsys.readouterr().out
    assert "cache-249" in out and "done-119" in out and "about 0.2 GB in all" in out


def test_dirty_files_reads_renames_in_either_column(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    for name, text in (("a", "x\n"), ("b", "y\n"), ("c", "z\n")):
        (repo / name).write_text(text)
    git(repo, "add", ".")
    git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "f")
    git(repo, "mv", "a", "a2")                       # "R "
    git(repo, "mv", "c", "c2")
    (repo / "c2").write_text("z\nmore\n")            # "RM"
    (repo / "b").rename(repo / "b2")
    git(repo, "add", "-N", "b2")                     # " R"
    (repo / "new").write_text("n\n")                 # "??"
    assert sorted(Git(None, "A", "a@b").dirty_files(repo)) == ["a2", "b2", "c2", "new"]


def test_a_hold_open_at_the_upgrade_to_schema_11_ends_where_it_began(tmp_path):
    import sqlite3

    from arceus_runner.db import MIGRATIONS, _split
    path = tmp_path / "runner.db"
    conn = sqlite3.connect(path)
    for n in range(10):
        for stmt in _split(MIGRATIONS[n]):
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {n + 1}")
    conn.execute("INSERT INTO workspace_holds (task_id, started_at) VALUES ('ACME-1', ?)", (iso(T0),))
    conn.commit()
    conn.close()
    store = ReviewStore(path)
    try:
        assert store.version == 11
        assert store.workspace_holds() == [("ACME-1", iso(T0), iso(T0))]  # seen by no tick yet: nothing is its
        store.close_open_holds()
        assert store.conn.execute("SELECT ended_at FROM workspace_holds").fetchone()[0] == iso(T0)
    finally:
        store.close()


def test_a_folder_swapped_while_it_is_emptied_loses_only_the_planned_contents(tmp_path, monkeypatch):
    from arceus_runner import cleanup as cl
    media = tmp_path / ".arceus" / "media"
    (media / "ACME-1" / "deep").mkdir(parents=True)
    (media / "ACME-1" / "deep" / "old.png").write_text("leftover")
    real_empty, swapped = cl._empty, []

    def empty_after_swap(fd, mount, where, out_of_time):
        if not swapped:  # the folder is open now: it moves away, and another takes its name
            swapped.append(True)
            (media / "ACME-1").rename(media / "moved")
            (media / "ACME-1").mkdir()
            (media / "ACME-1" / "precious.txt").write_text("not the planned folder")
        return real_empty(fd, mount, where, out_of_time)
    monkeypatch.setattr(cl, "_empty", empty_after_swap)
    it = Item("folder", str(media / "ACME-1"), ".arceus/media/ACME-1", "delete", "")
    assert remove_tree(media / "ACME-1", media, it) == "kept"
    assert it.detail["kept"] == "emptied, but another folder took its place meanwhile"
    assert (media / "ACME-1" / "precious.txt").exists()      # the folder put in its place: untouched
    assert list((media / "moved").iterdir()) == []            # the planned one: emptied through its handle


def test_a_volume_that_cannot_be_read_stays_with_its_container_gone(world):
    old = T0 - timedelta(hours=3)
    world.task("ACME-1", "done")
    vol = "e" * 64
    world.docker.add_volume(vol, dict(ANON))
    world.docker.add("done", created=old, finished=old, labels={"arceus.task": "ACME-1"}, volumes=(vol,))
    world.docker.volume = lambda name: None  # inspect failed
    result = world.cleanup.run("full", "manual")
    assert result["status"] == "succeeded"
    assert world.docker.calls == [("rm (volumes stay)", "done")] and vol in world.docker.vols


def test_a_mount_that_appears_inside_after_the_checks_stops_the_removal(tmp_path, monkeypatch):
    """A bind mount of the same file system has the same st_dev; the mount id of the open folder tells it."""
    from arceus_runner import cleanup as cl
    media = tmp_path / ".arceus" / "media"
    (media / "ACME-1" / "mnt").mkdir(parents=True)
    (media / "ACME-1" / "mnt" / "outside.txt").write_text("another place's data")
    (media / "ACME-1" / "a.png").write_text("x")
    mounted = os.stat(media / "ACME-1" / "mnt").st_ino
    monkeypatch.setattr(cl, "_mount_id", lambda fd: 2 if os.fstat(fd).st_ino == mounted else 1)
    it = Item("folder", str(media / "ACME-1"), ".arceus/media/ACME-1", "delete", "")
    with pytest.raises(OSError, match="something is mounted here"):
        remove_tree(media / "ACME-1", media, it)
    assert (media / "ACME-1" / "mnt" / "outside.txt").read_text() == "another place's data"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/self/fdinfo is Linux's")
def test_mount_id_reads_fdinfo(tmp_path):
    from arceus_runner.cleanup import _mount_id
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert isinstance(_mount_id(fd), int)
    finally:
        os.close(fd)
