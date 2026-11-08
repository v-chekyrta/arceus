"""Operations from the console: the runner's drain, the host's executor (requests, its fixed list, links in the data
folder, one at a time, cancel, lost operations) and the pages."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from arceus_runner.console import hostops
from arceus_runner.console.hostops import Host, Ledger

ID = "a" * 32
ID2 = "b" * 32


# -- the runner's side: the drain ------------------------------------------------------------------------------------

def test_while_drained_nothing_reaches_the_engine_and_the_runner_says_it_has_seen_it(cfg, secrets):
    from arceus_runner.engine import Engine
    from arceus_runner.screening import trusted
    from tests.test_loop import World
    world = World(cfg, secrets)
    runner = world.runner
    engine = Engine(cfg)
    runner.engine = engine
    engine.held = runner.engine_held
    calls = []
    engine._run = lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "{}", "")
    world.store.set_state("drain", {"token": "op1", "by": "Olga"})
    reply = engine.dispatch(task_id="ACME-1", message_id="m", author="a", branch="b", base="main",
                            repo=Path("/w"), text="x", clearance=trusted("x"))
    assert reply.rc == 6 and "restarted from the console (Olga)" in reply.why() and calls == []
    reply = engine.send(task_id="ACME-1", message_id="m2", source="other", author="a", repo=Path("/w"), text="y",
                        clearance=trusted("y"))
    assert reply.rc == 6 and calls == []
    assert runner.can_take() is False
    runner.acknowledge_drain()
    assert world.store.state("drain_ack")[0]["token"] == "op1"
    world.store.set_state("drain", None)
    runner.acknowledge_drain()
    assert world.store.state("drain_ack")[0]["token"] is None
    assert engine.held() is None


# -- the host's executor ---------------------------------------------------------------------------------------------

class FakeSystem:
    """systemctl, systemd-run, journalctl and the run user's commands, answered from memory."""

    def __init__(self, host_data: Path):
        self.calls: list[list[str]] = []
        self.units: dict[str, dict] = {"runner": {"ActiveState": "active", "ActiveEnterTimestamp": "t0"},
                                       "bulava-engine": {"ActiveState": "active", "ActiveEnterTimestamp": "e0"}}
        self.drain: dict | None = None
        self.ack: dict | None = None
        self.config = {"loaded_at": "2000-01-01T00:00:00+00:00"}
        self.engine_running: list[list[str]] = [[]]
        self.ack_on_set = True

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        out, rc = "", 0
        if args[0] == "systemctl" and args[1] == "show":
            u = self.units.get(args[2], {"ActiveState": "inactive", "LoadState": "not-found"})
            out = "\n".join(f"{k}={v}" for k, v in {"LoadState": "loaded", **u}.items())
        elif args[0] == "systemctl" and args[1] == "restart":
            name = args[2]
            self.units[name] = {"ActiveState": "active", "ActiveEnterTimestamp": f"{name}-restarted"}
            if name == "runner":
                self.config = {"loaded_at": datetime.now(UTC).isoformat(timespec="seconds")}
        elif args[0] == "systemd-run":
            unit = next(a.split("=", 1)[1] for a in args if a.startswith("--unit="))
            self.units[unit] = {"ActiveState": "active"}
        elif "drain" in args:
            what = args[args.index("drain") + 1]
            if what == "status":
                out = json.dumps({"drain": self.drain, "ack": self.ack, "config": self.config})
            elif what == "set":
                self.drain = {"token": args[args.index("--token") + 1]}
                if self.ack_on_set:
                    self.ack = {"token": self.drain["token"]}
            elif what == "clear":
                self.drain = None
        elif args[-3:] == ["task", "list", "--json"]:
            now = self.engine_running.pop(0) if len(self.engine_running) > 1 else self.engine_running[0]
            out = json.dumps([{"task_id": t, "state": "running"} for t in now])
        return subprocess.CompletedProcess(args, rc, out, "")


@pytest.fixture
def host(tmp_path):
    data = tmp_path / "data"
    (data / "console" / "ops" / "requests").mkdir(parents=True)
    system = FakeSystem(data)
    h = Host(data=data, setup=tmp_path / "setup.sh", ledger=tmp_path / "ledger", exe="/opt/arceus-runner/venv/bin/a",
             run=system, sleep=lambda s: None)
    h.system = system
    return h


