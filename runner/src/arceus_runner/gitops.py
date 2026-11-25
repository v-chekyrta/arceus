"""Git operations the runner does itself: clone, fetch, publish a reviewed commit.

The bot's credentials are in ~/.git-credentials for the code host only (see
write_git_credentials), so plain `git fetch`/`pull`/`push` works for the runner and for
the agent alike. Whether the agent may push from inside a run is the engine's policy
(Bulava's server profile refuses it today); either way the runner checks that the code host has
exactly the reviewed commit and pushes it itself when it is not there yet. The token is
never in a URL, on a command line or in a repository's .git/config.
"""

from __future__ import annotations

import re
import base64
import os
import shutil
import subprocess
import time
from collections.abc import Callable
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


def _diff_path(text: str, prefix: str) -> str | None:
    """A path from a `---`/`+++` line of `git diff`: None for /dev/null; quoted names unquoted; the tab git adds after
    a name with a space dropped."""
    if text.endswith("\t"):
        text = text[:-1]
    if text == "/dev/null":
        return None
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = _unquote(text[1:-1])
    return text[len(prefix):] if text.startswith(prefix) else text


_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(text: str) -> str:
    """A name git quoted C-style: backslash escapes and octal bytes, other characters as they are."""
    out, i = bytearray(), 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if text[i + 1:i + 4].isdigit() and len(text[i + 1:i + 4]) == 3:
                out.append(int(text[i + 1:i + 4], 8) & 0xFF)
                i += 4
                continue
        out += c.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


