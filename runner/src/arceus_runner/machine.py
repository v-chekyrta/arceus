"""This machine's own processes and folders, as cleanup reads and removes them (Docker and git have their own).

A removal acts on exactly what was checked: a process through a pidfd taken after its start time was compared, a
folder through descriptors opened without following links, never by a name something could be put in place of
meanwhile. Which process or folder goes is decided in cleanup.py; the pass's time is checked as each one goes.
"""

from __future__ import annotations

import os
import re
import signal
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arceus_runner.cleanup import Item

SESSION_COMMS = ("tmux", "claude", "codex")  # a process below one of these belongs to a live session
OUT_OF_TIME = "out of time"


def inside(path: str | None, roots: tuple[str, ...]) -> bool:
    if not path:
        return False
    p = os.path.normpath(path)
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def _entries(path: Path, out_of_time: Callable[[], bool] = lambda: False):
    """Every entry below `path`, links not followed, as os.DirEntry; the pass's time is checked as it goes, not
    only between folders, so a folder of a million files cannot hold the tick."""
    todo, n = [str(path)], 0
    while todo:
        try:
            with os.scandir(todo.pop()) as it:
                for e in it:
                    n += 1
                    if n % 256 == 0 and out_of_time():
                        raise TimeoutError(f"{path}: {OUT_OF_TIME}")
                    if e.is_dir(follow_symlinks=False):
                        todo.append(e.path)
                    yield e
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            continue
    if out_of_time():
        raise TimeoutError(f"{path}: {OUT_OF_TIME}")


def tree_size(path: Path, out_of_time: Callable[[], bool] = lambda: False) -> int:
    total = 0
    for e in _entries(path, out_of_time):
        if not e.is_dir(follow_symlinks=False):
            try:
                total += e.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    return total


# -- processes -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Proc:
    pid: int
    ticks: int           # start time in clock ticks after boot: with the pid, which process this is
    started: datetime
    cwd: str
    cmd: str
    session: bool        # below a tmux, claude or codex process (or the runner): a live session's own


def _stat(pid: int, proc: Path) -> tuple[int, str, int] | None:
    """(parent pid, command name, start ticks) from /proc/<pid>/stat."""
    try:
        text = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    comm = text[text.index("(") + 1:text.rindex(")")]
    rest = text[text.rindex(")") + 2:].split()
    return int(rest[1]), comm, int(rest[19])


def scan_processes(roots: tuple[str, ...], proc: Path = Path("/proc"),
                   out_of_time: Callable[[], bool] = lambda: False) -> list[Proc]:
    """This user's processes whose working folder is in the workspace (Linux; elsewhere none). The pass's time
    is checked as it goes."""
    def check(n: int) -> None:
        if n % 128 == 0 and out_of_time():
            raise TimeoutError(f"processes: {OUT_OF_TIME}")
    try:
        btime = int(next(line.split()[1] for line in (proc / "stat").read_text().splitlines()
                         if line.startswith("btime")))
    except (OSError, StopIteration, ValueError):
        return []
    hz = os.sysconf("SC_CLK_TCK")
    me, mine = os.getuid(), os.getpid()
    info = {}
    for n, d in enumerate(proc.iterdir(), 1):
        check(n)
        if d.name.isdigit() and (s := _stat(int(d.name), proc)):
            info[int(d.name)] = s
    out, steps = [], 0
    for n, (pid, (ppid, comm, ticks)) in enumerate(info.items(), 1):
        check(n)
        if pid == mine:
            continue
        try:
            if (proc / str(pid)).stat().st_uid != me:
                continue
            cwd = os.readlink(proc / str(pid) / "cwd")
            cmd = (proc / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        if not inside(cwd, roots):
            continue
        session, seen, p = comm.startswith(SESSION_COMMS), set(), ppid
        while not session and p > 1 and p in info and p not in seen:
            steps += 1
            check(steps)
            seen.add(p)
            session = p == mine or info[p][1].startswith(SESSION_COMMS)
            p = info[p][0]
        out.append(Proc(pid, ticks, datetime.fromtimestamp(btime + ticks / hz, UTC), cwd, cmd or comm, session))
    return out


def kill_process(p: Proc, wait: float = 10, proc: Path = Path("/proc")) -> str:
    """SIGTERM, then SIGKILL, to exactly this process: a pid handle taken after checking its start time, so a
    pid reused by another process is never signalled."""
    def same() -> bool:
        s = _stat(p.pid, proc)
        return s is not None and s[2] == p.ticks
    if not hasattr(os, "pidfd_open"):
        return "kept"  # no race-free handle on this system: never signal by pid alone
    try:
        fd = os.pidfd_open(p.pid)
    except ProcessLookupError:
        return "gone"
    try:
        if not same():
            return "gone"
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        deadline = time.monotonic() + wait
        while same() and time.monotonic() < deadline:
            time.sleep(0.2)
        if same():
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)
    return "done"


# -- folders ---------------------------------------------------------------------------------------


