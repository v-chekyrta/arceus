"""Git operations the runner does itself: clone, fetch, publish a reviewed commit.

The bot's credentials are in ~/.git-credentials for the code host only (see
write_git_credentials), so plain `git fetch`/`pull`/`push` works for the runner and for
the agent alike. Whether the agent may push from inside a run is the engine's policy
(Bulava's server profile refuses it today); either way the runner checks that the code host has
exactly the reviewed commit and pushes it itself when it is not there yet. The token is
never in a URL, on a command line or in a repository's .git/config.
"""

from __future__ import annotations

import base64
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

NETWORK_TIMEOUT = 600
LOCAL_TIMEOUT = 60
# The runner itself runs with umask 077 (its tokens and database), but the workspace is the
# project's: containers of the dev stack read files from it as other users. Git therefore runs with
# the ordinary 022, so a checkout is 644/755 as in any developer's clone.
WORKSPACE_UMASK = 0o022


def _workspace_umask() -> None:
    os.umask(WORKSPACE_UMASK)


class GitError(Exception):
    pass


@dataclass(frozen=True)
class GitAuth:
    base_url: str  # https://gitlab.example.com
    username: str
    token: str

    def env(self) -> dict[str, str]:
        basic = base64.b64encode(f"{self.username}:{self.token}".encode()).decode()
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{self.base_url.rstrip('/')}/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }


def base_env() -> dict[str, str]:
    keep = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/false"
    env.setdefault("LANG", "C.UTF-8")
    return env


HOME_BRANCH = "main"