class Git:
    def __init__(self, auth: GitAuth | None, author_name: str, author_email: str):
        self.auth = auth
        self.author_name = author_name
        self.author_email = author_email
        self.deadline: float | None = None  # time.monotonic() no command may outlive (cleanup's time budget)

    def _run(self, args: list[str], cwd: Path | None, network: bool = False, check: bool = True,
             read_only: bool = False, errors: str = "strict") -> subprocess.CompletedProcess:
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
            res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, errors=errors,
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

    def fetch_ref(self, repo: Path, ref: str) -> None:
        """A ref outside the branches (a merge request's head: refs/merge-requests/4/head), kept under
        refs/remotes/origin/ so its commits stay until the next fetch of it."""
        self._run(["fetch", "--quiet", "origin", f"+{ref}:refs/remotes/origin/{ref.removeprefix('refs/')}"], repo,
                  network=True)

    # -- a checkout of its own beside the clone (a test run beside a ticket, workspace_run.py) -----------------
    #
    # Someone else may be at work in the clone meanwhile (a ticket's agent: its branch, index and files, its own
    # fetches, its stash). Nothing here touches any of that, and nothing the test run does can: the commit is fetched
    # into a ref of the run's own, not into origin/<branch> or FETCH_HEAD, and the checkout is a clone of its own in a
    # folder outside the clone. It borrows the clone's objects read-only (`clone --shared`) instead of copying them,
    # and keeps its own refs, stash, index and config; its `origin` is removed, so nothing it does reaches the
    # clone. The run's ref keeps the borrowed commit from the clone's garbage collection.

    def fetch_pinned(self, repo: Path, branch: str, ref: str) -> str | None:
        """The code host's `branch` fetched into `ref` (refs/arceus/<run>) and its commit, None when the code host
        has no such branch. Nothing else of the clone changes: no remote-tracking branch, no FETCH_HEAD, no tags, no
        pruning. The ref keeps the commit while the run reads it, a force-push meanwhile too."""
        if not self._run(["ls-remote", "--heads", "origin", f"refs/heads/{branch}"], repo, network=True).stdout.strip():
            return None
        self._run(["fetch", "--quiet", "--no-tags", "--no-prune", "--no-write-fetch-head", "--no-recurse-submodules",
                   "--refmap=", "origin", f"+refs/heads/{branch}:{ref}"], repo, network=True)
        return self.ref_sha(repo, ref)

    def git_dirs(self, repo: Path) -> tuple[str, str]:
        """The repository's own git folder and its common one, as git resolves them (absolute)."""
        out = self._run(["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], repo,
                        read_only=True).stdout.split("\n")
        return out[0].strip(), out[1].strip()

    def set_ref(self, repo: Path, ref: str, sha: str) -> None:
        self._run(["update-ref", ref, sha], repo)

    def drop_ref(self, repo: Path, ref: str) -> None:
        if (repo / ".git").exists():
            self._run(["update-ref", "-d", ref], repo)

    def _checkout_of(self, clone: Path, dest: Path) -> bool:
        """Whether `dest` is a checkout `checkout_at` made of `clone`: a repository of its own (never a link), whose
        objects are borrowed from `clone`'s and nobody else's."""
        if dest.is_symlink() or not (dest / ".git").is_dir() or (dest / ".git").is_symlink():
            return False
        try:
            borrowed = (dest / ".git" / "objects" / "info" / "alternates").read_text().split()
        except OSError:
            return False
        return [os.path.realpath(p) for p in borrowed] == [os.path.realpath(clone / ".git" / "objects")]

    def checkout_at(self, clone: Path, dest: Path, sha: str) -> bool:
        """`dest` a checkout of `clone` at the commit `sha`, detached, in a repository of its own that borrows
        `clone`'s objects and has no remote. One already there, of this clone, at that commit with nothing changed
        in it, is kept; anything else at `dest` goes and the checkout is made again. No hook of the repository runs.
        True when it was made."""
        if self._checkout_of(clone, dest):
            res = self._run(["rev-parse", "HEAD"], dest, check=False)
            clean = self._run(["status", "--porcelain", "--untracked-files=normal"], dest, check=False,
                              read_only=True)
            if res.returncode == 0 and res.stdout.strip() == sha and self.current_branch(dest) is None \
                    and clean.returncode == 0 and not clean.stdout.strip():
                read_only(dest)
                return False
        self.drop_checkout(clone, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run(["clone", "--quiet", "--shared", "--no-checkout", "--no-hardlinks", "--template=",
                   str(clone), str(dest)], None)
        self._run(["remote", "remove", "origin"], dest)
        self._run(["-c", "core.hooksPath=/dev/null", "checkout", "--quiet", "--detach", sha], dest, network=True)
        read_only(dest)
        return True

    def drop_checkout(self, clone: Path, dest: Path) -> list[str]:
        """Whatever is at `dest` gone. Safe to repeat, also after a crash halfway. What had been changed or added in
        a checkout of `clone` there, as `dirty_files` names it. `dest` must be in a folder the runner owns
        (workspace_run.claim_folder): anything there is its own. A linked worktree an earlier version made there is
        also forgotten by the clone."""
        left: list[str] = []
        if self._checkout_of(clone, dest):
            try:
                left = self.dirty_files(dest)
            except GitError:
                pass
        linked = (dest / ".git").is_file() and not dest.is_symlink()
        if dest.is_symlink():
            dest.unlink()
        elif dest.exists():
            writable(dest)
            shutil.rmtree(dest)
        if linked and (clone / ".git").exists():
            self._run(["worktree", "prune"], clone)  # only registrations whose folder is gone
        return left

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

    def unreviewed_below(self, repo: Path, base: str, sha: str, target: str, limit: int = 10) -> list[str]:
        """Commits `sha` would bring into `target` that a review of `base..sha` never saw: those it shares with
        `base` and `target` lacks. None when `base` is on `target`; a `base` that a rebase left off the branch
        still leaves its shared history to check."""
        bases = self._run(["merge-base", "--all", base, sha], repo, check=False).stdout.split()
        if not bases:
            return []
        out = self._run(["log", "--format=%h %s", f"-{limit}", *bases, "--not", target], repo).stdout
        return [line for line in out.splitlines() if line.strip()]

    def changed_files(self, repo: Path, default: str, sha: str) -> list[str]:
        """The files `sha` changes compared with where it left `default`, as a merge request shows them."""
        out = self._run(["diff", "--name-only", "-z", f"{default}...{sha}"], repo).stdout
        return [f for f in out.split("\0") if f]

    def changed_lines(self, repo: Path, default: str, sha: str) -> dict[str, list[list[int]]]:
        """Per file `sha` changes compared with where it left `default`, the line ranges ([first, last]) it adds or
        changes in the new version; a file it deletes, the lines it had. A deletion inside a file counts as the line
        where it happened. Paths are spelled as `changed_files` gives them."""
        # Renames found as `changed_files` finds them, so a moved file counts by what changed in it; the lines of the
        # code are not ours to read, so whatever bytes they hold never stop it.
        out = self._run(["-c", "core.quotePath=false", "diff", "-U0", "--no-color", "--no-ext-diff",
                         f"{default}...{sha}"], repo, errors="replace").stdout
        lines: dict[str, list[list[int]]] = {}
        old = new = None
        for row in out.splitlines():
            if row.startswith("--- "):
                old = _diff_path(row[4:], "a/")
            elif row.startswith("+++ "):
                new = _diff_path(row[4:], "b/")
            elif row.startswith("@@") and (new or old):
                m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", row)
                if not m:
                    continue
                if new is None:  # the file is deleted: the lines it had
                    start, count, path = int(m.group(1)), int(m.group(2) or 1), old
                else:
                    start, count, path = int(m.group(3)), int(m.group(4) or 1), new
                lines.setdefault(path, []).append([max(start, 1), max(start + count - 1, start, 1)])
        return lines

    def ref_sha(self, repo: Path, ref: str) -> str | None:
        res = self._run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo, check=False)
        return res.stdout.strip() or None

    def text_lines(self, repo: Path, ref: str, path: str) -> list[str] | None:
        """The lines of a text file at `ref`; None when there is no such file there or it is binary. `path` may come
        from an agent: it only ever names an object, never a file on disk."""
        if not self.blob_lines(repo, ref, path):
            return None
        res = self._run(["cat-file", "-p", f"{ref}:{path}"], repo, check=False, errors="replace")
        return res.stdout.splitlines() if res.returncode == 0 else None

    def rule_files(self, repo: Path, ref: str) -> list[str]:
        """The files of `ref` that hold a project's written rules: every CLAUDE.md and AGENTS.md, at any depth."""
        out = self._run(["ls-tree", "-r", "--name-only", "-z", ref], repo, check=False, errors="replace").stdout
        return [f for f in out.split("\0") if f.rsplit("/", 1)[-1] in ("CLAUDE.md", "AGENTS.md")]

    def commit_before(self, repo: Path, ref: str, when: str) -> str | None:
        """The newest commit of `ref` made before `when` (ISO time, by commit date); None when it has none that old."""
        res = self._run(["rev-list", "-1", f"--before={when}", ref], repo, check=False)
        return res.stdout.strip() or None

    def files_between(self, repo: Path, base: str | None, sha: str, deleted: bool = False) -> list[str]:
        """The files `sha` adds or changes since where it left `base` (with `deleted`, the ones it deletes); every
        file of `sha` without a base."""
        if base is None:
            out = "" if deleted else self._run(["ls-tree", "-r", "--name-only", "-z", sha], repo).stdout
        else:
            out = self._run(["-c", "diff.renameLimit=0", "diff", "--name-only", "-z", "--no-renames",
                             f"--diff-filter={'D' if deleted else 'd'}", f"{base}...{sha}"], repo).stdout
        return [f for f in out.split("\0") if f]

    def commit_count(self, repo: Path, base: str | None, sha: str) -> int:
        out = self._run(["rev-list", "--count", f"{base}..{sha}" if base else sha], repo).stdout.strip()
        return int(out or 0)

    def blob_lines(self, repo: Path, sha: str, path: str) -> int | None:
        """How many lines the file has at `sha`: None when there is no such file there, 0 for an empty or a binary
        one. `path` may come from an agent: it only ever names an object, never a file on disk."""
        kind = self._run(["cat-file", "-t", f"{sha}:{path}"], repo, check=False)
        if kind.returncode != 0 or kind.stdout.strip() != "blob":
            return None
        res = self._run(["grep", "-c", "-I", "-e", "", sha, "--", f":(literal){path}"], repo, check=False)
        out = res.stdout.strip()
        return int(out.rsplit(":", 1)[1]) if out else 0

    def switch(self, repo: Path, ref: str) -> None:
        """Check out a branch, or a commit (detached), that the repository already has."""
        self._run(["checkout", "--quiet", ref], repo)

    def ref_sha(self, repo: Path, ref: str) -> str | None:
        """The commit a ref (`origin/main`) points at in the local repository, as last fetched."""
        res = self._run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo, check=False)
        return res.stdout.strip() or None

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

    def push_sha(self, repo: Path, sha: str, branch: str, replaces: str | None = None) -> None:
        """Push exactly this commit to the branch. Without `replaces` only a fast-forward: a diverged remote
        branch is an error. With it the branch is rewritten (a rebased MR branch), but only while the code host
        still has exactly that commit there: anything pushed since makes the push fail, and nothing is lost."""
        lease = [f"--force-with-lease=refs/heads/{branch}:{replaces}"] if replaces else []
        self._run(["push", "--quiet", *lease, "origin", f"{sha}:refs/heads/{branch}"], repo, network=True)

    def carries(self, repo: Path, sha: str, base: str, tip: str) -> bool:
        """`sha` already holds everything `tip` changed since `base`: merging `tip` into it would change nothing.

        Judged by content, not by commits: a change carried over by a rebase or a cherry-pick counts, one
        reverted since, or a merge commit's own conflict resolution left behind, does not."""
        res = self._run(["merge-tree", "--write-tree", f"--merge-base={base}", sha, tip], repo, check=False)
        if res.returncode not in (0, 1):
            raise GitError(f"git merge-tree: {self._scrub(res.stderr.strip())[-500:]}")
        tree = res.stdout.split("\n", 1)[0].strip()
        return res.returncode == 0 and tree == self._run(["rev-parse", f"{sha}^{{tree}}"], repo).stdout.strip()

    def unfinished(self, repo: Path) -> str | None:
        """The git operation started here and neither finished nor aborted: "rebase", "cherry-pick", "revert",
        "merge", or None."""
        def there(name: str) -> bool:
            return (repo / self._run(["rev-parse", "--git-path", name], repo).stdout.strip()).exists()
        if there("rebase-merge") or there("rebase-apply"):
            return "rebase"
        if there("CHERRY_PICK_HEAD"):
            return "cherry-pick"
        if there("REVERT_HEAD"):
            return "revert"
        if there("MERGE_HEAD"):
            return "merge"
        if there("sequencer/todo"):  # a cherry-pick or revert of several commits, stopped between two of them
            return "revert" if "revert" in (repo / self._run(["rev-parse", "--git-path", "sequencer/opts"],
                                                             repo).stdout.strip()).read_text(errors="replace") \
                else "cherry-pick"
        return None

    def abort(self, repo: Path, operation: str) -> None:
        """Back to where the operation started: the commits made before it are kept, the half-done part goes."""
        self._run([operation, "--abort"], repo)