def request(host: Host, op_id: str, body: dict) -> Path:
    path = host.ops_dir / "requests" / f"{op_id}.json"
    path.write_text(json.dumps({"id": op_id, **body}))
    return path


def test_a_request_is_recorded_started_as_a_unit_of_its_own_and_removed(host):
    request(host, ID, {"op": "restart-runner", "requested_by": "Olga <o@x>"})
    done = hostops.dispatch(host)
    assert done == [f"{ID}: restart-runner started"]
    rec = Ledger(host).get(ID)
    assert rec["state"] == "accepted" and rec["requested_by"] == "Olga <o@x>"
    run = [c for c in host.system.calls if c[0] == "systemd-run"][0]
    assert f"--unit=arceus-op-{ID}" in run and run[-2:] == ["run", ID] and "--collect" in run
    assert list((host.ops_dir / "requests").iterdir()) == []
    mirror = json.loads((host.ops_dir / "runs" / f"{ID}.json").read_text())
    assert mirror["state"] == "accepted"
    # The same request again (a retried write) is not a second operation.
    request(host, ID, {"op": "restart-runner", "requested_by": "Olga"})
    assert hostops.dispatch(host) == [f"{ID}: seen before, ignored"]


@pytest.mark.parametrize("name, body", [
    ("not-a-request.json", {"op": "setup"}),
    (f"{ID}.json", {"op": "rm -rf /", "id": ID}),
    (f"{ID}.json", {"op": "setup", "id": ID2}),
    (f"{ID}.json", "not an object"),
])
def test_anything_but_a_listed_operation_runs_nothing(host, name, body):
    (host.ops_dir / "requests" / name).write_text(json.dumps(body))
    hostops.dispatch(host)
    assert not [c for c in host.system.calls if c[0] == "systemd-run"]
    assert list((host.ops_dir / "requests").iterdir()) == []


def test_a_link_in_the_requests_folder_is_not_followed(host, tmp_path):
    secret = tmp_path / "elsewhere.json"
    secret.write_text(json.dumps({"id": ID, "op": "setup"}))
    (host.ops_dir / "requests" / f"{ID}.json").symlink_to(secret)
    assert "unreadable" in hostops.dispatch(host)[0]
    assert not [c for c in host.system.calls if c[0] == "systemd-run"]
    assert secret.exists()


def test_a_link_in_place_of_a_folder_makes_root_write_nothing_there(host, tmp_path):
    target = tmp_path / "victim"
    target.mkdir()
    (host.ops_dir / "runs").symlink_to(target)  # planted by whoever can write the data folder
    request(host, ID, {"op": "setup-check", "requested_by": "x"})
    hostops.dispatch(host)
    assert list(target.iterdir()) == []  # the ledger has it; the console's copy is not written through the link
    assert Ledger(host).get(ID)["state"] == "accepted"
    os.rmdir(host.ops_dir / "requests")
    (host.ops_dir / "requests").symlink_to(target)
    (target / f"{ID2}.json").write_text(json.dumps({"id": ID2, "op": "setup"}))
    assert "refused" in hostops.dispatch(host)[0]
    assert (target / f"{ID2}.json").exists()


def test_a_folder_made_by_someone_else_at_the_same_moment_is_simply_opened(host, monkeypatch):
    real_mkdir = os.mkdir

    def racing(name, mode=0o777, *, dir_fd=None):
        real_mkdir(name, mode, dir_fd=dir_fd)  # the console made it first
        raise FileExistsError(17, "File exists", name)
    monkeypatch.setattr(hostops.os, "mkdir", racing)
    d = hostops.SafeDir(host.data / "console" / "ops" / "runs", owner=host.owner)
    d.write("x.json", b"{}")
    d.close()
    assert (host.ops_dir / "runs" / "x.json").read_bytes() == b"{}"