class Git:
    def __init__(self, auth: GitAuth | None, author_name: str, author_email: str):
        self.auth = auth
        self.author_name = author_name
        self.author_email = author_email
        self.deadline: float | None = None  # time.monotonic() no command may outlive (cleanup's time budget)

    def _run(self, args: list[str], cwd: Path | None, network: bool = False, check: bool = True,
             read_only: bool = False) -> subprocess.CompletedProcess:
        env = base_env()
        if read_only:
            env["GIT_OPTIONAL_LOCKS"] = "0"  # `git status` would otherwise refresh the index
        if network and self.auth:
            env.update(self.auth.env())
        timeout = NETWORK_TIMEOUT if network else LOCAL_TIMEOUT
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise GitError(f"git {args[0]}: out of time")
        try:
            res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                                 timeout=timeout, preexec_fn=_workspace_umask)
        except subprocess.TimeoutExpired:
            late = self.deadline is not None and time.monotonic() >= self.deadline
            raise GitError(f"git {args[0]}: timed out" + (", out of time" if late else "")) from None
        if check and res.returncode != 0:
            raise GitError(f"git {' '.join(args[:2])}: {self._scrub(res.stderr.strip())[-500:]}")
        return res

    def _scrub(self, text: str) -> str:
        if self.auth:
            text = text.replace(self.auth.token, "<redacted>")
        return text

    def clone(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run(["clone", "--quiet", url, str(dest)], cwd=None, network=True)
        self.configure(dest)

    def configure(self, repo: Path) -> None:
        """Identity of the agent's commits. The runner itself commits only its own
        files in the workspace home (write_home) and, when an attempt fails, the agent's uncommitted
        work on the task branch (commit_all), so that nothing is lost and the next task starts clean."""
        self._run(["config", "user.name", self.author_name], repo)
        self._run(["config", "user.email", self.author_email], repo)

    def fix_modes(self, repo: Path) -> int:
        """Give tracked files the modes git records (644, or 755 for executables) where a checkout
        made under a private umask left them unreadable to others. Returns how many were fixed."""
        out = self._run(["ls-files", "-s", "-z"], repo).stdout
        fixed = 0
        for entry in out.split("\0"):
            if not entry or "\t" not in entry:
                continue
            meta, name = entry.split("\t", 1)
            mode = meta.split()[0]
            if mode not in ("100644", "100755"):
                continue
            path = repo / name
            try:
                st = path.lstat()
            except OSError:
                continue
            if st.st_mode & 0o044 == 0o044:
                continue
            path.chmod(0o755 if mode == "100755" else 0o644)
            fixed += 1
        for d in [repo, *(p for p in repo.rglob("*") if p.is_dir() and ".git" not in p.parts)]:
            if d.stat().st_mode & 0o055 != 0o055:
                d.chmod(0o755)
                fixed += 1
        return fixed

    def fetch(self, repo: Path) -> None:
        self._run(["fetch", "--quiet", "--prune", "origin"], repo, network=True)

    def is_clean(self, repo: Path) -> bool:
        return self._run(["status", "--porcelain", "--untracked-files=normal"], repo).stdout.strip() == ""

    def head(self, repo: Path) -> str:
        return self._run(["rev-parse", "HEAD"], repo).stdout.strip()

    def current_branch(self, repo: Path) -> str | None:
        res = self._run(["symbolic-ref", "--quiet", "--short", "HEAD"], repo, check=False)
        return res.stdout.strip() or None

    def has_commit(self, repo: Path, sha: str) -> bool:
        return self._run(["cat-file", "-e", f"{sha}^{{commit}}"], repo, check=False).returncode == 0

    def remote_sha(self, repo: Path, branch: str) -> str | None:
        out = self._run(["ls-remote", "origin", f"refs/heads/{branch}"], repo, network=True).stdout.strip()
        return out.split()[0] if out else None

    def branch_exists(self, repo: Path, branch: str) -> bool:
        return self._run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo, check=False).returncode == 0

    def start_branch(self, repo: Path, branch: str, base: str) -> None:
        """The task branch: kept if it exists (a follow-up), else made from the base."""
        if self.branch_exists(repo, branch):
            if self.current_branch(repo) != branch:
                self._run(["checkout", "--quiet", branch], repo)
        else:
            self._run(["checkout", "--quiet", "-b", branch, base], repo)

    def fetch_branch(self, repo: Path, branch: str) -> None:
        self._run(["fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], repo,
                  network=True)

    def commit_all(self, repo: Path, message: str) -> str:
        """Commit everything in the tree (tracked, untracked, not ignored) as the repository's author."""
        self._run(["add", "-A"], repo)
        self._run(["commit", "--quiet", "--no-verify", "-m", message], repo)
        return self.head(repo)

    def has_unpushed(self, repo: Path) -> bool:
        """HEAD has commits that no branch on GitLab (as last fetched) contains."""
        out = self._run(["rev-list", "--count", "HEAD", "--not", "--remotes=origin"], repo).stdout.strip()
        return out not in ("", "0")

    def is_ancestor(self, repo: Path, older: str, newer: str) -> bool:
        return self._run(["merge-base", "--is-ancestor", older, newer], repo, check=False).returncode == 0

    def changes_since_fork(self, repo: Path, default: str, sha: str) -> bool:
        """`sha` changes some file compared with where it left `default`: reverted work is no change."""
        res = self._run(["diff", "--quiet", f"{default}...{sha}"], repo, check=False)
        if res.returncode not in (0, 1):
            raise GitError(f"git diff {default}...{sha[:12]}: {self._scrub(res.stderr.strip())[-500:]}")
        return res.returncode == 1

    def changed_files(self, repo: Path, default: str, sha: str) -> list[str]:
        """The files `sha` changes compared with where it left `default`, as a merge request shows them."""
        out = self._run(["diff", "--name-only", "-z", f"{default}...{sha}"], repo).stdout
        return [f for f in out.split("\0") if f]

    def switch(self, repo: Path, ref: str) -> None:
        """Check out a branch, or a commit (detached), that the repository already has."""
        self._run(["checkout", "--quiet", ref], repo)

    def branch_sha(self, repo: Path, branch: str) -> str | None:
        res = self._run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo, check=False)
        return res.stdout.strip() or None

    def log_subjects(self, repo: Path, base: str, sha: str, limit: int = 30) -> list[str]:
        out = self._run(["log", "--format=%h %s", f"-{limit}", f"{base}..{sha}"], repo, check=False).stdout
        return [line for line in out.splitlines() if line.strip()]

    def catch_up(self, repo: Path, branch: str) -> str:
        """Bring the local task branch up to GitLab's when GitLab is ahead (a person pushed to the MR).

        Only a fast-forward: nothing local is ever dropped or rewritten. Returns what happened:
        "same", "forwarded", "ahead" (local has commits GitLab has not seen) or "diverged".
        """
        remote = self.remote_sha(repo, branch)
        if not remote:
            return "ahead"
        local = self.head(repo)
        if remote == local:
            return "same"
        self.fetch_branch(repo, branch)
        if self.is_ancestor(repo, local, remote):
            self._run(["merge", "--quiet", "--ff-only", f"origin/{branch}"], repo)
            return "forwarded"
        return "ahead" if self.is_ancestor(repo, remote, local) else "diverged"

    # -- the workspace home ---------------------------------------------------------------------
    #
    # Bulava runs a task in one git project. The workspace folder holding every repository of the
    # group is made that project: a repository of the runner's own few files, with each clone
    # below it ignored. The session starts there and the agent picks the repositories it needs.
    # Only HEAD, the index and those files are ever moved here: never the working tree as a whole,
    # so the clones inside are not touched whatever a task branch of the home contains.

    def write_home(self, home: Path, files: dict[str, str], message: str) -> bool:
        """Put the home on its main branch with these files committed. True if a commit was made."""
        home.mkdir(parents=True, exist_ok=True)
        if not (home / ".git").exists():
            self._run(["init", "--quiet", "-b", HOME_BRANCH], home)
        self.configure(home)
        if self.branch_exists(home, HOME_BRANCH):
            self.home_on(home, HOME_BRANCH)
        else:
            self._run(["symbolic-ref", "HEAD", f"refs/heads/{HOME_BRANCH}"], home)
        for name, text in files.items():
            (home / name).write_text(text)
        self._run(["add", "--", *files], home)
        if self.branch_exists(home, HOME_BRANCH) and \
                self._run(["diff", "--cached", "--quiet"], home, check=False).returncode == 0:
            return False
        self._run(["commit", "--quiet", "--no-verify", "-m", message], home)
        return True

    def home_on(self, home: Path, branch: str) -> None:
        """Switch the home to a branch it already has, touching only the files that branch tracks."""
        self._run(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], home)
        self._run(["reset", "--quiet"], home)
        # Plain files only: a repository the agent force-added (a gitlink) is never checked out here.
        tracked = [line.split("\t", 1)[1] for line in self._run(["ls-tree", "HEAD"], home).stdout.splitlines()
                   if "\t" in line and line.split()[1] == "blob"]
        if tracked:
            self._run(["checkout", "--quiet", "HEAD", "--", *tracked], home)

    # -- cleanup (cleanup.py) -------------------------------------------------------------------

    def dirty_files(self, repo: Path) -> list[str]:
        """What `git status` lists: changed tracked files and untracked ones that are not ignored."""
        entries = iter(self._run(["status", "--porcelain", "--untracked-files=normal", "-z"], repo,
                                 read_only=True).stdout.split("\0"))
        files = []
        for e in entries:
            if len(e) > 3:
                files.append(e[3:])
                if "R" in e[:2] or "C" in e[:2]:
                    next(entries, None)  # a rename's or copy's source path follows as an entry of its own
        return files

    def stash_all(self, repo: Path, message: str) -> str | None:
        """Changed and untracked files (never ignored ones: caches, builds) into a stash. Its commit, or None when
        there was nothing to stash (git then says so and makes none: the newest stash is an older one)."""
        def top() -> str | None:
            return self._run(["rev-parse", "--verify", "--quiet", "refs/stash"], repo, check=False).stdout.strip() or None
        before = top()
        self._run(["stash", "push", "--quiet", "--include-untracked", "-m", message], repo)
        after = top()
        return after if after != before else None

    def local_branches(self, repo: Path, prefix: str) -> dict[str, str]:
        out = self._run(["for-each-ref", "--format=%(refname:short) %(objectname)",
                         f"refs/heads/{prefix.rstrip('/')}"],
                        repo).stdout
        return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)

    def worktree_branches(self, repo: Path) -> set[str]:
        """Branches checked out in any worktree of the repository: `update-ref -d` would pull them from under it."""
        out = self._run(["worktree", "list", "--porcelain"], repo, read_only=True).stdout
        return {line.split(" ", 1)[1].removeprefix("refs/heads/") for line in out.splitlines()
                if line.startswith("branch ")}

    def on_remote(self, repo: Path, sha: str) -> bool:
        """Every commit of `sha` is in some branch of GitLab, as last fetched."""
        out = self._run(["rev-list", "--count", sha, "--not", "--remotes=origin"], repo).stdout.strip()
        return out == "0"

    def delete_branch_at(self, repo: Path, branch: str, sha: str) -> None:
        """Delete the branch only if it still points at `sha`: a commit made since keeps it."""
        self._run(["update-ref", "-d", f"refs/heads/{branch}", sha], repo)

    def push_sha(self, repo: Path, sha: str, branch: str) -> None:
        """Push exactly this commit to the branch. No force: a diverged remote branch is an error."""
        self._run(["push", "--quiet", "origin", f"{sha}:refs/heads/{branch}"], repo, network=True)