def read_only(top: Path) -> None:
    """Nothing under `top` writable by anyone, its git metadata included: a shell command that writes a file there,
    or git that writes its index, refs or stash there, fails. Links are not followed."""
    for d, dirs, files in os.walk(top, followlinks=False):
        for name in files:
            p = os.path.join(d, name)
            if not os.path.islink(p):
                os.chmod(p, os.lstat(p).st_mode & ~0o222)
        os.chmod(d, os.lstat(d).st_mode & ~0o222)


def writable(top: Path) -> None:
    """The owner may write under `top` again (read_only undone), so that the runner can remove it."""
    if top.is_symlink() or not top.is_dir():
        return
    os.chmod(top, os.lstat(top).st_mode | 0o700)
    for d, dirs, files in os.walk(top, followlinks=False):
        for name in dirs:
            p = os.path.join(d, name)
            if not os.path.islink(p):
                os.chmod(p, os.lstat(p).st_mode | 0o700)


def restore_workspace(git: Git, ws: Path, branch: str, repos: list[dict], home_prev: str | None, *,
                      stash_message: str, on_stashed: Callable[[str, str | None], None],
                      on_committed: Callable[[str, str], None]) -> None:
    """Every repository a run switched back on the branch it was on, what the run left stashed, the run's branch
    gone; then the same for the workspace home. Git steps only: the caller records the state after.

    `repos` are the snapshot's entries (`repo`, `prev`, `prev_head`, `sha`). Each step looks at where things are
    first, so a crash halfway is finished by calling this again: the stash comes before the switch, the switch
    before the branch is deleted, and a branch is deleted only at the commit just read (one made since keeps it)."""
    for r in repos:
        path = ws / r["repo"]
        if not (path / ".git").exists():
            continue
        if git.current_branch(path) == branch:
            if not git.is_clean(path):
                on_stashed(r["repo"], git.stash_all(path, stash_message))
            back = r.get("prev")
            git.switch(path, back if back and git.branch_exists(path, back) else r["prev_head"])
        sha = git.branch_sha(path, branch)
        if sha:
            if sha != r["sha"]:
                on_committed(r["repo"], sha)
            git.delete_branch_at(path, branch, sha)
    if (ws / ".git").exists():
        if git.current_branch(ws) == branch:
            git.home_on(ws, home_prev if home_prev and home_prev != branch and git.branch_exists(ws, home_prev)
                        else HOME_BRANCH)
        sha = git.branch_sha(ws, branch)
        if sha:
            git.delete_branch_at(ws, branch, sha)


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
