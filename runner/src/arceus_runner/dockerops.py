"""The docker CLI as cleanup uses it: reading, and removing one named object at a time.

Every call gets what is left of the cleanup pass's time (`deadline`), so a docker that does not answer cannot hold
the runner's tick. What goes is decided in cleanup.py; nothing here removes more than the one object it is given,
except the build cache, which Docker offers no other way to clean than a prune by age.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import timedelta

OUT_OF_TIME = "out of time"  # the words gitops and machine use too: cleanup tells a command its budget cut by them

_UNITS = {"B": 1, "kB": 1000, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def size_bytes(value: str | None) -> int:
    """Docker's own sizes ("633.2MB", "4.496GB": powers of 1000) back to bytes; 0 when unreadable."""
    n = read_size(value)
    return n if n is not None else 0


def read_size(value: str | None) -> int | None:
    """As `size_bytes`, but None when Docker's text is not a size: a measurement, never a guess."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKMGT]?B)\s*", value or "")
    return int(float(m[1]) * _UNITS[m[2]]) if m else None


class DockerError(Exception):
    pass


class Docker:
    """The docker CLI: reading, and removals of one named object at a time. Never `-f` on a removal, never a prune
    of containers, volumes or images: Docker would decide what goes, not the rules here. The build cache alone is
    pruned, by how long ago a build last used it."""

    def __init__(self, binary: str = "docker", timeout: float = 60):
        self.binary = binary
        self.timeout = timeout
        self.deadline: float | None = None  # time.monotonic() by which a pass must end: no call outlives it

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _run(self, *args: str, timeout: float | None = None, check: bool = True) -> subprocess.CompletedProcess:
        timeout = timeout or self.timeout
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise DockerError(f"docker {args[0]}: {OUT_OF_TIME}")
        try:
            r = subprocess.run([self.binary, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DockerError(f"docker {args[0]}: no answer in {timeout:.0f}s"
                              + (f", {OUT_OF_TIME}" if self.deadline is not None and
                                 time.monotonic() >= self.deadline else "")) from None
        except OSError as e:
            raise DockerError(f"docker: {e}") from None
        if check and r.returncode != 0:
            raise DockerError(f"docker {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[-300:]}")
        return r

    def _inspect(self, what: str, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        # One that went away since the listing makes inspect fail; the others are still printed.
        r = self._run(what, "inspect", *ids, check=False)
        try:
            return json.loads(r.stdout or "[]")
        except ValueError:
            raise DockerError(f"docker {what} inspect: {(r.stderr or r.stdout).strip()[-300:]}") from None

    def labelled_mounts(self, label: str, disposable: str, timeout: float = 20) -> list[tuple[str, bool, list[str]]]:
        """(task, marked disposable, anonymous volumes it mounts) of every container with `label`, in one cheap
        listing: seen while the container exists, so its volumes stay known as the task's after a `docker rm`
        without `-v`. Bind mounts and named volumes are left out."""
        r = self._run("ps", "-a", "--no-trunc", "--filter", f"label={label}", "--format",
                      f'{{{{.Label "{label}"}}}}\t{{{{.Label "{disposable}"}}}}\t{{{{.Mounts}}}}', timeout=timeout)
        out = []
        for line in r.stdout.splitlines():
            task, mark, mounts = (line.split("\t") + ["", ""])[:3]
            names = [m for m in mounts.split(",") if re.fullmatch(r"[0-9a-f]{64}", m)]
            if task and names:
                out.append((task, mark == "true", names))
        return out

    def containers(self) -> list[dict]:
        return self._inspect("container", self._run("ps", "-aq", "--no-trunc").stdout.split())

    def container(self, cid: str) -> dict | None:
        found = self._inspect("container", [cid])
        return found[0] if found else None

    def volumes(self) -> list[dict]:
        return self._inspect("volume", self._run("volume", "ls", "-q").stdout.split())

    def volume(self, name: str) -> dict | None:
        found = self._inspect("volume", [name])
        return found[0] if found else None

    def network(self, nid: str) -> dict | None:
        found = self._inspect("network", [nid])
        return found[0] if found else None

    def networks(self) -> list[dict]:
        return self._inspect("network", self._run("network", "ls", "-q", "--no-trunc").stdout.split())

    def dangling_images(self) -> list[str]:
        """The IDs of untagged images, as Docker itself tells them (`dangling=true`)."""
        return sorted(set(self._run("image", "ls", "-q", "--no-trunc", "--filter", "dangling=true").stdout.split()))

    def image(self, iid: str) -> dict | None:
        found = self._inspect("image", [iid])
        return found[0] if found else None

    def images(self) -> list[dict]:
        """Every image, untagged ones included (a labelled `docker build` without `-t`), but no build layers. With
        the containerd image store (Docker 29's default) a plain `image ls` leaves untagged images out: they are
        listed apart, as Docker's own `dangling=true`."""
        ids = self._run("image", "ls", "-q", "--no-trunc").stdout.split()
        return self._inspect("image", sorted(set(ids) | set(self.dangling_images())))

    def usage(self) -> dict[str, dict]:
        """From `docker system df -v`: each volume's size, and each image's size of its own (the layers no other
        image shares: what removing it frees). {"volumes": {name: bytes}, "images": {id: (own, total)}}."""
        raw = json.loads(self._run("system", "df", "-v", "--format", "{{json .}}", timeout=120).stdout or "{}")
        return {"volumes": {v.get("Name"): size_bytes(v.get("Size")) for v in raw.get("Volumes") or []},
                "images": {i.get("ID"): (size_bytes(i.get("UniqueSize")), size_bytes(i.get("Size")))
                           for i in raw.get("Images") or []}}

    def disk_usage(self) -> dict[str, dict]:
        """`docker system df`'s rows by type ("Images", "Build Cache", ...): {"Size": "11.38GB",
        "Reclaimable": "3.179GB (27%)", ...}. Docker counts a layer that images share once."""
        rows = {}
        for line in self._run("system", "df", "--format", "{{json .}}").stdout.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            rows[row.get("Type")] = row
        return rows

    def stop(self, cid: str) -> None:
        self._run("stop", "-t", "20", cid, timeout=90)

    def remove_container(self, cid: str, volumes: bool = True) -> None:
        # With `-v` its anonymous volumes go with it (named ones stay); a running one is refused.
        self._run("rm", *(("-v",) if volumes else ()), cid)

    def remove_network(self, nid: str) -> None:
        self._run("network", "rm", nid)

    def remove_volume(self, name: str) -> None:
        self._run("volume", "rm", name)  # refused while any container uses it

    def remove_image(self, iid: str) -> None:
        self._run("image", "rm", iid, timeout=300)  # refused while a container uses it or other tags share it

    def prune_build_cache(self, unused_for: timedelta) -> int | None:
        """Build cache no build has used for `unused_for` (BuildKit counts from the last use, not from when it was
        made), shared with an image or not. Only the builder `docker builder` targets: the daemon's own unless
        another was selected with `docker buildx use`. What Docker says it reclaimed, in bytes; None when it said
        nothing readable."""
        # `until` takes Go durations: hours or seconds, never days ("7d" is refused).
        secs = max(1, int(unused_for.total_seconds()))
        until = f"{secs // 3600}h" if secs % 3600 == 0 else f"{secs}s"
        r = self._run("builder", "prune", "--force", "--all", "--filter", f"until={until}", timeout=600)
        m = re.search(r"Total(?: reclaimed space)?:\s*([\d.]+\s*[kKMGT]?B)", r.stdout)
        return read_size(m[1]) if m else None