def write_git_credentials(base_url: str, username: str, token: str, home: Path | None = None) -> bool:
    """Keep the bot's code host credentials in ~/.git-credentials, for that host only.

    Other hosts' lines are left alone. Git is told to use the store for this host only, so
    nothing changes for any other remote of this user. Returns True if something changed.
    """
    from urllib.parse import quote, urlsplit

    home = home or Path.home()
    parts = urlsplit(base_url.rstrip("/"))
    host_url = f"{parts.scheme}://{parts.netloc}"
    line = f"{parts.scheme}://{quote(username, safe='')}:{quote(token, safe='')}@{parts.netloc}"
    store = home / ".git-credentials"
    lines = store.read_text().splitlines() if store.exists() else []
    kept = [x for x in lines if not (x.startswith(f"{parts.scheme}://") and x.endswith(f"@{parts.netloc}"))]
    changed = kept + [line] != lines
    if changed:
        tmp = store.with_suffix(".tmp")
        tmp.write_text("\n".join(kept + [line]) + "\n")
        tmp.chmod(0o600)
        tmp.replace(store)
    store.chmod(0o600)
    env = {**base_env(), "HOME": str(home)}
    key = f"credential.{host_url}.helper"
    current = subprocess.run(["git", "config", "--global", "--get", key], env=env, capture_output=True,
                             text=True).stdout.strip()
    if current != "store":
        subprocess.run(["git", "config", "--global", key, "store"], env=env, check=True, capture_output=True)
        changed = True
    return changed
