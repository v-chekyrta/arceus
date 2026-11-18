"""The host's side of the console's operations: what needs root on the machine, done from a fixed list.

The console runs in a container without sudo; it asks by writing a request into the data folder. On the host:

- `arceus-ops.path` (systemd) watches `<data>/console/ops/requests` and starts `arceus-ops.service`, which runs
  `dispatch` as root: each request is read without following links, checked against the fixed list (OPS), recorded
  in the host's own ledger (`/var/lib/arceus/ops`, root's), removed, and started as a transient systemd unit
  `arceus-op-<id>` (`run`). The unit outlives the console: setup can rebuild the console's container while it runs.
- One operation at a time: a request while one is under way is recorded as refused, with the reason.
- `arceus-ops-tick.timer` runs `tick` every few seconds: an operation whose unit is gone without a result (a reboot,
  a killed process) is marked interrupted, never succeeded and never run again; a drain left behind by one is
  lifted; and the services' state is written for the console's page (host.json).
- What the console reads, it reads from `<data>/console/ops/runs` (a copy of the ledger and each operation's
  output), written by root without following any link: the data folder is writable by the console's user, so a
  link planted there must never make root write elsewhere.

The assistant's keys (`assistant-key`) are the one request that carries a secret. It is taken at once, inside
`dispatch`, never by a transient unit: the value goes from the request straight into assistant.env (600, the run
user's, written without following links) and the request file is removed. The ledger, the console's copy of it, the
log and what `dispatch` prints name the key and who set it, never its value. `assistant-keys.json` in the console's
folder says which keys are set and since when.

Restarting the engine ends its tasks' sessions, so `restart-engine` first asks the runner to start nothing new in
the engine (the drain, runner_state in runner.db; the runner acknowledges it on its next pass), waits for the
engine's running tasks to end, restarts it, and lifts the drain however it ends.
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# operation -> (what the page calls it, the services it touches, who may ask: the console checks the role)
OPS = {
    "restart-runner": ("Restart the runner", ["runner"], "ops.restart"),
    "restart-engine": ("Restart the engine when it is idle", ["bulava-engine"], "ops.restart"),
    "setup-check": ("Run the setup check", [], "ops.check"),
    "setup": ("Run setup", ["runner", "bulava-engine", "the console", "observability"], "ops.setup"),
    "restart-assistant": ("Restart the assistant", ["assistant"], "ops.restart"),
}
# The assistant's keys (assistant/assistant/secrets.py): name -> what a value starts with. Set from the console's
# Assistant page by admins (settings.edit), never read back.
ASSISTANT_KEYS = {"ANTHROPIC_API_KEY": "sk-ant-", "OPENAI_API_KEY": "sk-", "SLACK_APP_TOKEN": "xapp-",
                  "SLACK_BOT_TOKEN": "xoxb-", "YOUTRACK_TOKEN": "perm"}
KEY_VALUE = re.compile(r"^[\x21-\x7e]{12,400}$")   # one printable word: no spaces, quotes or line breaks
SLACK_KEYS = ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN")
ACTIVE = ("accepted", "running", "waiting")
FINAL = ("succeeded", "failed", "cancelled", "interrupted", "refused")
_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_REQUEST = 4096
SERVICES = ("runner", "bulava-engine", "assistant")
ACK_WAIT = 600          # seconds the runner has to acknowledge a drain while it is active
RESTART_WAIT = 120      # seconds a restarted service has to be active again
POLL = 5


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Unsafe(Exception):
    """A path in the data folder that is not what it should be (a link, not a folder): nothing is done there."""


class SafeDir:
    """A folder under the data folder, opened one component at a time without following links, and the files in it
    handled only relative to it. Made when missing, owned by the run user (the console writes requests there)."""

    def __init__(self, path: Path, owner: tuple[int, int] | None = None, create: bool = True):
        if not path.is_absolute():
            raise Unsafe(f"{path} is not absolute")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                try:
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                        if owner:
                            os.chown(part, *owner, dir_fd=fd, follow_symlinks=False)
                    except FileExistsError:
                        pass  # made meanwhile (the console, or another pass): opened as it is, links refused
                    try:
                        nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    except OSError as e:
                        raise Unsafe(f"{path}: {part} is not a folder ({e.strerror})") from None
                except OSError as e:
                    raise Unsafe(f"{path}: {part} is not a folder ({e.strerror})") from None
                os.close(fd)
                fd = nxt
        except BaseException:
            os.close(fd)
            raise
        self.fd, self.path, self.owner = fd, path, owner

    def close(self) -> None:
        os.close(self.fd)

    def names(self) -> list[str]:
        return os.listdir(self.fd)

    def read(self, name: str, limit: int) -> bytes:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
                raise Unsafe(f"{name} is not a plain file of a sane size")
            return os.read(fd, limit + 1)
        finally:
            os.close(fd)

    def unlink(self, name: str) -> None:
        try:
            st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                return
            os.unlink(name, dir_fd=self.fd)
        except FileNotFoundError:
            pass

    def write(self, name: str, data: bytes, mode: int = 0o644) -> None:
        tmp = f".{name}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=self.fd)
        try:
            os.fchmod(fd, mode)
            os.write(fd, data)
            os.fsync(fd)
            if self.owner:
                os.fchown(fd, *self.owner)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def append(self, name: str, data: bytes) -> None:
        fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644, dir_fd=self.fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Unsafe(f"{name} is not a plain file")
            os.write(fd, data)
            if self.owner:
                os.fchown(fd, *self.owner)
        finally:
            os.close(fd)


@dataclass
class Host:
    """What the operations need of the machine; the tests give stand-ins."""
    data: Path                      # ~/arceus/data
    setup: Path                     # ~/arceus/setup/setup.sh
    runner_unit: str = "runner"     # this data folder's runner and engine (another project's on a shared machine)
    engine_unit: str = "bulava-engine"
    ledger: Path = Path("/var/lib/arceus/ops")
    # The nginx.conf setup wrote for the console (its console phase): whether it routes /assistant/, and where.
    console_nginx: Path = Path("/opt/arceus-console/nginx.conf")
    exe: str = sys.argv[0]          # this program, for the transient unit
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    popen: Callable[..., subprocess.Popen] = subprocess.Popen
    sleep: Callable[[float], None] = time.sleep

    @property
    def owner(self) -> tuple[int, int]:
        st = os.stat(self.data)
        return st.st_uid, st.st_gid

    @property
    def run_user(self) -> pwd.struct_passwd:
        return pwd.getpwuid(self.owner[0])

    @property
    def ops_dir(self) -> Path:
        return self.data / "console" / "ops"

    def unit(self, name: str) -> str:
        """The systemd unit of a service the page names ("runner", "bulava-engine", "assistant") for this data
        folder. The assistant's is setup's template unit, one per run user."""
        if name == "assistant":
            return f"arceus-assistant@{self.run_user.pw_name}"
        return {"runner": self.runner_unit, "bulava-engine": self.engine_unit}[name]

    @property
    def setup_user(self) -> pwd.struct_passwd:
        """Who runs setup.sh: the owner of its Arceus folder, which may not be this data folder's owner."""
        try:
            return pwd.getpwuid(os.stat(self.setup).st_uid)
        except FileNotFoundError:
            return self.run_user  # no setup.sh to run: the operation fails on its own, as the run user

    def args(self) -> list[str]:
        return ["--data", str(self.data), "--setup", str(self.setup), "--ledger", str(self.ledger),
                "--runner-unit", self.runner_unit, "--engine-unit", self.engine_unit]

    def as_user(self, args: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
        """A command as the run user, with their home: the runner's database and the engine are theirs."""
        u = self.run_user
        env = {"HOME": u.pw_dir, "USER": u.pw_name, "PATH": f"/usr/local/bin:{u.pw_dir}/.local/bin:/usr/bin:/bin",
               "LANG": "C.UTF-8", "ARCEUS_RUNNER_CONFIG": str(self.data / "config.yaml")}
        return self.run(args, user=u.pw_uid, group=u.pw_gid, extra_groups=[], env=env, capture_output=True,
                        text=True, timeout=timeout, cwd=u.pw_dir)

    def root(self, args: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
        return self.run(args, capture_output=True, text=True, timeout=timeout)


# -- the ledger ------------------------------------------------------------------------------------------------------

class Ledger:
    """The host's record of every operation (root's), and its copy in the data folder for the console."""

    def __init__(self, host: Host):
        self.host = host
        host.ledger.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(host.ledger, 0o700)

    def get(self, op_id: str) -> dict | None:
        try:
            return json.loads((self.host.ledger / f"{op_id}.json").read_text())
        except (FileNotFoundError, ValueError):
            return None

    def all(self) -> list[dict]:
        out = []
        for p in self.host.ledger.glob("*.json"):
            try:
                out.append(json.loads(p.read_text()))
            except ValueError:
                continue
        return sorted(out, key=lambda r: r.get("requested_at") or "")

    def put(self, rec: dict) -> dict:
        path = self.host.ledger / f"{rec['id']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
        os.replace(tmp, path)
        self.mirror(rec)
        return rec

    def update(self, op_id: str, **fields) -> dict:
        rec = self.get(op_id) or {"id": op_id}
        rec.update(fields)
        return self.put(rec)

    def log(self, op_id: str, line: str) -> None:
        text = f"[{datetime.now(UTC):%H:%M:%S}] {line.rstrip()}\n" if line.strip() else "\n"
        with open(self.host.ledger / f"{op_id}.log", "a", encoding="utf-8") as f:
            f.write(text)
        self._mirror_dir(lambda d: d.append(f"{op_id}.log", text.encode()))

    def mirror(self, rec: dict) -> None:
        self._mirror_dir(lambda d: d.write(f"{rec['id']}.json", json.dumps(rec, ensure_ascii=False).encode()))

    def _mirror_dir(self, act: Callable[[SafeDir], None]) -> None:
        try:
            d = SafeDir(self.host.ops_dir / "runs", owner=self.host.owner)
        except (Unsafe, OSError):
            return  # the console's copy only: the ledger has it
        try:
            act(d)
        except (Unsafe, OSError):
            pass
        finally:
            d.close()


# -- dispatch: requests to transient units ---------------------------------------------------------------------------

def dispatch(host: Host) -> list[str]:
    """Take every request the console left, record it, start it or refuse it. Returns what was done, for the log."""
    done = []
    ledger = Ledger(host)  # makes the ledger's folder, root's
    lock_fd = os.open("/run/arceus-ops.lock" if os.geteuid() == 0 else str(host.ledger / ".lock"),
                      os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        try:
            requests = SafeDir(host.ops_dir / "requests", owner=host.owner)
        except Unsafe as e:
            return [f"requests folder refused: {e}"]
        try:
            for name in sorted(requests.names()):
                try:
                    done.append(_one(host, ledger, requests, name))
                except Exception as e:  # a request this code did not foresee: dropped, the next ones still taken
                    done.append(f"{name}: dropped ({type(e).__name__}: {e})")
                finally:
                    requests.unlink(name)  # whatever it was: the folder empties, so the path unit rests
        finally:
            requests.close()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return done


def _one(host: Host, ledger: Ledger, requests: SafeDir, name: str) -> str:
    m = re.fullmatch(r"([0-9a-f]{32})\.json", name)
    if not m:
        return f"{name}: not a request, removed"
    op_id = m.group(1)
    try:
        req = json.loads(requests.read(name, MAX_REQUEST))
    except (Unsafe, OSError, ValueError) as e:
        return f"{name}: unreadable ({e}), removed"
    if not isinstance(req, dict) or req.get("id") != op_id:
        return f"{name}: malformed, removed"
    fields = {k: req.get(k) for k in ("op", "requested_by", "requested_at", "target")}
    if not all(v is None or isinstance(v, str) for v in fields.values()):
        return f"{name}: a field that is not text, removed"
    if ledger.get(op_id):
        return f"{op_id}: seen before, ignored"
    by = (fields["requested_by"] or "")[:200]
    op = fields["op"]
    if op == "assistant-key":
        return _assistant_key(host, ledger, op_id, by, req)
    if op == "cancel":
        target = fields["target"] or ""
        rec = ledger.get(target) if _ID.match(target) else None
        if rec and rec.get("state") in ACTIVE:
            ledger.update(target, cancel=True, cancelled_by=by)
            ledger.log(target, f"{by} asked to cancel")
            return f"{target}: cancel asked by {by}"
        return f"{target}: nothing to cancel"
    if op not in OPS:
        ledger.put({"id": op_id, "op": str(op)[:40], "requested_by": by, "requested_at": now_iso(), "state": "refused",
                    "result": "not an operation the host runs", "finished_at": now_iso()})
        return f"{op_id}: unknown operation refused"
    rec = {"id": op_id, "op": op, "title": OPS[op][0], "requested_by": by,
           "requested_at": (fields["requested_at"] or now_iso())[:40], "state": "accepted", "unit": f"arceus-op-{op_id}"}
    busy = [r for r in ledger.all() if r.get("state") in ACTIVE]
    if busy:
        b = busy[0]
        ledger.put({**rec, "state": "refused", "finished_at": now_iso(),
                    "result": f"another operation is under way: {b.get('title')}, asked by {b.get('requested_by')}. "
                              "One at a time."})
        return f"{op_id}: refused, {b['id']} under way"
    ledger.put(rec)
    r = host.root(["systemd-run", f"--unit=arceus-op-{op_id}", "--collect", "--no-block",
                   f"--description=Arceus console: {OPS[op][0]}", host.exe, "console", "hostops", *host.args(),
                   "run", op_id], timeout=60)
    if r.returncode != 0:
        ledger.update(op_id, state="failed", finished_at=now_iso(),
                      result=f"systemd-run did not start it: {(r.stderr or r.stdout).strip()[-300:]}")
        return f"{op_id}: systemd-run failed"
    return f"{op_id}: {op} started"


# -- running one ------------------------------------------------------------------------------------------------------

class Cancelled(Exception):
    pass


def run(host: Host, op_id: str) -> int:
    """One operation, in its transient unit. Its state and output go to the ledger and the console's copy."""
    ledger = Ledger(host)
    rec = ledger.get(op_id)
    if not rec or rec.get("state") != "accepted":
        return 1  # never run twice
    ledger.update(op_id, state="running", started_at=now_iso())
    say = lambda line: ledger.log(op_id, line)  # noqa: E731
    try:
        result, health = RUNS[rec["op"]](host, ledger, op_id, say)
        state = "succeeded"
    except Cancelled as e:
        state, result, health = "cancelled", str(e), ""
    except Failed as e:
        state, result, health = "failed", e.result, e.health
    except Exception as e:  # the operation's own bug: said, never a success
        state, result, health = "failed", f"{type(e).__name__}: {e}", ""
    say(f"{state}: {result}")
    ledger.update(op_id, state=state, finished_at=now_iso(), result=result, health=health)
    return 0 if state == "succeeded" else 1


class Failed(Exception):
    def __init__(self, result: str, health: str = ""):
        self.result, self.health = result, health
        super().__init__(result)


def stream(host: Host, say, args: list[str], env: dict | None = None, timeout: float = 3600) -> int:
    """A command's output into the operation's log, line by line, as it comes."""
    say("$ " + " ".join(args))
    p = host.popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, bufsize=1)
    started = time.monotonic()
    assert p.stdout is not None
    for line in p.stdout:
        say(line)
        if time.monotonic() - started > timeout:
            p.kill()
            say(f"stopped after {timeout:.0f}s")
    return p.wait()


def service(host: Host, name: str) -> dict:
    r = host.root(["systemctl", "show", name, "--property=ActiveState,SubState,ActiveEnterTimestamp,"
                                                "ExecMainStatus,LoadState"], timeout=30)
    out = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    return {"active": out.get("ActiveState", "unknown"), "sub": out.get("SubState", ""),
            "since": out.get("ActiveEnterTimestamp", ""), "status": out.get("ExecMainStatus", ""),
            "installed": out.get("LoadState", "") == "loaded"}


def runner_state(host: Host) -> dict:
    """The drain, its acknowledgement and the config the runner started with, from runner.db, as the run user."""
    r = host.as_user([host.exe, "--config", str(host.data / "config.yaml"), "console", "drain", "status"])
    if r.returncode != 0:
        raise Failed(f"runner.db cannot be read: {(r.stderr or r.stdout).strip()[-300:]}")
    return json.loads(r.stdout)


def drain(host: Host, *args: str) -> None:
    r = host.as_user([host.exe, "--config", str(host.data / "config.yaml"), "console", "drain", *args])
    if r.returncode != 0:
        raise Failed(f"the drain could not be {args[0]}: {(r.stderr or r.stdout).strip()[-300:]}")


def lift(host: Host, ledger: Ledger, op_id: str) -> bool:
    """Lift this operation's drain and make sure it is gone: then the record no longer owes it. Another
    operation's drain is never touched (the clear names this one's token). False when it could not be done: the
    record keeps owing it, and every tick tries again."""
    try:
        drain(host, "clear", "--token", op_id)
        current = (runner_state(host).get("drain") or {}).get("token")
    except Failed:
        return False
    if current == op_id:
        return False
    ledger.update(op_id, drain_owed=False)
    return True


def engine_tasks(host: Host) -> list[str] | None:
    """The engine's tasks running now (its own task list), or None when it cannot say."""
    tool = Path(host.run_user.pw_dir) / ".local" / "bin" / "night-shift"
    r = host.as_user([str(tool), "task", "list", "--json"], timeout=60)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout or "[]")
    except ValueError:
        return None
    tasks = data if isinstance(data, list) else data.get("tasks") or []
    return [t.get("task_id", "?") for t in tasks if isinstance(t, dict) and t.get("state") == "running"]


def _check_cancel(ledger: Ledger, op_id: str) -> None:
    rec = ledger.get(op_id) or {}
    if rec.get("cancel"):
        raise Cancelled(f"cancelled by {rec.get('cancelled_by') or 'someone'} before the engine was restarted")


def _wait_active(host: Host, name: str, say, after: str = "") -> dict:
    deadline = time.monotonic() + RESTART_WAIT
    while True:
        s = service(host, name)
        if s["active"] == "active" and (not after or s["since"] != after):
            return s
        if s["active"] == "failed" or time.monotonic() > deadline:
            r = host.root(["journalctl", "-u", name, "-n", "25", "--no-pager"], timeout=30)
            for line in (r.stdout or "").splitlines():
                say(line)
            raise Failed(f"{name} is {s['active']} ({s['sub']}) after the restart",
                         f"{name}: {s['active']} ({s['sub']}), exit status {s['status'] or '?'}")
        host.sleep(2)


def op_restart_runner(host: Host, ledger: Ledger, op_id: str, say) -> tuple[str, str]:
    before = service(host, host.unit("runner"))
    say(f"{host.unit('runner')}.service is {before['active']}, since {before['since'] or '?'}")
    say("A task in work keeps going: the agent's session is the engine's, and the runner picks the task up again.")
    r = host.root(["systemctl", "restart", host.unit("runner")], timeout=120)
    if r.returncode != 0:
        raise Failed(f"systemctl restart {host.unit('runner')} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    s = _wait_active(host, host.unit("runner"), say, after=before["since"] if before["active"] == "active" else "")
    # The runner writes which config it started with once its database is open: proof it got that far.
    deadline = time.monotonic() + 60
    loaded = ""
    while time.monotonic() < deadline:
        try:
            loaded = (runner_state(host).get("config") or {}).get("loaded_at") or ""
        except Failed:
            loaded = ""
        if loaded and loaded >= (ledger.get(op_id) or {}).get("started_at", ""):
            break
        host.sleep(2)
    health = f"{host.unit('runner')}.service active since {s['since']}"
    if loaded:
        health += f"; it loaded config.yaml at {loaded}"
    else:
        health += "; it has not recorded its start yet (see Home)"
    return "the runner restarted", health


def op_restart_engine(host: Host, ledger: Ledger, op_id: str, say) -> tuple[str, str]:
    rec = ledger.get(op_id) or {}
    ledger.update(op_id, drain_owed=True)  # first: whatever happens next, the drain is lifted in the end
    drain(host, "set", "--token", op_id, "--by", rec.get("requested_by") or "the console")
    say("Asked the runner to start nothing new in the engine.")
    try:
        ledger.update(op_id, state="waiting", waiting_for="the runner to acknowledge")
        deadline = time.monotonic() + ACK_WAIT
        while True:
            _check_cancel(ledger, op_id)
            st = runner_state(host)
            if (st.get("ack") or {}).get("token") == op_id:
                say("The runner acknowledged: nothing new starts in the engine.")
                break
            if service(host, host.unit("runner"))["active"] != "active":
                say("The runner is not running: nothing else starts work in the engine.")
                break
            if time.monotonic() > deadline:
                raise Failed("the runner did not acknowledge in 10 minutes; nothing was restarted")
            host.sleep(POLL)
        last = None
        while True:
            _check_cancel(ledger, op_id)
            running = engine_tasks(host)
            if running is None:
                raise Failed("the engine's task list cannot be read; nothing was restarted")
            if not running:
                break
            if running != last:
                say("Waiting for the engine's running tasks to end: " + ", ".join(running))
                ledger.update(op_id, waiting_for=", ".join(running))
                last = running
            host.sleep(15)
        ledger.update(op_id, state="running", waiting_for="")
        engine = host.unit("bulava-engine")
        before = service(host, engine)
        say(f"No task is running in the engine. Restarting {engine}.")
        r = host.root(["systemctl", "restart", engine], timeout=120)
        if r.returncode != 0:
            raise Failed(f"systemctl restart {engine} failed: {(r.stderr or r.stdout).strip()[-300:]}")
        s = _wait_active(host, engine, say, after=before["since"] if before["active"] == "active" else "")
    finally:
        if lift(host, ledger, op_id):
            say("Work may start in the engine again.")
        else:
            say("The drain could not be lifted now: the executor's next passes keep trying until it is.")
    return "the engine restarted between tasks", f"{host.unit('bulava-engine')} active since {s['since']}"


def _setup(host: Host, say, *args: str) -> int:
    u = host.setup_user  # setup.sh runs everything as its folder's owner, whichever project the console shows
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
           "SUDO_USER": u.pw_name, "HOME": "/root"}
    return stream(host, say, ["/bin/bash", str(host.setup), *args], env=env)


def op_setup_check(host: Host, ledger: Ledger, op_id: str, say) -> tuple[str, str]:
    rc = _setup(host, say, "--check")
    if rc == 0:
        return "every phase is done", ""
    if rc == 1:
        return "some phases are not done (todo above): an admin can run setup", ""
    raise Failed(f"setup.sh --check ended with exit {rc}")


def op_setup(host: Host, ledger: Ledger, op_id: str, say) -> tuple[str, str]:
    rc = _setup(host, say)
    if rc != 0:
        raise Failed(f"setup.sh ended with exit {rc}: see the output")
    s = service(host, host.unit("runner"))
    return ("setup is done. A new runner or engine runs once restarted: restart them here between tasks",
            f"{host.unit('runner')}.service {s['active']} since {s['since'] or '?'}")


def op_restart_assistant(host: Host, ledger: Ledger, op_id: str, say) -> tuple[str, str]:
    unit = host.unit("assistant")
    before = service(host, unit)
    if not before["installed"]:
        raise Failed(f"{unit} is not installed: turn the assistant on in Settings, then run setup (its assistant "
                     "phase installs it)")
    say(f"{unit}.service is {before['active']}, since {before['since'] or '?'}")
    say("A reply being written now is cut off and says so; messages from Slack are kept and answered after.")
    r = host.root(["systemctl", "restart", unit], timeout=120)
    if r.returncode != 0:
        raise Failed(f"systemctl restart {unit} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    s = _wait_active(host, unit, say, after=before["since"] if before["active"] == "active" else "")
    # The assistant writes its status (with the config.yaml it read) once it has started: proof it got that far.
    started = (ledger.get(op_id) or {}).get("started_at", "")
    deadline = time.monotonic() + 60
    status: dict = {}
    while time.monotonic() < deadline:
        status = assistant_status(host)
        if (status.get("started_at") or "") >= started:
            break
        host.sleep(2)
    health = f"{unit} active since {s['since']}"
    if (status.get("started_at") or "") >= started:
        health += "; it read config.yaml at " + status["started_at"]
    else:
        health += "; it has not written its status yet (see the Assistant page)"
    return "the assistant restarted", health


def assistant_route(host: Host) -> dict:
    """Whether the console's nginx routes /assistant/, and to which port: setup writes it from config.yaml, so a saved
    change of assistant.web is in nginx only once setup has run."""
    try:
        text = host.console_nginx.read_text()
    except FileNotFoundError:
        return {"known": False, "why": "the console's nginx.conf is not there (setup's console phase writes it)"}
    except OSError as e:
        return {"known": False, "why": f"the console's nginx.conf cannot be read ({e.strerror or e})"}
    m = re.search(r"location /assistant/ \{[^}]*?proxy_pass http://127\.0\.0\.1:(\d+);", text)
    return {"known": True, "port": int(m.group(1)) if m else None}


def assistant_status(host: Host) -> dict:
    try:
        d = SafeDir(host.data, create=False)
    except (Unsafe, OSError):
        return {}
    try:
        return json.loads(d.read("assistant-status.json", 64 * 1024))
    except (Unsafe, OSError, ValueError):
        return {}
    finally:
        d.close()


# -- the assistant's keys: write-only --------------------------------------------------------------------------------

KEYS_FILE = "assistant.env"


def _key_lines(text: str) -> list[tuple[str | None, str]]:
    """assistant.env as (name or None, the line as written)."""
    out: list[tuple[str | None, str]] = []
    for line in text.splitlines():
        name = line.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in line and \
            not line.lstrip().startswith("#") else None
        out.append((name, line))
    return out


def _keys_set(text: str) -> set[str]:
    return {n for n, line in _key_lines(text) if n and line.split("=", 1)[1].strip().strip("\"'")}


def write_key_state(host: Host) -> None:
    """Which keys are set and since when, for the console (no value, ever)."""
    try:
        times = json.loads((host.ledger / "assistant-keys.json").read_text())
    except (FileNotFoundError, ValueError):
        times = {}
    present: set[str] | None
    try:
        d = SafeDir(host.data, create=False)
        try:
            present = _keys_set(d.read(KEYS_FILE, 64 * 1024).decode("utf-8", "replace"))
        except FileNotFoundError:
            present = set()
        finally:
            d.close()
    except (Unsafe, OSError):
        present = None
    state = {"at": now_iso(), "readable": present is not None,
             "keys": {n: {"set": bool(present and n in present), **(times.get(n) or {})} for n in ASSISTANT_KEYS}}
    try:
        out = SafeDir(host.ops_dir, owner=host.owner)
    except (Unsafe, OSError):
        return
    try:
        out.write("assistant-keys.json", json.dumps(state).encode())
    finally:
        out.close()


def _assistant_key(host: Host, ledger: Ledger, op_id: str, by: str, req: dict) -> str:
    """Set, replace or remove one of the assistant's keys, now. The value is never recorded anywhere but
    assistant.env; what is said back names the key alone."""
    name, value = req.get("name"), req.get("value")
    rec = {"id": op_id, "op": "assistant-key", "requested_by": by, "requested_at": now_iso(),
           "title": f"The assistant's {name if name in ASSISTANT_KEYS else 'key'}"}

    def finish(state: str, result: str) -> str:
        ledger.put({**rec, "state": state, "result": result, "finished_at": now_iso()})
        return f"{op_id}: assistant key {name if name in ASSISTANT_KEYS else '?'}: {state}"

    if name not in ASSISTANT_KEYS or not isinstance(value, str):
        return finish("refused", "not one of the assistant's keys")
    remove = value == ""
    rec["title"] = f"{'Remove' if remove else 'Set'} the assistant's {name}"
    if not remove and (not KEY_VALUE.match(value) or not value.startswith(ASSISTANT_KEYS[name])):
        return finish("refused", f"not a {name}: it starts with {ASSISTANT_KEYS[name]}, one line, no spaces")
    # Only the data folder's own assistant.env, which setup made: config.yaml may name another file, and root does
    # not write where an editable setting points.
    r = host.as_user([host.exe, "--config", str(host.data / "config.yaml"), "console", "env"], timeout=60)
    wanted = next((line.split("=", 1)[1] for line in (r.stdout or "").splitlines()
                   if line.startswith("ASSISTANT_SECRETS=")), "")
    if r.returncode != 0 or wanted != str(host.data / KEYS_FILE):
        return finish("refused", "assistant.secrets in config.yaml is not this data folder's assistant.env: set the "
                                 "keys on the server")
    try:
        d = SafeDir(host.data, owner=host.owner, create=False)
    except (Unsafe, OSError) as e:
        return finish("failed", f"the data folder cannot be opened safely ({e})")
    try:
        try:
            text = d.read(KEYS_FILE, 64 * 1024).decode("utf-8")
        except FileNotFoundError:
            text = "# The assistant's tokens (docs/assistant.md). Set from the console's Assistant page.\n"
        lines = [line for n, line in _key_lines(text) if n != name]
        if not remove:
            lines.append(f"{name}={value}")
        d.write(KEYS_FILE, ("\n".join(lines) + "\n").encode(), mode=0o600)
    except (Unsafe, OSError, UnicodeDecodeError) as e:
        return finish("failed", f"assistant.env was not written ({type(e).__name__})")
    finally:
        d.close()
    try:
        times = json.loads((host.ledger / "assistant-keys.json").read_text())
    except (FileNotFoundError, ValueError):
        times = {}
    if remove:
        times.pop(name, None)
    else:
        times[name] = {"at": now_iso(), "by": by}
    (host.ledger / "assistant-keys.json").write_text(json.dumps(times))
    write_key_state(host)
    result = f"{name} {'removed' if remove else 'set'}."
    unit = host.unit("assistant")
    if name in SLACK_KEYS:
        if service(host, unit)["active"] == "active":
            r = host.root(["systemctl", "restart", unit], timeout=120)
            result += (" The assistant restarted to connect to Slack with it." if r.returncode == 0 else
                       " The assistant could not be restarted: restart it from the Assistant page.")
        else:
            result += " The assistant is not running: it connects with it when it starts."
    else:
        result += " Her next reply uses it." if name.endswith("_API_KEY") else " Her next ticket uses it."
    return finish("succeeded", result)


RUNS = {"restart-runner": op_restart_runner, "restart-engine": op_restart_engine, "setup-check": op_setup_check,
        "setup": op_setup, "restart-assistant": op_restart_assistant}
assert set(RUNS) == set(OPS)


# -- tick: reconcile and the services' state -------------------------------------------------------------------------

def tick(host: Host) -> None:
    ledger = Ledger(host)
    for rec in ledger.all():
        if rec.get("state") not in ACTIVE:
            continue
        unit = service(host, rec.get("unit") or f"arceus-op-{rec['id']}")
        if unit["active"] in ("active", "activating", "deactivating"):
            continue
        if rec.get("state") == "accepted" and _age(rec.get("requested_at")) < 30:
            continue  # systemd-run is still bringing it up
        ledger.log(rec["id"], "Its process is gone without a result (a reboot, or it was killed): interrupted.")
        owed = rec.get("op") == "restart-engine" or bool(rec.get("drain_owed"))
        ledger.update(rec["id"], state="interrupted", finished_at=now_iso(), drain_owed=owed,
                      result="interrupted: its process ended without a result. Nothing is retried by itself.")
    # A drain an operation still owes (it ended, or was lost, before lifting it): tried again on every pass.
    for rec in ledger.all():
        if rec.get("drain_owed") and rec.get("state") not in ACTIVE:
            if lift(host, ledger, rec["id"]):
                ledger.log(rec["id"], "The drain is lifted: work may start in the engine again.")
    services = {name: service(host, host.unit(name)) for name in SERVICES}
    routes = {"assistant": assistant_route(host)}
    write_key_state(host)
    try:
        d = SafeDir(host.ops_dir, owner=host.owner)
    except (Unsafe, OSError):
        return
    try:
        d.write("host.json", json.dumps({"at": now_iso(), "services": services, "routes": routes}).encode())
    finally:
        d.close()


def _age(iso: str | None) -> float:
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(str(iso))).total_seconds()
    except (TypeError, ValueError):
        return 1e9


def main(args) -> int:
    host = Host(data=Path(args.data), setup=Path(args.setup), ledger=Path(args.ledger), exe=sys.argv[0],
                runner_unit=getattr(args, "runner_unit", "runner"), engine_unit=getattr(args, "engine_unit", "bulava-engine"))
    if args.step == "dispatch":
        for line in dispatch(host):
            print(line)
        return 0
    if args.step == "run":
        if not _ID.match(args.id or ""):
            return 2
        return run(host, args.id)
    tick(host)
    return 0