def test_one_at_a_time_a_second_is_refused_with_the_reason(host):
    request(host, ID, {"op": "restart-engine", "requested_by": "Olga"})
    hostops.dispatch(host)
    request(host, ID2, {"op": "setup", "requested_by": "Ivan"})
    hostops.dispatch(host)
    rec = Ledger(host).get(ID2)
    assert rec["state"] == "refused" and "Restart the engine when it is idle" in rec["result"] and "Olga" in rec["result"]


def test_an_operation_never_runs_twice(host):
    request(host, ID, {"op": "restart-runner", "requested_by": "x"})
    hostops.dispatch(host)
    assert hostops.run(host, ID) == 0
    assert Ledger(host).get(ID)["state"] == "succeeded"
    assert hostops.run(host, ID) == 1  # again: refused, no second restart
    assert sum(1 for c in host.system.calls if c[:2] == ["systemctl", "restart"]) == 1


def test_restarting_the_runner_says_how_it_came_back(host):
    request(host, ID, {"op": "restart-runner", "requested_by": "x"})
    hostops.dispatch(host)
    hostops.run(host, ID)
    rec = Ledger(host).get(ID)
    assert rec["state"] == "succeeded" and "runner.service active since runner-restarted" in rec["health"]
    assert "loaded config.yaml" in rec["health"]
    log = (host.ops_dir / "runs" / f"{ID}.log").read_text()
    assert "A task in work keeps going" in log


def test_a_runner_that_does_not_come_back_is_a_failure_with_its_journal(host):
    request(host, ID, {"op": "restart-runner", "requested_by": "x"})
    hostops.dispatch(host)
    real = host.system.__call__

    def failing(args, **kw):
        r = real(args, **kw)
        if args[:2] == ["systemctl", "restart"]:
            host.system.units["runner"] = {"ActiveState": "failed", "SubState": "failed", "ExecMainStatus": "2"}
        if args[0] == "journalctl":
            return subprocess.CompletedProcess(args, 0, "config: mode: Input should be 'observe' or 'live'\n", "")
        return r
    host.run = failing
    hostops.run(host, ID)
    rec = Ledger(host).get(ID)
    assert rec["state"] == "failed" and "runner is failed" in rec["result"] and "exit status 2" in rec["health"]
    assert "Input should be" in (host.ops_dir / "runs" / f"{ID}.log").read_text()


def test_the_engine_restarts_only_once_its_tasks_end_and_work_is_let_go_again(host):
    host.system.engine_running = [["ACME-12"], ["ACME-12"], []]
    request(host, ID, {"op": "restart-engine", "requested_by": "Olga"})
    hostops.dispatch(host)
    hostops.run(host, ID)
    rec = Ledger(host).get(ID)
    assert rec["state"] == "succeeded", rec
    calls = [c for c in host.system.calls if "drain" in c or c[:2] == ["systemctl", "restart"]]
    order = [("set" if "set" in c else "clear" if "clear" in c else "restart") for c in calls
             if "status" not in c]
    assert order == ["set", "restart", "clear"]
    log = (host.ops_dir / "runs" / f"{ID}.log").read_text()
    assert "The runner acknowledged" in log and "Waiting for the engine's running tasks to end: ACME-12" in log
    assert host.system.drain is None


def test_an_engine_restart_cancelled_while_it_waits_restarts_nothing_and_lets_work_go(host):
    host.system.engine_running = [["ACME-12"]]
    request(host, ID, {"op": "restart-engine", "requested_by": "Olga"})
    hostops.dispatch(host)
    ticks = {"n": 0}

    def sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 2:
            request(host, ID2, {"op": "cancel", "target": ID, "requested_by": "Ivan"})
            hostops.dispatch(host)
    host.sleep = sleep
    hostops.run(host, ID)
    rec = Ledger(host).get(ID)
    assert rec["state"] == "cancelled" and "Ivan" in rec["result"]
    assert not [c for c in host.system.calls if c[:3] == ["systemctl", "restart", "bulava-engine"]]
    assert host.system.drain is None


def test_a_runner_that_never_acknowledges_fails_the_restart_and_lifts_the_drain(host, monkeypatch):
    host.system.ack_on_set = False
    monkeypatch.setattr(hostops, "ACK_WAIT", -1)
    request(host, ID, {"op": "restart-engine", "requested_by": "x"})
    hostops.dispatch(host)
    hostops.run(host, ID)
    rec = Ledger(host).get(ID)
    assert rec["state"] == "failed" and "did not acknowledge" in rec["result"]
    assert host.system.drain is None


