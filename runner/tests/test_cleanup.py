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
from arceus_runner.cleanup import AdviceUnavailable, ClaudeAdvisor, Cleanup, Item, Situation
from arceus_runner.config import Config
from arceus_runner.db import ReviewStore
from arceus_runner.dockerops import DockerError
from arceus_runner.engine import EngineError
from arceus_runner.events import EventLog
from arceus_runner.gitops import Git
from arceus_runner.machine import Proc, kill_process, remove_tree, scan_processes
from arceus_runner.metrics import Metrics

from .conftest import FakeClock, example_raw, providers, read_events
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
        self.imgs: dict[str, dict] = {}
        self.deadline = None
        self.calls: list[tuple] = []
        self.fail: set[str] = set()  # ids whose removal fails

    def available(self) -> bool:
        return True

    def add(self, cid: str, *, status: str = "exited", created: datetime, finished: datetime | None = None,
            labels: dict | None = None, binds: tuple = (), volumes: tuple = (), networks: tuple = (),
            image: str = "sha256:postgres", auto_remove: bool = False) -> None:
        self.cs[cid] = {
            "Id": cid, "Name": f"/{cid}", "Created": iso(created), "Image": image,
            "State": {"Status": status, "FinishedAt": iso(finished) if finished else "0001-01-01T00:00:00Z"},
            "Config": {"Labels": labels or {}, "Image": "postgres:16"}, "HostConfig": {"AutoRemove": auto_remove},
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

    def labelled_mounts(self, label: str, disposable: str) -> list[tuple[str, bool, list[str]]]:
        self.calls.append(("ps", label))
        out = []
        for c in self.cs.values():
            labels = c["Config"]["Labels"]
            names = [m["Name"] for m in c["Mounts"] if m.get("Type") == "volume" and len(m["Name"]) == 64]
            if labels.get(label) and names:
                out.append((labels[label], labels.get(disposable) == "true" or c["HostConfig"]["AutoRemove"], names))
        return out

    def container(self, cid: str) -> dict | None:
        return copy.deepcopy(self.cs.get(cid))

    def volumes(self) -> list[dict]:
        return copy.deepcopy(list(self.vols.values()))

    def networks(self) -> list[dict]:
        return copy.deepcopy(list(self.nets.values()))

    def dangling_images(self) -> list[str]:
        return sorted(i for i, img in self.imgs.items() if not img["RepoTags"])

    def add_image(self, iid: str, tags: tuple = (), labels: dict | None = None, size: int = 1 << 30,
                  created: datetime = T0 - timedelta(days=2)) -> None:
        self.imgs[iid] = {"Id": iid, "RepoTags": list(tags), "Config": {"Labels": labels or {}}, "Size": size,
                          "Created": iso(created)}

    def add_dangling(self, n: int, created: datetime = T0 - timedelta(days=2)) -> None:
        """`n` untagged images nothing labels: old builds a newer one took the tag from."""
        for k in range(n):
            self.add_image(f"sha256:{len(self.imgs) + k:064x}", created=created)

    def image(self, iid: str) -> dict | None:
        return copy.deepcopy(self.imgs.get(iid))

    now = T0  # the daemon's clock
    cache: list[tuple[datetime, int]] = []  # build cache records: (last used, bytes)
    says_total = True  # `builder prune` ends with "Total: <size>"

    def prune_build_cache(self, unused_for: timedelta) -> int | None:
        self.calls.append(("builder prune", unused_for))
        if "build-cache" in self.fail:
            raise DockerError("docker builder prune: error")
        old = [r for r in self.cache if self.now - r[0] >= unused_for]  # BuildKit: by the last use
        self.cache = [r for r in self.cache if r not in old]
        return sum(b for _t, b in old) if self.says_total else None

    def images(self) -> list[dict]:
        return copy.deepcopy(list(self.imgs.values()))

    df_rows: dict = {}  # what `docker system df` would say, by type
    sizes: dict = {}  # what `docker system df -v` would say: {"volumes": {name: bytes}, "images": {id: (own, total)}}

    def usage(self) -> dict:
        return copy.deepcopy(self.sizes)

    def disk_usage(self) -> dict:
        """`df_rows` as set; else Docker's totals of what there is (every image here is its own layers)."""
        rows = copy.deepcopy(self.df_rows)
        if self.df_broken is not None:
            return self.df_broken
        # Docker prints every type, "0B" when there is none.
        if "Images" not in rows:
            rows["Images"] = {"Type": "Images", "Size": f"{sum(i['Size'] for i in self.imgs.values())}B"}
        if "Build Cache" not in rows:
            rows["Build Cache"] = {"Type": "Build Cache", "Size": f"{sum(b for _t, b in self.cache)}B",
                                   "Reclaimable": f"{sum(b for _t, b in self.cache)}B"}
        return rows

    df_broken: dict | None = None  # what `docker system df` says instead, once set

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
    world.task("ACME-5", "stopped", failed_at=T0 - timedelta(days=1))  # people closed every MR: `/continue` works
    world.task("ACME-6", "stopped", failed_at=T0 - timedelta(days=30))
    world.task("ACME-7", "stopped", updated=T0 - timedelta(hours=1))   # the ticket was moved on by hand
    d = world.docker
    d.add("done-exited", created=old, finished=old, labels={"arceus.task": "ACME-1"})
    d.add("done-running", status="running", created=old, labels={"arceus.task": "ACME-1"})
    d.add("done-just-exited", created=old, finished=T0 - timedelta(minutes=5), labels={"arceus.task": "ACME-1"})
    d.add("review-exited", created=old, finished=old, labels={"arceus.task": "ACME-2"})
    d.add("resumable-exited", created=old, finished=old, labels={"arceus.task": "ACME-3"})
    d.add("old-failure-exited", created=old, finished=old, labels={"arceus.task": "ACME-4"})
    d.add("closed-exited", created=old, finished=old, labels={"arceus.task": "ACME-5"})
    d.add("old-closed-exited", created=old, finished=old, labels={"arceus.task": "ACME-6"})
    d.add("moved-on-exited", created=old, finished=old, labels={"arceus.task": "ACME-7"})
    d.add("never-ours", created=old, finished=old, labels={"arceus.task": "ACME-99"})
    d.add("grafana", status="running", created=old, labels={"com.docker.compose.project": "arceus-observability"})
    d.add("console-nginx", status="exited", created=old, finished=old,
          labels={"com.docker.compose.project": "arceus-console"})
    d.add("kept", created=old, finished=old, labels={"arceus.task": "ACME-1", "arceus.keep": "true"})
    d.add("other-project", created=old, finished=old, binds=("/srv/other",))

    result = world.cleanup.run("full", "manual")

    assert result["status"] == "succeeded"
    assert world.items(result) == {"done-exited": ("remove", "done"), "done-running": ("stop", "done"),
                                   "old-failure-exited": ("remove", "done"), "old-closed-exited": ("remove", "done"),
                                   "moved-on-exited": ("remove", "done")}
    # Everything nobody owns is listed for the person who cleans by hand; a waiting task's things are not.
    assert world.unknown(result) == {"never-ours", "other-project"}
    assert set(d.cs) == {"done-just-exited", "review-exited", "resumable-exited", "closed-exited", "never-ours",
                         "grafana", "console-nginx", "kept", "other-project"}


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
    w = World(tmp_path, protect={"compose_projects": ["acme-dev"]})
    w.task("ACME-1", "done")
    w.hold("ACME-1", T0 - timedelta(hours=10))
    w.docker.add("web", created=T0 - timedelta(hours=9), finished=T0 - timedelta(hours=9),
                 labels={**compose(w.ws, "acme-dev"), "arceus.task": "ACME-1"})
    w.docker.add_network("n", "acme-dev_default", T0 - timedelta(hours=9), {"com.docker.compose.project": "acme-dev"})
    result = w.cleanup.run("full", "manual")
    assert result["planned"] == [] and result["unknown"] == []
    assert w.docker.calls == []


def test_report_mode_changes_nothing(tmp_path):
    w = World(tmp_path, mode="observe")  # the runner's observe mode is always report, whatever cleanup.mode says
    w.task("ACME-1", "done")
    w.docker.add("done-exited", created=T0 - timedelta(hours=3), finished=T0 - timedelta(hours=3),
                 labels={"arceus.task": "ACME-1"})
    w.docker.add_dangling(4)
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
    assert branch.result == "kept" and branch.detail["kept"] == "the code host no longer has its commits"
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


def test_the_files_from_the_ticket_and_the_review_replies_stay_as_long_as_the_notes(world):
    world.task("ACME-1", "done", updated=T0 - timedelta(days=91))
    world.task("ACME-2", "done", updated=T0 - timedelta(days=20))
    world.task("ACME-3", "code_review", updated=T0 - timedelta(days=120))  # still waiting: everything stays
    base = world.ws / ".arceus"
    for kind in ("ticket", "replies"):
        for key in ("ACME-1", "ACME-2", "ACME-3"):
            (base / kind / key).mkdir(parents=True)
            (base / kind / key / "x").write_text("x")

    result = world.cleanup.run("full", "manual")

    assert world.items(result) == {".arceus/ticket/ACME-1": ("delete", "done"),
                                   ".arceus/replies/ACME-1": ("delete", "done")}
    assert world.unknown(result) == set()
    for kind in ("ticket", "replies"):
        assert sorted(p.name for p in (base / kind).iterdir()) == ["ACME-2", "ACME-3"]


def test_the_report_says_what_removing_by_hand_would_free_and_whose_the_name_says(world, capsys):
    """Sizes from `docker system df -v`: an image frees only the layers no other image shares. A name that spells
    a task is a hint for the person, never a reason to remove anything."""
    from arceus_runner.cli import _print_cleanup
    world.task("SHOP-7173", "code_review")
    world.task("SHOP-717", "done")  # its key is a prefix of the other's: no hint from shop7173-*
    d = world.docker
    d.add("shop7173-mysql", status="running", created=T0 - timedelta(days=2), binds=("/srv/x",))
    d.add_volume("shop7173-mysql")
    d.add_volume("e" * 64, {"com.docker.volume.anonymous": ""})
    d.add_volume("f" * 64, {"com.docker.volume.anonymous": ""})
    d.add_network("n1", "shop7173", T0 - timedelta(days=2))
    d.add_image("sha256:assets", ("shop-assets-js:latest",), size=5_090_000_000)
    d.add_image("sha256:node", ("node:24",), size=1_650_000_000)
    d.add_image("sha256:mysql", ("mysql:8.0",), size=1_100_000_000)
    d.cs["shop7173-mysql"]["Image"] = "sha256:mysql"
    d.sizes = {"volumes": {"shop7173-mysql": 281_000_000, "e" * 64: 300_000_000, "f" * 64: 4_000},
               "images": {"sha256:assets": (594_000_000, 5_090_000_000), "sha256:node": (1_650_000_000, 1_650_000_000)}}
    d.df_rows = {"Images": {"Type": "Images", "Size": "7.84GB", "Reclaimable": "2.244GB (28%)"}}

    result = world.cleanup.run("full", "manual")

    why = {i["name"]: i["why"] for i in result["unknown"]}
    assert "5.1 GB, 594.0 MB of it its own" in why["shop-assets-js:latest"]
    assert "; 1.6 GB, made" in why["node:24"]  # nothing shared worth saying
    # The biggest own part first; what removing all of them frees is Docker's count, a shared layer once.
    assert [i["name"] for i in result["unknown"] if i["kind"] == "image"] == \
        ["node:24", "shop-assets-js:latest", "2 images above"]
    assert why["2 images above"].startswith("Docker counts 2.244GB in images no container uses")
    assert "281.0 MB" in why["shop7173-mysql"] and "300.0 MB" in why["2 anonymous volumes"]
    for name in ("shop7173-mysql", "shop7173"):
        assert why[name].endswith("the name says SHOP-7173 (waiting)"), why[name]
    assert "SHOP-717 " not in " ".join(why.values()) and "mysql:8.0" not in why
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
    monkeypatch.setattr("arceus_runner.machine.os.unlink", slow_unlink)

    result = world.cleanup.run("full", "manual")

    assert result["status"] == "partial" and "out of time" in result["errors"]
    assert world.items(result) == {".arceus/media/ACME-1": ("delete", "skipped")}
    assert media.is_dir() and len(list(media.iterdir())) == 2  # the rest stays under its own name


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
    from arceus_runner.machine import tree_size
    big = tmp_path / "media" / "ACME-1"
    big.mkdir(parents=True)
    for n in range(600):
        (big / f"{n}.png").write_bytes(b"x")
    calls = {"n": 0}

    def out_of_time() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # gone after the first look, in the middle of one folder
    with pytest.raises(TimeoutError):
        tree_size(big, out_of_time)


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
    monkeypatch.setattr("arceus_runner.machine.os.stat", lambda p, *a, **k: os.stat_result(
        (*real_stat(p)[:2], real_stat(p).st_dev + 1, *real_stat(p)[3:])) if str(p) == str(media) else real_stat(p))
    monkeypatch.setattr("arceus_runner.machine.mount_points", lambda: [])  # macOS: no mount table to read
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
    d.add_dangling(3, created=T0 - timedelta(days=30))  # the running task may be about to run one by id

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
    tracker, host = providers(w.cfg, secrets, FakeYouTrack(), FakeGitLab(), clock)
    metrics = Metrics()
    runner = loop.Runner(w.cfg, w.store, w.events, tracker, host, now=lambda: w.now, mono=clock, metrics=metrics,
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
    from arceus_runner.dockerops import Docker
    import uuid
    tag = uuid.uuid4().hex[:8].upper()
    done, waiting = f"ZZ{tag}-1", f"ZZ{tag}-2"
    # The host's own daemon: its build cache and images are not this test's, so the age rules are off.
    w = World(tmp_path, build_cache_after=None, dangling_images_after=None)
    w.docker = Docker()
    w.docker.dangling_images = lambda: []
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


@pytest.mark.skipif(not os.environ.get("ARCEUS_REAL_DOCKER"), reason="set ARCEUS_REAL_DOCKER=1 on a host with Docker")
def test_real_docker_releases_only_what_a_waiting_task_marked_disposable(tmp_path):
    """Against the real daemon: a task waiting past `release_after` loses its `arceus.disposable` container with
    its anonymous volume, its `--rm` container, its marked volume and its network; its unmarked container keeps
    its anonymous volume and its unmarked volume stays. The next message to the agent names what went."""
    from arceus_runner.cleanup import release_note
    from arceus_runner.dockerops import Docker
    import uuid
    tag = uuid.uuid4().hex[:8].lower()
    key = f"ZZ{tag.upper()}-2"
    w = World(tmp_path, release_after="3d", build_cache_after=None, dangling_images_after=None)
    w.docker = Docker()
    w.docker.dangling_images = lambda: []
    w.cleanup = w.make()
    real_now = datetime.now(UTC)
    w.task(key, "code_review")
    w.store.update_task(key, attempt_started=(real_now - timedelta(days=6)).isoformat())
    worked(w, key, real_now - timedelta(days=6), real_now - timedelta(days=5))

    def run(*args: str) -> str:
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()

    own = ["--label", f"arceus.task={key}"]
    gone = [*own, "--label", "arceus.disposable=true"]
    made = {"container": [], "volume": [], "network": []}
    try:
        made["container"].append(run("create", *gone, "--name", f"zz-{tag}-db", "-v", "/data", "busybox:1.36", "true"))
        made["container"].append(run("create", *own, "--name", f"zz-{tag}-repro", "-v", "/data", "busybox:1.36",
                                     "true"))
        made["container"].append(run("run", "-d", "--rm", *own, "--name", f"zz-{tag}-app", "busybox:1.36",
                                     "sleep", "600"))
        db_vol = run("inspect", "-f", "{{range .Mounts}}{{.Name}}{{end}}", f"zz-{tag}-db")
        repro_vol = run("inspect", "-f", "{{range .Mounts}}{{.Name}}{{end}}", f"zz-{tag}-repro")
        made["volume"] += [db_vol, repro_vol,
                           run("volume", "create", *gone, f"zz-{tag}-testdb"), run("volume", "create", *own,
                                                                                    f"zz-{tag}-dump")]
        made["network"].append(run("network", "create", *own, f"zz-{tag}-net"))
        w.now = real_now + timedelta(hours=1)

        result = w.cleanup.run("full", "manual")

        assert result["status"] == "succeeded", result["errors"]
        assert run("ps", "-a", "--format", "{{.Names}}", "--filter", f"label=arceus.task={key}") == f"zz-{tag}-repro"
        volumes = set(run("volume", "ls", "-q").split())
        assert db_vol not in volumes and f"zz-{tag}-testdb" not in volumes
        assert {repro_vol, f"zz-{tag}-dump"} <= volumes
        assert run("network", "ls", "-q", "--filter", f"name=zz-{tag}-net") == ""
        note = release_note(w.store, key)
        assert f"zz-{tag}-db" in note and f"zz-{tag}-app" in note and f"zz-{tag}-testdb" in note
        assert f"zz-{tag}-repro" not in note and f"zz-{tag}-dump" not in note
        # The agent's next round makes them again from the same commands: nothing half-removed holds a name.
        made["container"].append(run("create", *gone, "--name", f"zz-{tag}-db", "-v", "/data", "busybox:1.36",
                                     "true"))
        made["container"].append(run("run", "-d", "--rm", *own, "--name", f"zz-{tag}-app", "busybox:1.36",
                                     "sleep", "600"))
        made["volume"].append(run("volume", "create", *gone, f"zz-{tag}-testdb"))
        made["network"].append(run("network", "create", *own, f"zz-{tag}-net"))
        made["volume"].append(run("inspect", "-f", "{{range .Mounts}}{{.Name}}{{end}}", f"zz-{tag}-db"))
        assert run("start", "-a", f"zz-{tag}-db") == "" and run("inspect", "-f", "{{.State.ExitCode}}",
                                                              f"zz-{tag}-db") == "0"
    finally:
        for kind, ids in made.items():
            for i in ids:
                subprocess.run(["docker", *(["rm", "-f", "-v"] if kind == "container" else [kind, "rm"]), i],
                               capture_output=True)
        w.events.close()
        w.store.close()


@pytest.mark.skipif(not os.environ.get("ARCEUS_REAL_DOCKER"), reason="set ARCEUS_REAL_DOCKER=1 on a host with Docker")
def test_real_docker_removes_only_the_anonymous_volumes_a_finished_tasks_container_left(tmp_path):
    """Against the real daemon: mongo-style `-v /data/db -v /data/configdb` volumes of a container labelled for a
    finished task, removed with `docker rm -f` (no -v), go a day after a pass first saw them unused: the runner saw
    the container use them. The same volumes of an unlabelled container stay, and so does one a stopped container
    still has. Every volume the host had before the test is protected as well."""
    from arceus_runner.dockerops import Docker
    import uuid

    def run(*args: str) -> str:
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()

    w = World(tmp_path, protect={"volumes": run("volume", "ls", "-q").split()}, stopped_after="30d",
              build_cache_after=None, dangling_images_after=None)
    w.docker = Docker()
    w.docker.dangling_images = lambda: []
    w.cleanup = w.make()
    tag = uuid.uuid4().hex[:8]
    key = f"ZZ{tag.upper()}-1"
    w.task(key, "done")
    made: list[str] = []

    def database(name: str, *labels: str) -> list[str]:
        run("create", "--name", name, *labels, "-v", "/data/db", "-v", "/data/configdb", "busybox:1.36", "true")
        found = run("inspect", "-f", "{{range .Mounts}}{{.Name}} {{end}}", name).split()
        made.extend(found)
        return found
    try:
        left = database(f"zz-{tag}-db", "--label", f"arceus.task={key}")
        stranger = database(f"zz-{tag}-person")
        w.now = datetime.now(UTC)
        w.cleanup.observe()  # the runner's tick, while the containers exist
        run("rm", "-f", f"zz-{tag}-db", f"zz-{tag}-person")  # as the agents did: no -v
        run("create", "--name", f"zz-{tag}-kept", "-v", "/data", "busybox:1.36", "true")
        kept = run("inspect", "-f", "{{range .Mounts}}{{.Name}}{{end}}", f"zz-{tag}-kept")
        made.append(kept)

        first = w.cleanup.run("full", "manual")
        assert first["status"] == "succeeded", first["errors"]
        assert set(left) <= set(run("volume", "ls", "-q").split())
        w.now += timedelta(hours=25)
        second = w.cleanup.run("full", "manual")

        assert second["status"] == "succeeded", second["errors"]
        assert w.items(second) == {f"anonymous volume {v[:12]}": ("remove", "done") for v in left}
        volumes = set(run("volume", "ls", "-q").split())
        assert not set(left) & volumes and {kept, *stranger} <= volumes
    finally:
        subprocess.run(["docker", "rm", "-f", f"zz-{tag}-kept", f"zz-{tag}-db", f"zz-{tag}-person"],
                       capture_output=True)
        for v in made:
            subprocess.run(["docker", "volume", "rm", v], capture_output=True)
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
    monkeypatch.setattr("arceus_runner.machine.mount_points", lambda: ["/", str(media / "ACME-1" / "data")])
    assert remove_tree(media / "ACME-1", media) == "kept"
    assert (media / "ACME-1" / "data").exists()
    monkeypatch.setattr("arceus_runner.machine.mount_points", lambda: ["/", str(media / "ACME-2")])
    assert remove_tree(media / "ACME-1", media) == "done"
    assert list(media.iterdir()) == []  # nothing left behind under another name


def test_mount_points_reads_mountinfo(tmp_path):
    from arceus_runner.machine import mount_points
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
    monkeypatch.setattr("arceus_runner.machine.mount_points", lambda: None)
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
    d.add_dangling(5)
    d.df_rows = {"Build Cache": {"Type": "Build Cache", "Size": "11.38GB", "Reclaimable": "3.179GB"}}
    result = world.cleanup.run("full", "manual")
    # The build cache goes by its own rule (build_cache_after), the untagged images are under a week old.
    assert world.items(result) == {"acme1-e2e:latest": ("remove", "done"), "untagged-a1": ("remove", "done"),
                                   "build cache": ("prune", "done")}
    assert world.unknown(result) == {"5 untagged images", "otherteam/api:1", "python:3.12", "node:24", "runs-it"}
    images = [i for i in result["unknown"] if i["kind"] == "image" and ":" in i["name"]]
    assert images[0]["name"] == "node:24" and "2.1 GB" in images[0]["why"]  # the biggest first
    assert set(d.imgs) - set(d.dangling_images()) == {"sha256:a1used", "sha256:a2", "sha256:other", "sha256:py",
                                                      "sha256:node"}
    assert len(d.dangling_images()) == 5
    assert ("image rm", "sha256:other") not in d.calls


def test_a_hung_docker_command_is_cut_at_the_pass_deadline(tmp_path):
    """The real Docker wrapper: a command that never answers ends when the pass's budget does, not after its
    own timeout, and the pass ends partial with what was left marked skipped."""
    import time

    from arceus_runner.dockerops import Docker
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")   # exec: the process cut is the one that sleeps
    fake.chmod(0o755)
    d = Docker(binary=str(fake), timeout=60)
    d.deadline = time.monotonic() + 0.3
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
    monkeypatch.setattr("arceus_runner.machine.mount_points", swap)
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
        assert store.version == len(MIGRATIONS)
        assert store.workspace_holds() == [("ACME-1", iso(T0), iso(T0))]  # seen by no tick yet: nothing is its
        store.close_open_holds()
        assert store.conn.execute("SELECT ended_at FROM workspace_holds").fetchone()[0] == iso(T0)
    finally:
        store.close()


def test_a_folder_swapped_while_it_is_emptied_loses_only_the_planned_contents(tmp_path, monkeypatch):
    from arceus_runner import machine as cl
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
    from arceus_runner import machine as cl
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
    from arceus_runner.machine import _mount_id
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert isinstance(_mount_id(fd), int)
    finally:
        os.close(fd)


# -- a task left waiting (release_after) ---------------------------------------------------------


@pytest.fixture
def waiting_world(tmp_path):
    w = World(tmp_path, release_after="3d")
    yield w
    w.events.close()
    w.store.close()


def worked(w: World, key: str, start: datetime, end: datetime) -> None:
    """`key` held the workspace from `start` to `end`: a round of its work."""
    w.store.hold_workspace(key, iso(start))
    w.store.hold_workspace(None, iso(end))


def left_behind(w: World, key: str, made: datetime, done: datetime) -> None:
    """What a task's agent typically leaves. Marked as made again by a script (`arceus.disposable=true`) or `--rm`:
    a test database with its anonymous volume, a running app container, a seeded volume. Not marked, so possibly
    the only copy of something: a reproduction container with its anonymous volume, and a volume. Plus a labelled
    network and image, and a compose service it started (tied to it by time only)."""
    tag = key.lower().replace("-", "")
    own, gone = {"arceus.task": key}, {"arceus.task": key, "arceus.disposable": "true"}
    d = w.docker
    d.add_volume(f"{'a' * 63}{tag[-1]}", {"com.docker.volume.anonymous": ""})
    d.add(f"{tag}-db", created=made, finished=done, labels=gone, volumes=(f"{'a' * 63}{tag[-1]}",))
    d.add(f"{tag}-app", status="running", created=made, labels=own, auto_remove=True)
    d.add_volume(f"{'b' * 63}{tag[-1]}", {"com.docker.volume.anonymous": ""})
    d.add(f"{tag}-repro", created=made, finished=done, labels=own, volumes=(f"{'b' * 63}{tag[-1]}",))
    d.add(f"{tag}-web-1", created=made, finished=done, labels=compose(w.ws, "web"), binds=(str(w.ws / "web"),))
    d.add_volume(f"{tag}-testdb", gone)
    d.add_volume(f"{tag}-dump", own)
    d.add_network(f"net-{tag}", f"{tag}-net", made, own)
    d.add_image(f"sha256:{tag}", (f"{tag}-app:dev",), own)


def test_a_task_left_waiting_loses_what_can_be_made_again_and_keeps_its_work(waiting_world):
    """A merge request waits for review for days: after `release_after` with no work, what is marked as made again
    goes (containers, test data), with its network, image and dev server; what nothing marks so stays, and so do
    its branch, uncommitted files, notes and screenshots."""
    w = waiting_world
    w.task("ACME-2", "code_review", updated=T0)  # touched by bookkeeping today: not work
    worked(w, "ACME-2", T0 - timedelta(days=5), T0 - timedelta(days=4))
    left_behind(w, "ACME-2", T0 - timedelta(days=5) + timedelta(hours=1), T0 - timedelta(days=4))
    w.procs = [Proc(300, 3, T0 - timedelta(days=4, hours=12), w.ws, "python manage.py runserver", session=False)]
    web = make_repo(w, "web")
    git(web, "checkout", "-q", "-b", "agent/ACME-2-work")
    (web / "half-done.py").write_text("x")
    for kind in ("notes", "media", "replies"):
        (w.ws / ".arceus" / kind / "ACME-2").mkdir(parents=True)

    result = w.cleanup.run("full", "manual")

    assert w.items(result) == {
        "acme2-db": ("remove", "done"), "acme2-app": ("stop", "done"), "acme2-testdb": ("remove", "done"),
        "acme2-net": ("remove", "done"), "acme2-app:dev": ("remove", "done"),
        "python manage.py runserver (pid 300)": ("kill", "done")}
    why = {i["name"]: i["why"] for i in result["planned"]}
    assert why["acme2-testdb"] == "test data of ACME-2, which has waited 4 days with no work"
    assert ("rm", "acme2-db") in w.docker.calls and "a" * 63 + "2" not in w.docker.vols  # its test database went too
    assert {"acme2-repro", "acme2-dump", "b" * 63 + "2"} <= set(w.docker.cs) | set(w.docker.vols)  # maybe unique
    assert "acme2-web-1" in w.docker.cs  # a compose service tied by time, nothing marks it disposable
    assert (web / "half-done.py").exists() and git(web, "stash", "list") == ""
    assert git(web, "rev-parse", "--abbrev-ref", "HEAD") == "agent/ACME-2-work"
    assert all((w.ws / ".arceus" / kind / "ACME-2").exists() for kind in ("notes", "media", "replies"))


def test_the_wait_is_counted_from_the_last_work(waiting_world):
    w = waiting_world
    w.task("ACME-2", "code_review")  # under the limit by a minute
    worked(w, "ACME-2", T0 - timedelta(days=4), T0 - timedelta(days=3) + timedelta(minutes=1))
    w.task("ACME-3", "code_review")  # an old round, but a `/continue` (or the MR comment) since
    worked(w, "ACME-3", T0 - timedelta(days=9), T0 - timedelta(days=8))
    w.store.update_task("ACME-3", published_at_ms=int((T0 - timedelta(days=1)).timestamp() * 1000))
    w.task("ACME-4", "needs_input")  # asked its question two days ago
    w.store.update_task("ACME-4", question_at_ms=int((T0 - timedelta(days=2)).timestamp() * 1000))
    w.task("ACME-5", "needs_input", updated=T0 - timedelta(days=6))  # nothing else known of it: its record's age
    for key in ("ACME-2", "ACME-3", "ACME-4", "ACME-5"):
        w.docker.add(f"{key}-db", created=T0 - timedelta(days=10), finished=T0 - timedelta(days=9),
                     labels={"arceus.task": key, "arceus.disposable": "true"})

    assert w.items(w.cleanup.run("full", "manual")) == {"ACME-5-db": ("remove", "done")}

    w.now = T0 + timedelta(minutes=2)
    assert w.items(w.cleanup.run("full", "manual")) == {"ACME-2-db": ("remove", "done")}


def test_a_task_about_to_go_on_keeps_everything(waiting_world):
    """Review comments waiting for the agent, or an accepted `/continue`: the round is next, whatever the wait."""
    w = waiting_world
    w.task("ACME-2", "code_review")
    w.task("ACME-3", "failed", failed_at=T0 - timedelta(days=5))
    for key in ("ACME-2", "ACME-3"):
        worked(w, key, T0 - timedelta(days=6), T0 - timedelta(days=5))
        w.docker.add(f"{key}-db", created=T0 - timedelta(days=6), finished=T0 - timedelta(days=5),
                     labels={"arceus.task": key, "arceus.disposable": "true"})
    w.store.add_signal("n1", "ACME-2", "review", "j.doe", "please rename it", iso(T0 - timedelta(hours=1)))
    w.store.action_intent("yt-continue-c1", "ACME-3", "continue", '{"author": "j.doe", "comment": "c1"}')

    assert w.items(w.cleanup.run("full", "manual")) == {}
    assert {"ACME-2-db", "ACME-3-db"} <= set(w.docker.cs)


def test_an_answer_that_comes_during_the_pass_keeps_what_was_planned(waiting_world, monkeypatch):
    w = waiting_world
    w.task("ACME-3", "failed", failed_at=T0 - timedelta(days=5))
    worked(w, "ACME-3", T0 - timedelta(days=6), T0 - timedelta(days=5))
    w.docker.add("acme3-db", created=T0 - timedelta(days=6), finished=T0 - timedelta(days=5),
                 labels={"arceus.task": "ACME-3", "arceus.disposable": "true"})
    w.docker.add_volume("acme3-testdb", {"arceus.task": "ACME-3", "arceus.disposable": "true"})
    plan = w.cleanup.plan

    def plan_then_continue(*a, **kw):
        found = plan(*a, **kw)
        w.store.action_intent("yt-continue-c1", "ACME-3", "continue", '{"author": "j.doe", "comment": "c1"}')
        return found
    monkeypatch.setattr(w.cleanup, "plan", plan_then_continue)

    result = w.cleanup.run("full", "manual")

    assert w.items(result) == {"acme3-db": ("remove", "kept"), "acme3-testdb": ("remove", "kept")}
    assert "acme3-db" in w.docker.cs and "acme3-testdb" in w.docker.vols


def test_a_live_or_silent_engine_session_keeps_a_waiting_tasks_things_in_an_independent_pass(waiting_world):
    """A full pass waits for a quiet engine anyway; an independent one runs beside a working task, so it asks the
    engine about each task it would release."""
    w = waiting_world
    w.task("ACME-1", "dispatched")
    w.task("ACME-2", "needs_input")
    worked(w, "ACME-2", T0 - timedelta(days=6), T0 - timedelta(days=5))
    w.docker.add("acme2-db", created=T0 - timedelta(days=6), finished=T0 - timedelta(days=5),
                 labels={"arceus.task": "ACME-2", "arceus.disposable": "true"})
    holder = w.store.task("ACME-1")

    w.engine.shows["ACME-2"] = {"state": "needs_input", "attempt_live": True}
    assert w.items(w.make().run("independent", "daily", holder)) == {}
    w.engine.shows.clear()
    w.engine.broken = True
    assert w.items(w.make().run("independent", "daily", holder)) == {}
    w.engine.broken = False
    assert w.items(w.make().run("independent", "daily", holder)) == {"acme2-db": ("remove", "done")}


def test_report_mode_lists_a_waiting_tasks_things_and_removes_nothing(tmp_path):
    w = World(tmp_path, cleanup_mode="report", release_after="3d")
    try:
        w.task("ACME-2", "code_review")
        worked(w, "ACME-2", T0 - timedelta(days=5), T0 - timedelta(days=4))
        left_behind(w, "ACME-2", T0 - timedelta(days=5) + timedelta(hours=1), T0 - timedelta(days=4))
        before = (copy.deepcopy(w.docker.cs), copy.deepcopy(w.docker.vols), copy.deepcopy(w.docker.nets))

        result = w.cleanup.run("full", "manual")

        assert {n: a for n, (a, _r) in w.items(result).items()} == {
            "acme2-db": "remove", "acme2-app": "stop", "acme2-testdb": "remove", "acme2-net": "remove",
            "acme2-app:dev": "remove"}
        assert (w.docker.cs, w.docker.vols, w.docker.nets) == before and w.docker.calls == []
    finally:
        w.events.close()
        w.store.close()


def test_without_release_after_a_waiting_task_keeps_everything(world):
    world.task("ACME-2", "code_review")
    worked(world, "ACME-2", T0 - timedelta(days=60), T0 - timedelta(days=59))
    left_behind(world, "ACME-2", T0 - timedelta(days=60), T0 - timedelta(days=59))
    assert world.items(world.cleanup.run("full", "manual")) == {}


def test_release_after_is_a_wait_not_a_pause(tmp_path):
    with pytest.raises(ValueError, match="at least 1h"):
        config(tmp_path, release_after="30m")
    assert config(tmp_path, release_after="2d").cleanup.release_after == timedelta(days=2)


def test_a_mark_taken_off_during_the_pass_keeps_the_container(waiting_world, monkeypatch):
    w = waiting_world
    w.task("ACME-2", "code_review")
    worked(w, "ACME-2", T0 - timedelta(days=6), T0 - timedelta(days=5))
    w.docker.add("acme2-db", created=T0 - timedelta(days=6), finished=T0 - timedelta(days=5),
                 labels={"arceus.task": "ACME-2", "arceus.disposable": "true"})
    plan = w.cleanup.plan

    def plan_then_unmark(*a, **kw):
        found = plan(*a, **kw)
        del w.docker.cs["acme2-db"]["Config"]["Labels"]["arceus.disposable"]  # the agent took the mark off
        return found
    monkeypatch.setattr(w.cleanup, "plan", plan_then_unmark)

    assert w.items(w.cleanup.run("full", "manual")) == {"acme2-db": ("remove", "kept")}
    assert "acme2-db" in w.docker.cs


def test_a_release_cut_short_goes_on_at_the_next_pass_and_says_what_it_did(waiting_world, monkeypatch):
    """The pass's budget runs out after the first removal: the rest is `skipped`, the pass `partial`, and the next
    pass removes what is left. What the agent is told lists only what was really removed."""
    from arceus_runner.cleanup import release_note
    w = waiting_world
    w.task("ACME-2", "code_review")
    w.store.update_task("ACME-2", attempt_started=iso(T0 - timedelta(days=6)))
    worked(w, "ACME-2", T0 - timedelta(days=6), T0 - timedelta(days=5))
    for name in ("acme2-a", "acme2-b"):
        w.docker.add(name, created=T0 - timedelta(days=6), finished=T0 - timedelta(days=5),
                     labels={"arceus.task": "ACME-2", "arceus.disposable": "true"})
    remove = w.docker.remove_container

    def slow_remove(cid, volumes=True):
        remove(cid, volumes)
        w.mono.advance(w.cfg.cleanup.max_duration.total_seconds() + 1)
    monkeypatch.setattr(w.docker, "remove_container", slow_remove)

    first = w.cleanup.run("full", "manual")
    assert first["status"] == "partial"
    assert sorted(w.items(first).values()) == [("remove", "done"), ("remove", "skipped")]
    [left] = [n for n in ("acme2-a", "acme2-b") if n in w.docker.cs]
    assert release_note(w.store, "ACME-2").count("acme2-") == 1 and left not in release_note(w.store, "ACME-2")

    w.now += w.cfg.cleanup.retry_after
    second = w.cleanup.run("full", "retry")
    assert second["status"] == "succeeded" and w.items(second) == {left: ("remove", "done")}
    assert "removed what can be made again: containers acme2-a, acme2-b. " in release_note(w.store, "ACME-2")


def test_the_note_names_only_what_an_acting_pass_removed_since_the_last_attempt(waiting_world):
    from arceus_runner.cleanup import release_note
    w = waiting_world
    w.task("ACME-2", "code_review")
    w.store.update_task("ACME-2", attempt_started=iso(T0 - timedelta(days=5)))
    w.store.cleanup_started("old", "daily", "full", "act", iso(T0 - timedelta(days=9)))
    w.store.cleanup_item("old", "container", "c0", "remove", "done", "ACME-2", {"name": "told-before"})
    w.store.cleanup_started("seen", "daily", "full", "report", iso(T0 - timedelta(days=1)))
    w.store.cleanup_item("seen", "container", "c1", "remove", "planned", "ACME-2", {"name": "only-planned"})
    assert release_note(w.store, "ACME-2") is None
    w.store.cleanup_started("now", "daily", "full", "act", iso(T0))
    w.store.cleanup_item("now", "image", "sha256:1", "remove", "done", "ACME-2", {"name": "acme2-app:dev"})
    w.store.cleanup_item("now", "network", "n1", "remove", "done", "ACME-2", {"name": "acme2-net"})
    w.store.cleanup_item("now", "volume", "v`rm -rf`", "remove", "done", "ACME-2", {"name": "v`rm -rf`"})
    note = release_note(w.store, "ACME-2")
    assert "networks acme2-net; images acme2-app:dev." in note
    assert "told-before" not in note and "only-planned" not in note and "rm -rf" not in note


# -- anonymous volumes left behind (orphaned_volumes_after) ---------------------------------------


def anon(n: int) -> str:
    return f"{n:064x}"


def orphan(w: World, n: int, created: datetime = T0 - timedelta(days=2)) -> str:
    w.docker.add_volume(anon(n), {"com.docker.volume.anonymous": ""}, created=created)
    return anon(n)


def left_by(w: World, key: str | None, n: int, *, disposable: bool = False) -> str:
    """A container (labelled for `key`) with the anonymous volume a database image makes, seen by the runner while
    it exists, then removed with `docker rm -f` and no `-v`: the volume stays, with no label and no container."""
    name = orphan(w, n)
    labels = {"arceus.task": key, **({"arceus.disposable": "true"} if disposable else {})} if key else {}
    w.docker.add(f"db-{n}", status="running", created=T0 - timedelta(days=2), labels=labels, volumes=(name,))
    w.cleanup._observed = None
    w.cleanup.observe()
    del w.docker.cs[f"db-{n}"]
    return name


def test_an_anonymous_volume_a_finished_tasks_container_left_goes_a_day_after_a_pass_saw_it_unused(world):
    """`docker rm -f` without `-v` leaves the database's anonymous volume behind. The runner saw the task's
    labelled container use it, so it is the task's: counted at first, removed once unused for a day."""
    world.task("ACME-1", "done")
    left = left_by(world, "ACME-1", 1)

    first = world.cleanup.run("full", "manual")
    assert world.items(first) == {} and "1 anonymous volumes" in world.unknown(first)
    world.now += timedelta(hours=23)
    assert world.items(world.cleanup.run("full", "manual")) == {}
    world.now += timedelta(hours=1)
    result = world.cleanup.run("full", "manual")

    assert world.items(result) == {f"anonymous volume {left[:12]}": ("remove", "done")}
    [why] = [i["why"] for i in result["planned"]]
    assert why == ("anonymous, and no container has used it since 2026-10-20; a container labelled for ACME-1 used "
                   "it and was removed without `-v`; ACME-1, which is over")
    assert left not in world.docker.vols


def test_an_anonymous_volume_nobody_can_be_tied_to_stays_however_long_it_is_unused(world):
    """It can still be mounted by its ID, and may be the only copy of a person's database: unused alone does not
    make it garbage. Nor does being made while a task held the workspace."""
    world.task("ACME-1", "done")
    worked(world, "ACME-1", T0 - timedelta(days=3), T0 - timedelta(days=1))
    strangers = [orphan(world, 1), left_by(world, None, 2)]  # never seen used; used by an unlabelled container
    world.cleanup.run("full", "manual")
    world.now += timedelta(days=60)
    result = world.cleanup.run("full", "manual")
    assert world.items(result) == {} and set(strangers) <= set(world.docker.vols)
    [count] = [i for i in result["unknown"] if i["name"] == "2 anonymous volumes"]
    assert "can still be mounted by their ID" in count["why"] and "prune_unowned_volumes" in count["why"]


def test_prune_unowned_volumes_removes_them_host_wide_when_asked(tmp_path):
    w = World(tmp_path, prune_unowned_volumes=True, protect={"volumes": [anon(3)]})
    try:
        stranger, kept = orphan(w, 1), orphan(w, 3)
        w.cleanup.run("full", "manual")
        w.now += timedelta(days=1)
        result = w.cleanup.run("full", "manual")
        assert w.items(result) == {f"anonymous volume {stranger[:12]}": ("remove", "done")}
        assert "`prune_unowned_volumes` is on" in result["planned"][0]["why"] and kept in w.docker.vols
    finally:
        w.events.close()
        w.store.close()


def test_a_waiting_tasks_volume_goes_only_if_its_container_was_marked_disposable(waiting_world):
    w = waiting_world
    w.task("ACME-2", "code_review")
    worked(w, "ACME-2", T0 - timedelta(days=6), T0 - timedelta(days=5))
    w.task("ACME-3", "code_review")
    seeded, repro, fresh = left_by(w, "ACME-2", 1, disposable=True), left_by(w, "ACME-2", 2), left_by(w, "ACME-3", 3)
    w.cleanup.run("full", "manual")
    w.now += timedelta(days=1)
    assert w.items(w.cleanup.run("full", "manual")) == {f"anonymous volume {seeded[:12]}": ("remove", "done")}
    assert {repro, fresh} <= set(w.docker.vols)  # unmarked; a task with recent work (ACME-3 was never idle)


def test_a_volume_two_tasks_containers_used_is_nobodys(world):
    world.task("ACME-1", "done")
    world.task("ACME-2", "done")
    shared = left_by(world, "ACME-1", 1)
    world.docker.add("other", created=T0, labels={"arceus.task": "ACME-2"}, volumes=(shared,))
    world.cleanup._observed = None
    world.cleanup.observe()
    del world.docker.cs["other"]
    world.cleanup.run("full", "manual")
    world.now += timedelta(days=1)
    assert world.items(world.cleanup.run("full", "manual")) == {} and shared in world.docker.vols


def test_observing_is_one_cheap_listing_every_few_minutes(world):
    world.cleanup.observe()
    world.cleanup.observe()
    world.now += timedelta(minutes=2)
    world.cleanup.observe()
    assert world.docker.calls.count(("ps", "arceus.task")) == 2


def test_a_volume_used_again_or_made_anew_starts_its_day_again(world):
    world.task("ACME-1", "done")
    d = world.docker
    again, anew = left_by(world, "ACME-1", 1), left_by(world, "ACME-1", 2)
    world.cleanup.run("full", "manual")
    world.now += timedelta(hours=12)
    d.add("restarted", status="running", created=world.now, volumes=(again,))  # a person mounted it by its ID
    del d.vols[anew]
    orphan(world, 2, created=world.now)  # the same name, a new volume
    world.cleanup.run("full", "manual")
    del d.cs["restarted"]
    world.now += timedelta(hours=13)  # 25 h after the first pass, 13 h after these changes
    assert world.items(world.cleanup.run("full", "manual")) == {}
    world.now += timedelta(hours=24)
    assert set(world.items(world.cleanup.run("full", "manual"))) == {f"anonymous volume {again[:12]}",
                                                                     f"anonymous volume {anew[:12]}"}


def test_a_pass_that_could_not_list_volumes_does_not_restart_the_clock(world, monkeypatch):
    world.task("ACME-1", "done")
    left = left_by(world, "ACME-1", 1)
    world.cleanup.run("full", "manual")
    world.now += timedelta(hours=12)
    monkeypatch.setattr(world.docker, "available", lambda: False)
    world.cleanup.run("full", "manual")
    monkeypatch.undo()
    world.now += timedelta(hours=12)
    assert world.items(world.cleanup.run("full", "manual")) == {f"anonymous volume {left[:12]}": ("remove", "done")}


def test_a_container_that_mounts_it_during_the_pass_keeps_it(world, monkeypatch):
    world.task("ACME-1", "done")
    left = left_by(world, "ACME-1", 1)
    world.cleanup.run("full", "manual")
    world.now += timedelta(days=1)
    plan = world.cleanup.plan

    def plan_then_mount(*a, **kw):
        found = plan(*a, **kw)
        world.docker.add("came-back", status="running", created=world.now, volumes=(left,))
        return found
    monkeypatch.setattr(world.cleanup, "plan", plan_then_mount)

    assert world.items(world.cleanup.run("full", "manual")) == {f"anonymous volume {left[:12]}": ("remove", "kept")}
    assert left in world.docker.vols


def test_an_independent_pass_removes_them_while_another_task_works(world):
    world.task("ACME-1", "done")
    world.task("ACME-2", "dispatched")
    left = left_by(world, "ACME-1", 1)
    holder = world.store.task("ACME-2")
    world.cleanup.run("independent", "daily", holder)
    world.now += timedelta(days=1)
    assert world.items(world.cleanup.run("independent", "daily", holder)) == {
        f"anonymous volume {left[:12]}": ("remove", "done")}


def test_without_orphaned_volumes_after_they_are_only_counted(tmp_path):
    w = World(tmp_path, orphaned_volumes_after=None)
    try:
        w.task("ACME-1", "done")
        left_by(w, "ACME-1", 1)
        w.cleanup.run("full", "manual")
        w.now += timedelta(days=30)
        result = w.cleanup.run("full", "manual")
        assert w.items(result) == {} and anon(1) in w.docker.vols
        [count] = [i for i in result["unknown"] if i["name"] == "1 anonymous volumes"]
        assert "`docker volume prune` removes these" in count["why"]
    finally:
        w.events.close()
        w.store.close()
    with pytest.raises(ValueError, match="at least 1h"):
        config(tmp_path, orphaned_volumes_after="10m")


def test_a_week_of_test_databases_removed_without_v_is_cleared(world):
    """The staging06 week: agents ran mongo and mysql with the task label and removed them with `docker rm -f`;
    each left anonymous volumes. A day after the runner first sees them unused, the finished tasks' are gone; a
    person's database volume beside them stays."""
    for k in range(10):
        world.task(f"EDCMP-{k}", "done")
    for n in range(136):
        left_by(world, f"EDCMP-{n % 10}", n + 1)
    persons = orphan(world, 999)
    first = world.cleanup.run("full", "manual")
    assert "137 anonymous volumes" in world.unknown(first)
    world.now += timedelta(days=1)
    result = world.cleanup.run("full", "manual")
    assert result["planned_total"] == 136 and set(world.docker.vols) == {persons}
    assert all(r == ("remove", "done") for r in world.items(result).values())

# -- the age rules: build cache, untagged images, unused tagged images ----------------------------


def test_build_cache_no_build_used_for_a_week_goes_and_the_rest_stays(world, capsys):
    """BuildKit counts a record's age from its last use: a record made long ago and used yesterday stays."""
    from arceus_runner.cli import _print_cleanup
    d = world.docker
    d.cache = [(T0 - timedelta(days=10), 1_800_000_000), (T0 - timedelta(days=1), 300_000_000)]
    result = world.cleanup.run("full", "manual")
    assert world.items(result) == {"build cache": ("prune", "done")}
    assert d.calls == [("builder prune", timedelta(days=7))]
    assert d.cache == [(T0 - timedelta(days=1), 300_000_000)]
    assert result["freed_by_rule"] == {"build_cache": 1_800_000_000}
    _print_cleanup(result["planned"], result["unknown"], result["errors"], freed_by_rule=result["freed_by_rule"])
    assert "Freed, as Docker counts it: build cache 1.8 GB" in capsys.readouterr().out


def test_old_untagged_images_go_and_those_a_container_or_a_protection_holds_stay(tmp_path):
    w = World(tmp_path, protect={"compose_projects": ["acme-dev"]})
    w.task("ACME-2", "code_review")  # waits: what is labelled for it stays, whatever its age
    d, old = w.docker, T0 - timedelta(days=30)
    d.add_image("sha256:old", created=old, size=700_000_000)
    d.add_image("sha256:stopped-uses-it", created=old)
    d.add("stopped", created=old, finished=old, binds=("/srv/x",), image="sha256:stopped-uses-it")
    d.add_image("sha256:fresh", created=T0 - timedelta(days=6))
    d.add_image("sha256:waiting-task", (), {"arceus.task": "ACME-2"}, created=old)
    d.add_image("sha256:kept", (), {"arceus.keep": "true"}, created=old)
    d.add_image("sha256:dev-stack", (), {"com.docker.compose.project": "acme-dev"}, created=old)
    d.add_image("sha256:tagged", ("otherteam/api:1",), created=old)
    w.cleanup = w.make()
    result = w.cleanup.run("full", "manual")
    assert w.items(result) == {"untagged image old": ("remove", "done")}
    assert set(d.imgs) == {"sha256:stopped-uses-it", "sha256:fresh", "sha256:waiting-task", "sha256:kept",
                           "sha256:dev-stack", "sha256:tagged"}
    why = {i["name"]: i["why"] for i in result["unknown"]}
    assert why["2 untagged images"] == "1 made less than 7 days ago, 1 used by a stopped or running container: they stay"
    assert "otherteam/api:1" in why  # tagged, no task label: reported while unused_images_after is off
    # Docker's own count before the rule and after it: the one image's 700 MB.
    assert result["freed_by_rule"] == {"dangling_images": 700_000_000}
    w.events.close()
    w.store.close()


def test_tagged_images_without_a_task_label_stay_while_unused_images_after_is_off(world):
    d = world.docker
    d.add_image("sha256:base", ("python:3.12",), created=T0 - timedelta(days=400))
    result = world.cleanup.run("full", "manual")
    assert world.items(result) == {} and "python:3.12" in world.unknown(result)
    assert not [c for c in d.calls if c[0] == "image rm"] and "freed_by_rule" not in result


def test_unused_images_after_removes_old_tagged_images_and_never_a_protected_one(tmp_path):
    w = World(tmp_path, unused_images_after="30d",
              protect={"images": ["postgres", "node:*"], "compose_projects": ["acme-dev"]})
    w.task("ACME-1", "done")
    d, old = w.docker, T0 - timedelta(days=60)
    d.add_image("sha256:api", ("otherteam/api:1", "otherteam/api:latest"), created=old, size=400_000_000)
    d.add_image("sha256:pg", ("postgres:16",), created=old)                   # protect.images: postgres
    d.add_image("sha256:node", ("node:24",), created=old)                     # protect.images: node:*
    d.add_image("sha256:py", ("python:3.12",), created=T0 - timedelta(days=3))  # younger than 30 days
    d.add_image("sha256:keep", ("acme/tool:1",), {"arceus.keep": "true"}, created=old)
    d.add_image("sha256:dev", ("acme-dev-web:latest",), {"com.docker.compose.project": "acme-dev"}, created=old)
    d.add_image("sha256:used", ("mysql:8.0",), created=old)
    d.add("stopped", created=old, finished=old, binds=("/srv/x",), image="sha256:used")
    # A task's own image, but protected by name: protection wins over the task's rules too.
    d.add_image("sha256:task", ("node:24-acme1",), {"arceus.task": "ACME-1"}, created=old)
    result = w.cleanup.run("full", "manual")
    assert w.items(result) == {"otherteam/api:1, otherteam/api:latest": ("remove", "done")}
    assert set(d.imgs) == {"sha256:pg", "sha256:node", "sha256:py", "sha256:keep", "sha256:dev", "sha256:used",
                           "sha256:task"}
    assert result["freed_by_rule"] == {"unused_images": 400_000_000}
    assert w.unknown(result) == {"python:3.12", "stopped"}  # protected images are not even listed
    w.events.close()
    w.store.close()


def test_the_age_rules_change_nothing_in_report_mode_or_while_a_task_works(tmp_path):
    old = T0 - timedelta(days=30)
    w = World(tmp_path, mode="observe", unused_images_after="7d")  # observe: report, whatever cleanup.mode says
    w.docker.cache = [(old, 1_000)]
    w.docker.add_image("sha256:untagged", created=old)
    w.docker.add_image("sha256:tagged", ("python:3.12",), created=old)
    result = w.cleanup.run("full", "manual")
    assert w.items(result) == {"build cache": ("prune", "planned"), "untagged image untagged": ("remove", "planned"),
                               "python:3.12": ("remove", "planned")}
    assert w.docker.calls == [] and "freed_by_rule" not in result
    w.events.close()
    w.store.close()

    w = World(tmp_path / "act", unused_images_after="7d")
    w.task("ACME-3", "dispatched")
    w.docker.cache = [(old, 1_000)]
    w.docker.add_image("sha256:untagged", created=old)
    w.docker.add_image("sha256:tagged", ("python:3.12",), created=old)
    # A task works: an independent pass touches only what cannot be its own. A host-wide rule cannot promise that.
    result = w.cleanup.run("independent", "daily", w.store.task("ACME-3"))
    assert w.items(result) == {} and w.docker.calls == [] and "freed_by_rule" not in result
    w.events.close()
    w.store.close()


def test_a_second_look_keeps_an_image_tagged_labelled_or_protected_since_the_plan(tmp_path):
    w = World(tmp_path, unused_images_after="7d", protect={"images": ["acme/*"]})
    d, old = w.docker, T0 - timedelta(days=30)
    for iid in ("tagged-since", "labelled-since", "protected-since", "gone-since", "goes"):
        d.add_image(f"sha256:{iid}", created=old)
    d.add_image("sha256:tags-moved", ("otherteam/api:1",), created=old)
    w.cleanup = w.make()
    items, _ = w.cleanup.plan("full")
    d.imgs["sha256:tagged-since"]["RepoTags"] = ["mine:1"]
    d.imgs["sha256:labelled-since"]["Config"]["Labels"] = {"arceus.task": "ACME-9"}
    d.imgs["sha256:protected-since"]["RepoTags"] = ["acme/tool:1"]
    del d.imgs["sha256:gone-since"]
    d.imgs["sha256:tags-moved"]["RepoTags"] = ["otherteam/api:2"]
    w.cleanup.execute("r1", items, errors := [], deadline=w.mono() + 600)
    result = {i.ref.removeprefix("sha256:"): (i.result, i.detail.get("kept")) for i in items}
    assert result == {
        "tagged-since": ("kept", "it was tagged since"),
        "labelled-since": ("kept", "it is labelled for ACME-9 now"),
        "protected-since": ("kept", "it is protected now"),
        "gone-since": ("gone", None),
        "goes": ("done", None),
        "tags-moved": ("kept", "its tags changed"),
    }
    assert errors == []
    assert set(d.imgs) == {"sha256:tagged-since", "sha256:labelled-since", "sha256:protected-since",
                           "sha256:tags-moved"}
    w.events.close()
    w.store.close()


def test_what_a_rule_freed_is_only_what_docker_says(world, monkeypatch):
    """Never an estimate: a prune that failed says nothing; one Docker did not report is measured on its own
    count; a rule that removed nothing freed 0."""
    d, old = world.docker, T0 - timedelta(days=30)
    d.cache = [(old, 5_000)]
    d.fail.add("build-cache")
    d.add_image("sha256:untagged", created=old, size=9_000)
    result = world.cleanup.run("full", "manual")
    assert result["status"] == "partial" and any("builder prune" in e for e in result["errors"])
    assert result["freed_by_rule"] == {"build_cache": None, "dangling_images": 9_000}

    # Docker printed no "Total:": the build cache's own count before and after.
    d.fail.clear()
    d.says_total = False
    world.now += timedelta(hours=2)
    result = world.cleanup.run("full", "retry")
    assert result["freed_by_rule"] == {"build_cache": 5_000}

    # A rule whose every removal was refused: 0, not a guess.
    d.add_image("sha256:refused", created=old)
    monkeypatch.setattr(d, "remove_image", lambda iid: (_ for _ in ()).throw(
        DockerError("docker image rm: conflict: unable to delete (must be forced) - image is being used")))
    world.now += timedelta(hours=2)
    result = world.cleanup.run("full", "retry")
    assert world.items(result)["untagged image refused"] == ("remove", "kept")
    assert result["freed_by_rule"] == {"dangling_images": 0}


def test_last_pass_and_metrics_keep_what_each_rule_freed(tmp_path, capsys, monkeypatch):
    import yaml

    from arceus_runner import cli
    w = World(tmp_path)
    w.docker.cache = [(T0 - timedelta(days=10), 1_800_000_000)]
    w.docker.add_image("sha256:untagged", created=T0 - timedelta(days=30), size=0)
    w.cleanup.run("full", "manual")
    metrics = Metrics()
    metrics.update_cleanup(w.cleanup)
    assert metrics.registry.get_sample_value("runner_cleanup_rule_freed_bytes", {"rule": "build_cache"}) == 1.8e9
    assert metrics.registry.get_sample_value("runner_cleanup_rule_freed_bytes", {"rule": "dangling_images"}) == 0
    assert metrics.registry.get_sample_value("runner_cleanup_rule_freed_bytes", {"rule": "unused_images"}) is None
    w.events.close()
    w.store.close()
    # Read back from the database by another process, as `cleanup --last` does after a restart.
    raw = example_raw()
    raw.pop("slack")
    raw["health"].pop("devstack_check", None)
    raw["paths"] = {k: str(v) for k, v in w.cfg.paths.model_dump().items()}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    assert cli.main(["--config", str(tmp_path / "config.yaml"), "cleanup", "--last"]) == 0
    assert "Freed, as Docker counts it: build cache 1.8 GB; untagged images 0 B" in capsys.readouterr().out


def test_age_rules_are_in_days_and_can_be_switched_off(tmp_path):
    c = config(tmp_path).cleanup
    assert (c.build_cache_after, c.dangling_images_after, c.unused_images_after) == \
        (timedelta(days=7), timedelta(days=7), None)
    for key in ("build_cache_after", "dangling_images_after", "unused_images_after"):
        with pytest.raises(Exception, match="at least 1d"):
            config(tmp_path, **{key: "12h"})
    c = config(tmp_path, build_cache_after=None, dangling_images_after=None).cleanup
    assert c.build_cache_after is None and c.dangling_images_after is None


def test_the_build_cache_off_is_reported_and_untagged_images_off_are_counted(tmp_path):
    w = World(tmp_path, build_cache_after=None, dangling_images_after=None)
    w.docker.cache = [(T0 - timedelta(days=30), 3_179_000_000)]
    w.docker.add_image("sha256:untagged", created=T0 - timedelta(days=30))
    result = w.cleanup.run("full", "manual")
    why = {i["name"]: i["why"] for i in result["unknown"]}
    assert w.items(result) == {} and w.docker.calls == []
    assert why["build cache"].endswith("`build_cache_after` is off: `docker builder prune` by hand")
    assert why["1 untagged images"].endswith("`dangling_images_after` is off")
    w.events.close()
    w.store.close()


def test_docker_prunes_the_build_cache_by_hours_and_reads_what_it_reclaimed(tmp_path):
    """The real wrapper's command line: `until` in hours (Docker refuses "7d"), `--all` so cache an image shares
    goes too, never a prune of images; and the size Docker prints, in both forms it has printed it."""
    from arceus_runner.dockerops import Docker
    argv = tmp_path / "argv"
    fake = tmp_path / "docker"
    for out, want in (("ID\\tRECLAIMABLE\\nabc\\t1.2GB\\nTotal:\\t1.8GB", 1_800_000_000),
                      ("Total reclaimed space: 0B", 0), ("", None)):
        fake.write_text(f'#!/bin/sh\necho "$@" > {argv}\nprintf "{out}\\n"\n')
        fake.chmod(0o755)
        assert Docker(binary=str(fake)).prune_build_cache(timedelta(days=7)) == want
        assert argv.read_text().strip() == "builder prune --force --all --filter until=168h"


def _image_tar(path: Path, created: str, payload: bytes, tag: str | None = None) -> Path:
    """A `docker load` archive of a one-layer image made at `created`, untagged unless `tag` is given: an image
    as old as wanted without waiting, and without pulling anything."""
    import hashlib
    import io
    import json
    import tarfile

    def tar_bytes(files: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size, info.mtime = len(data), 0
                t.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    layer = tar_bytes({"payload": payload})
    config = json.dumps({"architecture": "amd64", "os": "linux", "created": created, "config": {},
                         "rootfs": {"type": "layers", "diff_ids": [f"sha256:{hashlib.sha256(layer).hexdigest()}"]},
                         "history": [{"created": created, "created_by": "test"}]}).encode()
    manifest = json.dumps([{"Config": "config.json", "RepoTags": [tag] if tag else None,
                            "Layers": ["layer.tar"]}]).encode()
    path.write_bytes(tar_bytes({"config.json": config, "layer.tar": layer, "manifest.json": manifest}))
    return path


@pytest.mark.skipif(not os.environ.get("ARCEUS_REAL_DOCKER"), reason="set ARCEUS_REAL_DOCKER=1 on a host with Docker")
def test_real_docker_age_rules_in_a_throwaway_daemon(tmp_path, monkeypatch):
    """The age rules prune host-wide, so they run against a Docker of their own (docker:dind), never the host's:
    an old untagged image goes; an old untagged image a stopped container uses, a fresh one and an old tagged one
    stay; build cache used today stays, and `until` really prunes by the last use."""
    import socket
    import time
    import uuid

    from arceus_runner.dockerops import Docker
    name = f"zz-dind-{uuid.uuid4().hex[:8]}"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    dind = "docker:29-dind"
    pulled = subprocess.run(["docker", "image", "inspect", dind], capture_output=True).returncode != 0
    started = subprocess.run(["docker", "run", "-d", "--rm", "--privileged", "--name", name, "-e",
                              "DOCKER_TLS_CERTDIR=", "-p", f"127.0.0.1:{port}:2375", dind],
                             capture_output=True, text=True)
    if started.returncode != 0:  # what Docker said: no daemon, no privileged containers, no registry
        pytest.fail(f"the throwaway Docker ({dind}) did not start: {started.stderr.strip()[-500:]}")
    monkeypatch.setenv("DOCKER_HOST", f"tcp://127.0.0.1:{port}")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)

    def run(*args: str) -> str:
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()

    def load(tar: Path) -> str:
        out = run("load", "-i", str(tar))
        return run("image", "inspect", "-f", "{{.Id}}", out.rsplit(" ", 1)[-1]) if "Loaded image:" in out \
            else out.rsplit(" ", 1)[-1]

    w = None
    try:
        for _ in range(60):
            if subprocess.run(["docker", "info"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("the throwaway Docker did not start")
        old = "2020-01-01T00:00:00Z"
        goes = load(_image_tar(tmp_path / "a.tar", old, os.urandom(300_000)))
        held = load(_image_tar(tmp_path / "b.tar", old, os.urandom(1000)))
        run("create", "--name", "stopped", held, "/payload")
        fresh = load(_image_tar(tmp_path / "c.tar", datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                                os.urandom(1000)))
        tagged = load(_image_tar(tmp_path / "d.tar", old, os.urandom(1000), tag="zz-base:1"))
        dangling = set(run("image", "ls", "-q", "--no-trunc", "--filter", "dangling=true").split())
        assert {goes, held, fresh} <= dangling and tagged not in dangling, (goes, held, fresh, dangling)
        ctx = tmp_path / "ctx"
        ctx.mkdir()
        (ctx / "Dockerfile").write_text("FROM scratch\nCOPY f /f\n")
        (ctx / "f").write_bytes(os.urandom(200_000))
        run("build", "-q", str(ctx))
        cache_before = run("system", "df", "--format", "{{if eq .Type \"Build Cache\"}}{{.Size}}{{end}}")
        assert cache_before.strip() not in ("", "0B"), cache_before

        w = World(tmp_path / "w")
        w.now = datetime.now(UTC)
        w.docker = Docker()
        w.cleanup = w.make()
        result = w.cleanup.run("full", "manual")
        assert result["status"] == "succeeded", result["errors"]
        left = {i for i in run("image", "ls", "-aq", "--no-trunc").split()}
        assert goes not in left and {held, fresh, tagged} <= left
        assert w.items(result)["build cache"] == ("prune", "done")
        assert result["freed_by_rule"]["build_cache"] == 0  # used today: kept by the 7 days
        assert result["freed_by_rule"]["dangling_images"] > 0
        assert "zz-base:1" in w.unknown(result)  # tagged, no task label: reported, unused_images_after is off

        time.sleep(2)
        freed = Docker().prune_build_cache(timedelta(seconds=1))
        assert freed and freed > 0
        assert run("system", "df", "--format", "{{if eq .Type \"Build Cache\"}}{{.Size}}{{end}}").strip() == "0B"
    finally:
        if w:
            w.events.close()
            w.store.close()
        # `-v`: the daemon's own /var/lib/docker is an anonymous volume of the dind image. The image goes too
        # when this test pulled it: the host is left as it was.
        host = {k: v for k, v in os.environ.items() if k != "DOCKER_HOST"}
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, env=host)
        if pulled:
            subprocess.run(["docker", "image", "rm", dind], capture_output=True, env=host)


@pytest.mark.parametrize("after", [{}, {"Images": {"Type": "Images"}}, {"Images": {"Type": "Images", "Size": "unreadable"}}],
                         ids=["no row", "no size", "not a size"])
def test_a_measurement_docker_did_not_give_is_unknown_never_freed(tmp_path, capsys, after):
    """Docker's count after the rule is missing or unreadable: what the rule freed is not known. It stays null in
    the pass's result, in the stored report and `cleanup --last`, and the metric says nothing; a real 0B is 0."""
    import json

    import yaml

    from arceus_runner import cli
    w = World(tmp_path)
    d = w.docker
    d.add_image("sha256:old", created=T0 - timedelta(days=30), size=1_000_000)
    remove = d.remove_image

    def remove_then_break(iid: str) -> None:
        remove(iid)
        d.df_broken = after
    d.remove_image = remove_then_break
    result = w.cleanup.run("full", "manual")
    assert w.items(result) == {"untagged image old": ("remove", "done")}
    assert result["freed_by_rule"] == {"dangling_images": None}
    stored = json.loads(w.store.cleanup_runs(1)[0]["summary"])
    assert stored["freed_by_rule"] == {"dangling_images": None}
    metrics = Metrics()
    metrics.update_cleanup(w.cleanup)
    assert metrics.registry.get_sample_value("runner_cleanup_rule_freed_bytes", {"rule": "dangling_images"}) is None
    w.events.close()
    w.store.close()

    raw = example_raw()
    raw.pop("slack")
    raw["health"].pop("devstack_check", None)
    raw["paths"] = {k: str(v) for k, v in w.cfg.paths.model_dump().items()}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    args = ["--config", str(tmp_path / "config.yaml"), "cleanup", "--last"]
    assert cli.main(args) == 0
    assert "Freed, as Docker counts it: untagged images unknown (Docker did not say)" in capsys.readouterr().out
    assert cli.main(args + ["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["freed_by_rule"] == {"dangling_images": None}


def test_docker_sizes_read_as_sizes_or_not_at_all():
    from arceus_runner.dockerops import read_size, size_bytes
    assert read_size("0B") == 0 and read_size("1.8GB") == 1_800_000_000 and read_size("204.8kB") == 204_800
    for bad in (None, "", "unreadable", "1.2.3GB", "GB"):
        assert read_size(bad) is None and size_bytes(bad) == 0


@pytest.mark.parametrize("rule,tags", [("dangling_images", ()), ("unused_images", ("otherteam/api:1",))])
@pytest.mark.parametrize("error", ["docker image rm: no answer in 300s, out of time", "docker image rm: daemon error"])
def test_an_image_removal_cut_or_failed_after_it_began_freed_an_unknown_amount(tmp_path, capsys, rule, tags, error):
    """Docker may have removed the image before the command failed or was cut: the rule says nothing (null) in the
    result, the stored report, `cleanup --last` and the metric; never 0. One that never began frees 0."""
    import json

    import yaml

    from arceus_runner import cli
    w = World(tmp_path, unused_images_after="7d")
    d = w.docker
    d.add_image("sha256:old", tags, created=T0 - timedelta(days=30))
    remove = d.remove_image

    def remove_then_fail(iid: str) -> None:
        remove(iid)  # it went, but the client never heard so
        raise DockerError(error)
    d.remove_image = remove_then_fail
    result = w.cleanup.run("full", "manual")
    assert [i["result"] for i in result["planned"]] == ["skipped" if "out of time" in error else "failed"]
    assert result["freed_by_rule"] == {rule: None}
    assert json.loads(w.store.cleanup_runs(1)[0]["summary"])["freed_by_rule"] == {rule: None}
    metrics = Metrics()
    metrics.update_cleanup(w.cleanup)
    assert metrics.registry.get_sample_value("runner_cleanup_rule_freed_bytes", {"rule": rule}) is None
    w.events.close()
    w.store.close()
    raw = example_raw()
    raw.pop("slack")
    raw["health"].pop("devstack_check", None)
    raw["paths"] = {k: str(v) for k, v in w.cfg.paths.model_dump().items()}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    args = ["--config", str(tmp_path / "config.yaml"), "cleanup", "--last"]
    assert cli.main(args) == 0
    assert "unknown (Docker did not say)" in capsys.readouterr().out
    assert cli.main(args + ["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["freed_by_rule"] == {rule: None}


def test_an_image_removal_that_never_began_frees_0(world):
    """The pass's time was over before the command: nothing ran, so nothing went."""
    world.docker.add_image("sha256:old", created=T0 - timedelta(days=30))
    items, _ = world.cleanup.plan("full")
    world.cleanup.execute("r1", items, errors := [], deadline=world.mono() - 1)
    assert [i.result for i in items] == ["skipped"] and "error" not in items[0].detail
    assert world.cleanup.freed_by_rule == {"dangling_images": 0} and errors == ["out of time"]