def mount_points(mountinfo: Path = Path("/proc/self/mountinfo")) -> list[str] | None:
    """Every mount point (Linux). A bind mount of the same file system has the same st_dev, so only this list
    shows it. None on Linux when it cannot be read: then nothing is deleted. Elsewhere: none, and the st_dev
    walk below is the check."""
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        return None if sys.platform.startswith("linux") else []
    def unescape(p: str) -> str:
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), p)
    return [unescape(line.split()[4]) for line in lines if len(line.split()) > 4]


def remove_tree(path: Path, parent: Path, it: Item | None = None, *,
                out_of_time: Callable[[], bool] = lambda: False) -> str:
    """Delete a folder that is really where it was planned: not a symbolic link, its parent the expected one,
    no other file system mounted inside. The folder checked is the one deleted: it is opened, compared with what
    was checked, and emptied through that descriptor, each folder below opened the same way, never by a path that
    something could be put in place of meanwhile. Links are removed, never followed; another file system inside
    stops it. The pass's time is checked at every entry: what is left of a cut folder stays under its own name,
    for the next pass."""
    def kept(why: str) -> str:
        if it:
            it.detail["kept"] = why
        return "kept"
    try:
        st = path.lstat()
    except FileNotFoundError:
        return "gone"
    if not stat.S_ISDIR(st.st_mode):
        return kept("not a plain folder")
    if os.path.realpath(path.parent) != os.path.normpath(parent):
        return kept("its parent is not the workspace's .arceus folder")
    try:
        parent_st = os.stat(parent)
    except FileNotFoundError:
        return "gone"
    if st.st_dev != parent_st.st_dev:
        return kept("a file system is mounted on it")
    target = os.path.realpath(path)
    mounts = mount_points()
    if mounts is None:
        return kept("the mount table could not be read")
    if any(inside(m, (target,)) for m in mounts):
        return kept("a file system is mounted on or inside it")
    for e in _entries(path, out_of_time):
        if e.is_dir(follow_symlinks=False) and e.stat(follow_symlinks=False).st_dev != st.st_dev:
            return kept("another file system is mounted inside")
    # The parent is held open from here: a link put in place of it, or of a folder above it, after the checks
    # cannot send the removal elsewhere.
    pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        held = os.fstat(pfd)
        if (held.st_dev, held.st_ino) != (parent_st.st_dev, parent_st.st_ino):
            return kept("its parent changed while it was checked")
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=pfd)
        except FileNotFoundError:
            return "gone"
        except NotADirectoryError:
            return kept("another folder took its place while it was checked")
        try:
            if not _same(os.fstat(fd), st):
                return kept("another folder took its place while it was checked")
            _empty(fd, (st.st_dev, _mount_id(fd)), str(path), out_of_time)
        finally:
            os.close(fd)
        # Now an empty folder; it goes only while its name still means the folder emptied above. (Between this
        # look and rmdir only an empty folder could take its place, and rmdir takes nothing else.)
        try:
            if not _same(os.stat(path.name, dir_fd=pfd, follow_symlinks=False), st):
                return kept("emptied, but another folder took its place meanwhile")
            os.rmdir(path.name, dir_fd=pfd)
        except FileNotFoundError:
            pass
    finally:
        os.close(pfd)
    return "done"


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _mount_id(fd: int) -> int | None:
    """Which mount an open folder is on (Linux: /proc/self/fdinfo), read from the descriptor itself, so a bind
    mount of the same file system put inside after the mount table was read still shows. None elsewhere."""
    if not sys.platform.startswith("linux"):
        return None
    for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():  # unreadable: OSError, nothing goes
        if line.startswith("mnt_id:"):
            return int(line.split()[1])
    raise OSError("fdinfo has no mnt_id")


def _empty(fd: int, mount: tuple[int, int | None], where: str, out_of_time: Callable[[], bool]) -> None:
    """Everything in the open folder `fd`, through descriptors only: each folder below is opened without following
    a link, checked to be the entry just listed and on the same mount (`mount`: st_dev and mount id) before it is
    emptied."""
    with os.scandir(fd) as it:
        entries = [(e.name, e.stat(follow_symlinks=False)) for e in it]
    for name, est in entries:
        if out_of_time():
            raise TimeoutError(f"{where}: {OUT_OF_TIME}, partly removed")
        if stat.S_ISDIR(est.st_mode):
            if est.st_dev != mount[0]:
                raise OSError(f"{where}/{name}: another file system is mounted here; left as it is")
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                continue
            try:
                if not _same(os.fstat(child), est):
                    raise OSError(f"{where}/{name}: changed while it was removed; left as it is")
                if _mount_id(child) != mount[1]:
                    raise OSError(f"{where}/{name}: something is mounted here; left as it is")
                _empty(child, mount, f"{where}/{name}", out_of_time)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=fd)
        else:
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass
    if out_of_time():
        raise TimeoutError(f"{where}: {OUT_OF_TIME}, partly removed")