def test_a_lost_operation_is_interrupted_never_succeeded_and_its_drain_lifted(host):
    request(host, ID, {"op": "restart-engine", "requested_by": "x"})
    hostops.dispatch(host)
    Ledger(host).update(ID, state="waiting")
    host.system.drain = {"token": ID}
    del host.system.units[f"arceus-op-{ID}"]  # the machine rebooted: the unit is gone
    hostops.tick(host)
    rec = Ledger(host).get(ID)
    assert rec["state"] == "interrupted" and "Nothing is retried" in rec["result"]
    assert host.system.drain is None
    state = json.loads((host.ops_dir / "host.json").read_text())
    assert state["services"]["runner"]["active"] == "active"
    assert hostops.run(host, ID) == 1  # and it is not run after all


class FakeProc:
    def __init__(self, lines, rc):
        self.stdout, self.rc = iter(lines), rc

    def wait(self):
        return self.rc


@pytest.mark.parametrize("rc, state, words", [(0, "succeeded", "every phase is done"),
                                              (1, "succeeded", "some phases are not done"),
                                              (3, "failed", "exit 3")])
def test_the_setup_check_runs_setup_as_its_user_would_and_says_what_it_found(host, rc, state, words):
    seen = {}

    def popen(args, **kw):
        seen["args"], seen["env"] = args, kw["env"]
        return FakeProc(["[ ok ] packages\n", "[todo] console\n"], rc)
    host.popen = popen
    request(host, ID, {"op": "setup-check", "requested_by": "x"})
    hostops.dispatch(host)
    hostops.run(host, ID)
    rec = Ledger(host).get(ID)
    assert rec["state"] == state and words in rec["result"]
    assert seen["args"][-1] == "--check" and seen["env"]["SUDO_USER"]
    assert "[todo] console" in (host.ops_dir / "runs" / f"{ID}.log").read_text()


# -- the pages -------------------------------------------------------------------------------------------------------

pytest.importorskip("starlette")


@pytest.fixture
def pages(tmp_path):
    from arceus_runner.console.web import Settings
    from arceus_runner.db import ReviewStore
    from tests.conftest import example_raw
    from tests.test_console_auth import make_admin
    raw = example_raw()
    raw["paths"] = {"db": str(tmp_path / "runner.db"), "secrets": str(tmp_path / "secrets.env")}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    s = Settings(config_path=config, db_path=tmp_path / "console" / "console.db", ops_dir=tmp_path / "console" / "ops")
    make_admin(s)
    ReviewStore(tmp_path / "runner.db").close()
    (s.ops_dir / "runs").mkdir(parents=True)

    return s


def fresh(s, age: float = 0) -> None:
    """The host's executor has just written the services' state (age seconds ago)."""
    at = (datetime.now(UTC) - timedelta(seconds=age)).isoformat(timespec="seconds")
    (s.ops_dir / "host.json").write_text(json.dumps({"at": at, "services": {
        "runner": {"active": "active", "since": "Sun 2026-11-09 07:00:00 UTC"},
        "bulava-engine": {"active": "active", "since": "Sun 2026-11-09 06:00:00 UTC"}}}))


def test_without_the_hosts_executor_the_page_says_so_and_starts_nothing(pages):
    from tests.test_console_auth import csrf_of, signed_in
    admin = signed_in(pages, "owner@example.com")
    page = admin.get("/operations/").text
    assert "Operations are not available" in page and 'href="/operations/new/' not in page
    r = admin.post("/operations/new/restart-runner", data={"csrf": csrf_of(page)})
    assert r.status_code == 409 and "Not started" in r.text
    assert not list((pages.ops_dir / "requests").glob("*.json")) if (pages.ops_dir / "requests").exists() else True
    fresh(pages, age=600)
    assert "last answered" in admin.get("/operations/").text


def test_an_operator_restarts_the_runner_and_follows_it_live(pages):
    from tests.test_console_auth import csrf_of
    from tests.test_console_qa import person
    fresh(pages)
    op = person(pages, "op@example.com", {"operator"})
    page = op.get("/operations/").text
    assert "Active" in page and 'href="/operations/new/restart-runner"' in page
    assert 'href="/operations/new/setup"' not in page  # admins only
    confirm = op.get("/operations/new/restart-runner").text
    assert "A task in work keeps going" in confirm and "runner" in confirm
    r = op.post("/operations/new/restart-runner", data={"csrf": csrf_of(confirm)})
    assert r.status_code == 303
    op_id = r.headers["location"].split("/")[2]
    req = json.loads((pages.ops_dir / "requests" / f"{op_id}.json").read_text())
    assert req["op"] == "restart-runner" and "op@example.com" in req["requested_by"]
    assert "Waiting for the host" in op.get(f"/operations/{op_id}/").text
    # The host took it and is running it: the page follows its record and output.
    os.unlink(pages.ops_dir / "requests" / f"{op_id}.json")
    (pages.ops_dir / "runs" / f"{op_id}.json").write_text(json.dumps(
        {"id": op_id, "op": "restart-runner", "title": "Restart the runner", "state": "running",
         "requested_by": req["requested_by"], "requested_at": req["requested_at"]}))
    (pages.ops_dir / "runs" / f"{op_id}.log").write_text("[07:00:01] runner.service is active\n")
    live = op.get(f"/operations/{op_id}/live").text
    assert "Running" in live and "runner.service is active" in live and "data-final" not in live
    (pages.ops_dir / "runs" / f"{op_id}.json").write_text(json.dumps(
        {"id": op_id, "op": "restart-runner", "title": "Restart the runner", "state": "succeeded",
         "requested_by": req["requested_by"], "requested_at": req["requested_at"], "result": "the runner restarted",
         "health": "runner.service active since t1"}))
    live = op.get(f"/operations/{op_id}/live").text
    assert "Done" in live and "runner.service active since t1" in live and "data-final" in live


def test_who_may_start_what(pages):
    from tests.test_console_auth import csrf_of, signed_in
    from tests.test_console_qa import person
    fresh(pages)
    op = person(pages, "op@example.com", {"operator"})
    token = csrf_of(op.get("/operations/").text)
    assert op.post("/operations/new/setup", data={"csrf": token}).status_code == 403
    assert op.get("/operations/new/setup-check").status_code == 200
    for roles in (set(), {"qa"}, {"architect"}):
        c = person(pages, f"{'-'.join(sorted(roles)) or 'n'}@example.com", roles)
        assert c.get("/operations/").status_code == 403
        assert c.post("/operations/new/restart-runner", data={"csrf": token}).status_code == 403
    admin = signed_in(pages, "owner@example.com")
    assert admin.post("/operations/new/setup", data={"csrf": csrf_of(admin.get("/operations/").text)}).status_code == 303


def test_a_second_operation_while_one_runs_is_refused_on_the_page(pages):
    from tests.test_console_auth import csrf_of
    from tests.test_console_qa import person
    fresh(pages)
    (pages.ops_dir / "runs" / f"{ID}.json").write_text(json.dumps(
        {"id": ID, "op": "restart-engine", "title": "Restart the engine when it is idle", "state": "waiting",
         "waiting_for": "ACME-12", "requested_by": "Olga", "requested_at": "2026-11-09T07:00:00+00:00"}))
    op = person(pages, "op@example.com", {"operator"})
    page = op.get("/operations/new/restart-runner").text
    assert "is under way" in page and " disabled" in page
    r = op.post("/operations/new/restart-runner", data={"csrf": csrf_of(page)})
    assert r.status_code == 409 and "One at a time" in r.text
    one = op.get(f"/operations/{ID}/").text
    assert "Waiting: ACME-12" in one and "Cancel" in one
    r = op.post(f"/operations/{ID}/cancel", data={"csrf": csrf_of(one)})
    assert r.status_code == 303
    cancel = [json.loads(p.read_text()) for p in (pages.ops_dir / "requests").glob("*.json")]
    assert [(c["op"], c["target"]) for c in cancel] == [("cancel", ID)]
