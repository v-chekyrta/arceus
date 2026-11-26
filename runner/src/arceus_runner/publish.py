"""Publishing a task's reviewed commit: pushed to the code host exactly as Codex passed it, an MR per changed
repository, the ticket told and moved to Code Review.

Every step is safe to repeat after a crash, and the order is what makes it so:

- `publishing` is written before anything leaves the machine, so the next tick's reconcile publishes again;
- every repository is checked (the receipt, the branch point, closed MRs) before the first push;
- a push is checked against the remote before and after; a branch is rewritten only over the runner's own last
  push, and a person's push sends the work back to the agent instead of being overwritten;
- an MR is looked up before one is created; uploads, notes and the ticket comment go through the action journal.

Delivery decides when to publish and what a failure does (`fail`); this module only publishes."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arceus_runner import media
from arceus_runner.config import Config
from arceus_runner.db import Task, TaskStore
from arceus_runner.events import EventLog
from arceus_runner.gitops import Git, GitError
from arceus_runner.http import BadRequest, NotFound
from arceus_runner.integrations import CodeHost, IssueTracker, MergeRequest, Repo, key_part
from arceus_runner.replies import unattributed
from arceus_runner.texts import media_section, mr_media_markdown, with_media_section
from arceus_runner.workspace import Workspace


class Publisher:
    def __init__(self, cfg: Config, store: TaskStore, events: EventLog, git: Git, host: CodeHost,
                 tracker: IssueTracker, workspace: Workspace, *, fail: Callable[..., Any],
                 comment_with_media: Callable[..., tuple[str, int] | None],
                 hold_back: Callable[[Task, set[str], int], bool] | None = None,
                 journal_merges: Callable[..., list[tuple[str, dict]]] | None = None,
                 arm_merges: Callable[..., None] | None = None):
        self.cfg = cfg
        self.store = store
        self.events = events
        self.git = git
        self.host = host
        self.tracker = tracker
        self.workspace = workspace
        self.fail = fail  # Delivery.fail: the task fails with a reason, told on the ticket
        self.comment_with_media = comment_with_media  # Delivery.comment_with_media: a journaled ticket comment
        self.hold_back = hold_back
        self.journal_merges = journal_merges
        self.arm_merges = arm_merges
        # review.ReviewLoop, set by the loop: counts the round, follows the MRs, sends a person's push back to the
        # agent (count_round, after_publish, note_moved).
        self.review = None

    def publish(self, task: Task, show: dict, attempt: int) -> bool:
        """The reviewed commit of every repository on the code host, an MR for each, the ticket told and moved to
        Code Review. `show` is `task show` with a passed receipt (Delivery.publish checked it). True when published;
        False when the task failed or went back to the agent, with nothing after that point done.

        Every step can be repeated after a crash: `publishing` is written before anything leaves the machine, so
        reconcile publishes again; every repository is checked before the first push; a push is checked against
        the remote first, an MR is looked up before it is created, the ticket comment is journaled."""
        receipt = show.get("receipt") or {}
        workspace = self.cfg.paths.workspace.resolve()
        task = self.store.update_task(task.task_id, state="publishing")
        plan, problems = self._check_receipt(task, receipt.get("repos") or [], workspace)
        if problems:
            # Nothing is pushed unless every repository checks out.
            self.fail(task, "the commit is not the reviewed one, nothing was pushed", "\n".join(problems),
                      attempt=attempt)
            return False
        if not plan:
            self.fail(task, "no changes", attempt=attempt)
            return False
        if self.hold_back is not None:
            changed = {self._repo_name(p) for p, _ in plan} | {m.repo for m in self.store.task_mrs(task.task_id)}
            if self.hold_back(task, changed, attempt):
                return False

        group = {(workspace / r.relative_path).resolve(): r for r in self.host.repos()}
        missing = [str(path) for path, _ in plan if path not in group]
        if missing:  # all of them are resolved before the first push
            self.fail(task, f"a repository is not in the {self.host.label} {self.host.scope_kind}, nothing was pushed",
                      "\n".join(missing))
            return False
        targets = self._targets(task, plan, group)
        unreviewed = self._below_base(plan, group, targets)
        if unreviewed:
            self.fail(task, "the branch has commits below the reviewed range, nothing was pushed",
                      "\n".join(unreviewed), attempt=attempt)
            return False
        plan, dropped, reopened = self._closed_mrs(task, plan, group, targets)
        if reopened:
            self.fail(task, "a merge request of this task was closed without merge, but its branch has changes "
                            "again; nothing was pushed", "\n".join(reopened), attempt=attempt)
            return False
        stored = {m.repo: m for m in self.store.task_mrs(task.task_id)}
        dropped = [d for d in dropped if d[0] in stored]
        if dropped:
            task = self._close_dropped(task, dropped, stored)
        if not plan:
            self.fail(task, "the agent took all of its changes back out; its merge requests are closed" if dropped
                      else "no changes", attempt=attempt)
            return False
        visuals, skipped = media.load(media.media_dir(workspace, task.task_id))
        for line in skipped:
            self.events.emit("media_skipped", task_id=task.task_id, detail=line)
        urls: list[str] = []
        published: list[tuple[Repo, MergeRequest, str]] = []
        for path, r in plan:
            repo = group[path]
            sha, branch = r["reviewed_sha"], r["branch"]
            if not self._push(task, repo, path, r, stored):
                return False  # failed, or back to the agent: nothing after it is published
            self.events.emit("pushed", task_id=task.task_id, repo=repo.relative_path, branch=branch, sha=sha)
            shots = self._mr_media(task, repo.id, repo.relative_path, media.for_repo(visuals, repo.relative_path))
            description = self._mr_description(task, show, path, r, shots, targets[path])
            existed = {m.repo: m for m in self.store.task_mrs(task.task_id)}.get(repo.relative_path)
            mr = self._open_mr(repo, task, branch, description, after_closed=bool(r.get("after_closed")))
            if (existed or mr.state != "opened" or self.store.task_mrs(task.task_id)) \
                    and not (receipt.get("no_outcome") and existed):
                # A later round: describe what is there now. A round that declared no outcome has nothing to say
                # about the work, so the MR keeps its description rather than losing "What was done".
                self.host.set_description(repo.id, mr.iid, description)
            self.store.upsert_mr(task.task_id, repo.relative_path, repo.id, mr.iid, mr.url, sha)
            urls.append(mr.url)
            published.append((repo, mr, sha))
            self.events.emit("mr_ready", task_id=task.task_id, repo=repo.relative_path, mr_iid=mr.iid, url=mr.url)

        task = self.review.count_round(task)  # a review round counts once it ends in published code
        follow_up = task.review_rounds or task.ci_attempts
        self.store.update_task(task.task_id, mr_urls=self._mr_urls(task))
        shas = "-".join(r["reviewed_sha"][:10] for _, r in plan)
        head = ("Arceus addressed the review comments; its own review passed the updated changes. Same MR, updated "
                "branch:" if follow_up else "Arceus finished the work; its own review passed the changes. Merge request:")
        body = head + "\n" + "\n".join(f"- {u}" for u in urls)
        if dropped:
            body += "\n\nNo longer part of this task (its changes were taken back out, the MR is closed):\n" + \
                "\n".join(f"- {mr.url}" for _, mr, _ in dropped)
        posted = self.comment_with_media(task, f"mr:{shas}", body, visuals)
        self.review.after_publish(task, {self._repo_name(p): r["reviewed_sha"] for p, r in plan})
        merges = self.journal_merges(task, published) if self.journal_merges else []
        self.tracker.set_state(task.issue_db_id, self.cfg.tracker.states.code_review)
        self.store.update_task(task.task_id, state="code_review", reason=None,
                               published_at_ms=task.published_at_ms or (posted[1] if posted else None))
        self.events.emit("task_published", task_id=task.task_id, mrs=urls, round=follow_up)
        if merges and self.arm_merges:
            self.arm_merges(task, merges)
        return True

    def _why_rejected(self, path: Path, branch: str, sha: str, remote: str | None, err: GitError) -> tuple[str, str]:
        """The branch on the code host is not the reviewed commit and is not one the runner may replace."""
        host = self.host.label
        if remote:
            try:
                self.git.fetch_branch(path, branch)
                if self.git.is_ancestor(path, sha, remote):
                    # Someone (the agent, or a person) pushed more on top of what Codex reviewed.
                    return (f"the branch on {host} has commits that were not reviewed",
                            f"reviewed {sha[:12]}, on {host} {remote[:12]}; the branch is left as it is, no MR opened")
            except GitError:
                pass
            return (f"the branch on {host} has diverged from the reviewed commit",
                    f"reviewed {sha[:12]}, on {host} {remote[:12]}; not replaced: the runner rewrites a branch only "
                    "over its own last push")
        return "the push was rejected", str(err)

    def _check_receipt(self, task: Task, repos: list[dict], workspace: Path) \
            -> tuple[list[tuple[Path, dict]], list[str]]:
        """What the engine's receipt says to publish, checked against the workspace: (the repositories with
        reviewed changes, with their receipt; what does not check out). Pushes nothing."""
        problems: list[str] = []
        plan: list[tuple[Path, dict]] = []
        mine = {(workspace / n).resolve() for n in self.workspace.names(task)}
        seen: set[Path] = set()
        for r in repos:
            path = Path(r.get("path") or "").resolve()
            name = r.get("path")
            if path == workspace and not task.repo:
                # The home is the runner's, never published: whatever was committed there stays local.
                if (r.get("reviewed_sha") or "") != (r.get("base_sha") or ""):
                    self.events.emit("home_commits_not_published", task_id=task.task_id,
                                     base=r.get("base_sha"), reviewed=r.get("reviewed_sha"))
                continue
            if workspace not in path.parents:
                problems.append(f"{name}: outside the workspace")
                continue
            if path not in mine:
                reviewed, base = r.get("reviewed_sha") or "", r.get("base_sha") or ""
                if reviewed and reviewed == base:
                    # A clone the task was never given (one added to the group after the task was taken, or another
                    # task's) that this task did not change: nothing of it would be published.
                    self.events.emit("foreign_repo_unchanged", task_id=task.task_id, repo=name,
                                     branch=r.get("branch"), sha=reviewed)
                    continue
                problems.append(f"{name}: not one of this task's repositories")
                continue
            seen.add(path)
            if r.get("branch") != task.branch:
                problems.append(f"{name}: branch {r.get('branch')!r}, expected {task.branch!r}")
            if not r.get("clean"):
                problems.append(f"{name}: the working tree is not clean")
            reviewed, base = r.get("reviewed_sha") or "", r.get("base_sha") or ""
            try:
                head, current = self.git.head(path), self.git.current_branch(path)
            except GitError as e:
                problems.append(f"{name}: {e}")
                continue
            if head != reviewed:
                problems.append(f"{name}: HEAD {head[:12]} is not the reviewed {reviewed[:12]}")
            if current != r.get("branch"):
                problems.append(f"{name}: branch {current!r} is checked out")
            if reviewed and reviewed != base:
                plan.append((path, r))
        for path in sorted(mine - seen):
            # A repository Bulava did not report on may still hold the agent's commits: never lose them silently.
            try:
                if path.exists() and self.git.branch_exists(path, task.branch) \
                        and self.git.current_branch(path) == task.branch and self.git.has_unpushed(path):
                    problems.append(f"{self._repo_name(path)}: has commits on {task.branch}, but the execution engine "
                                    "did not report on this repository")
            except GitError as e:
                problems.append(f"{self._repo_name(path)}: {e}")
        return plan, problems

    def _close_dropped(self, task: Task, dropped: list[tuple[str, MergeRequest, str]], stored: dict) -> Task:
        """The MRs of repositories the agent took its changes back out of leave the task: closed by the runner when
        still open, a note once on each. The task as it is after."""
        for repo, mr, sha in dropped:
            # The agent took its changes back out: the MR is no longer part of this task.
            if mr.state == "opened":
                pid = stored[repo].project_id
                key = f"{task.task_id}:close-mr:{key_part(pid)}:{key_part(mr.iid)}"
                if not self.store.action(key):  # the note once, also when the close is retried after a crash
                    self.host.note_mr(pid, mr.iid, (
                        f"Closed by the Arceus runner: the agent took its changes for {task.task_id} back out of "
                        f"this repository (reviewed commit `{sha[:12]}`), so there is nothing left to merge here. "
                        "The rest of the task goes on in its other merge requests."))
                    self.store.action_intent(key, task.task_id, "close-mr")
                self.host.close_mr(pid, mr.iid)
                self.store.action_done(key)
            self.store.drop_mr(task.task_id, repo)
            self.events.emit("mr_dropped", task_id=task.task_id, repo=repo, mr_iid=mr.iid, url=mr.url,
                             closed_by_runner=mr.state == "opened")
        return self.store.update_task(task.task_id, mr_urls=self._mr_urls(task))

    def _mr_urls(self, task: Task) -> str:
        """The task's MRs as `mr_urls` holds them: the ones it follows (`task_mrs`), a merged one among them. An MR
        replaced by a new one in its repository, or dropped from the task, is not there."""
        return json.dumps(sorted(m.url for m in self.store.task_mrs(task.task_id)))

    def _push(self, task: Task, repo: Repo, path: Path, r: dict, stored: dict) -> bool:
        """The reviewed commit on the code host's branch. False when publishing stops here: the task failed, or a
        person's push sends the work back to the agent; nothing after this repository may be pushed then."""
        sha, branch = r["reviewed_sha"], r["branch"]
        remote = self.git.remote_sha(path, branch)
        if remote != sha:
            published = stored[repo.relative_path].published_sha if repo.relative_path in stored else None
            replaces = self._replaceable(path, branch, sha, remote, published)
            try:
                self.git.push_sha(path, sha, branch, replaces)
            except GitError as e:
                # The server may have taken the push before the connection broke. Ask it: the
                # right commit there is success; an unreachable remote stays for the next tick.
                remote = self.git.remote_sha(path, branch)
                if remote != sha:
                    if replaces and remote == replaces:
                        # Nobody pushed in between, yet the rewrite was refused: the host forbids force pushes
                        # to this branch (a protected branch), so a rebased branch cannot be published.
                        self.fail(task, f"{self.host.label} refused to replace the MR branch with its rebased "
                                        "version", f"{repo.relative_path}: {e}\nAllow force push to the "
                                        f"task branches ({branch}) on {self.host.label}, then /continue.")
                        return False
                    if remote and self.store.task_mrs(task.task_id):
                        self.git.fetch_branch(path, branch)
                        if not self.git.is_ancestor(path, sha, remote):
                            # A person pushed to the MR branch while the agent was fixing: their
                            # commits stay, and the agent brings them in on the next round.
                            rewritten = bool(published) and self.git.has_commit(path, published) \
                                and not self.git.is_ancestor(path, published, sha)
                            self.review.note_moved(task, repo.relative_path, remote, sha, published, rewritten)
                            return False
                        # The reviewed commit is already in, people added more on top: theirs to own.
                        self.events.emit("branch_ahead_of_review", task_id=task.task_id,
                                         repo=repo.relative_path, reviewed=sha, remote=remote)
                    else:
                        self.fail(task, *self._why_rejected(path, branch, sha, remote, e))
                        return False
        remote = self.git.remote_sha(path, branch)
        if remote != sha and not (remote and self.store.task_mrs(task.task_id)
                                  and self.git.is_ancestor(path, sha, remote)):
            self.fail(task, f"after the push the branch on {self.host.label} is not the reviewed commit",
                      f"{repo.relative_path}: {remote} instead of {sha}")
            return False
        return True

    def _replaceable(self, path: Path, branch: str, sha: str, remote: str | None, published: str | None) -> str | None:
        """The commit on the code host that the reviewed one may replace, when the agent rebased the MR branch.

        Only a branch the runner published itself: its own last push there, or that push with people's commits
        on top whose changes the reviewed commit holds (the agent cherry-picked them onto its rebased branch).
        Anything else is left in place: the push is then a plain one and fails, and nothing anyone pushed is lost.
        """
        if not remote or not published:
            return None
        self.git.fetch_branch(path, branch)
        if self.git.is_ancestor(path, remote, sha):
            return None  # a fast-forward needs no force
        if remote == published:
            return remote
        if self.git.has_commit(path, published) and self.git.is_ancestor(path, published, remote) \
                and self.git.carries(path, sha, published, remote):
            return remote
        return None

    def _open_mr(self, repo: Repo, task: Task, branch: str, description: str, after_closed: bool) -> MergeRequest:
        """The branch's MR, created unless it already has one (a retry after a crash, or a person closed it).

        `after_closed`: a person asked for more work after closing the branch's MR, so a closed one
        is not the answer and a new MR is opened next to it.
        """
        existing = self.host.branch_mr(repo.id, branch)
        if existing and not (after_closed and existing.state == "closed"):
            return existing
        return self.host.create_mr(repo.id, source_branch=branch, target_branch=repo.default_branch or task.base_branch,
                                   title=f"{task.task_id}: {task.summary}"[:250], description=description,
                                   label=self.cfg.code_host.mr_label)

    def _targets(self, task: Task, plan: list[tuple[Path, dict]], group: dict) -> dict[Path, str]:
        """The remote branch each repository's work merges into: the target of its MR when it has one (a person
        may have pointed it at a release branch, and the agent rebased onto that), else the default branch."""
        out = {}
        for path, r in plan:
            default = group[path].default_branch or task.base_branch or "main"
            mr = self.host.branch_mr(group[path].id, r["branch"])
            target = mr.target if mr and mr.target else default
            if target != default:
                self.git.fetch_branch(path, target)
            out[path] = f"origin/{target}"
        return out

    def _below_base(self, plan: list[tuple[Path, dict]], group: dict, targets: dict[Path, str]) -> list[str]:
        """Repositories whose reviewed range leaves out commits the branch would bring into its target.

        Codex saw `base_sha..reviewed_sha`. A base above the branch point (an engine that measured
        from the start of the attempt, not of the task) leaves the commits below it unreviewed,
        and pushing the branch would publish them.
        """
        out = []
        for path, r in plan:
            target = targets[path]
            if self.git.is_ancestor(path, r["base_sha"], target):
                continue
            below = self.git.unreviewed_below(path, r["base_sha"], r["reviewed_sha"], target)
            if below:
                out.append(f"{group[path].relative_path}: the review started at {r['base_sha'][:12]}, which is "
                           f"not on {target}; never reviewed: " + "; ".join(below))
        return out

    def _closed_mrs(self, task: Task, plan: list[tuple[Path, dict]], group: dict, targets: dict[Path, str]) \
            -> tuple[list[tuple[Path, dict]], list[tuple[str, MergeRequest, str]], list[str]]:
        """Split off the repositories whose branch has nothing left to merge, and those the code host refuses.

        A branch whose commits add up to nothing against its target (the agent reverted its
        work there, its way of taking a repository out of the task) is not pushed and gets no MR; its
        MR leaves the task, closed by the runner when it is still open. A branch that does change
        something while a person closed its MR cannot go anywhere: that decision stands, and the
        runner neither reopens the MR nor opens another.
        """
        kept, dropped, refused = [], [], []
        for path, r in plan:
            repo = group[path]
            changed = self.git.changes_since_fork(path, targets[path], r["reviewed_sha"])
            mr = self.host.branch_mr(repo.id, r["branch"])
            if changed and mr and mr.state == "closed" and self._asked_after(task, mr):
                kept.append((path, {**r, "after_closed": True}))  # a new MR next to the closed one
            elif changed and mr and mr.state == "closed":
                refused.append(f"{repo.relative_path}: {mr.url} was closed by a person; reopen it, or have the "
                               f"agent take these changes out of {repo.relative_path}")
            elif changed:
                kept.append((path, r))
            else:
                self.events.emit("no_net_change", task_id=task.task_id, repo=repo.relative_path,
                                 reviewed=r["reviewed_sha"])
                if mr and mr.state in ("opened", "closed"):  # a merged one stays: it is done, not dropped
                    dropped.append((repo.relative_path, mr, r["reviewed_sha"]))
        return kept, dropped, refused

    @staticmethod
    def _asked_after(task: Task, mr: MergeRequest) -> bool:
        """The MR was closed before the last go-ahead (`/continue`) people gave the task after its closing."""
        return bool(mr.closed_at_ms and task.published_at_ms and mr.closed_at_ms < task.published_at_ms)

    def _repo_name(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.cfg.paths.workspace.resolve()))

    def _mr_media(self, task: Task, project_id, repo: str, items: list[media.MediaItem]) -> list[str]:
        """Upload the agent's screenshots and recordings to the project once each; Markdown for the MR."""
        out = []
        if not self.host.supports_uploads:
            return out
        for m in items:
            key = f"{task.task_id}:upload:{key_part(project_id)}:{m.sha256}"
            act = self.store.action(key)
            if act and act[0] == "done":
                url = act[1]
            else:
                self.store.action_intent(key, task.task_id, "upload", m.upload_name)
                try:
                    url = self.host.upload(project_id, m.upload_name, m.data(), m.mime)
                except (BadRequest, NotFound) as e:  # refused (size, type): the MR goes out without it
                    self.events.emit("media_skipped", task_id=task.task_id, repo=repo, detail=f"{m.path.name}: {e}")
                    continue
                self.store.action_done(key, url)
            out.append(mr_media_markdown(m, url))
        return out

    def task_media(self, task: Task) -> list[media.MediaItem]:
        """The screenshots and recordings the agent left for this task, checked as for an MR."""
        visuals, skipped = media.load(media.media_dir(self.cfg.paths.workspace.resolve(), task.task_id))
        for line in skipped:
            self.events.emit("media_skipped", task_id=task.task_id, detail=line)
        return visuals

    def refresh_media(self, task: Task) -> list[media.MediaItem]:
        """A round that changed no code may still bring new screenshots: put them into the open MRs."""
        visuals = self.task_media(task)
        if not visuals:
            return []
        for mr in self.store.task_mrs(task.task_id):
            if mr.state not in ("", "opened"):
                continue
            shots = self._mr_media(task, mr.project_id, mr.repo, media.for_repo(visuals, mr.repo))
            current = self.host.description(mr.project_id, mr.iid)
            updated = with_media_section(current, shots)
            if updated != current:
                self.host.set_description(mr.project_id, mr.iid, updated)
        return visuals

    def _mr_description(self, task: Task, show: dict, path: Path, r: dict, shots: list[str] = (),
                        target: str | None = None) -> str:
        """What the MR says: the ticket, what the agent says it did, the commits, how it was checked.

        The commits are those the MR brings into `target`: after a rebase the reviewed range also holds the
        target's own newer commits, which are not the task's."""
        outcome = (show.get("outcome") or {}).get("summary") or ""
        commits = self.git.log_subjects(path, target or r["base_sha"], r["reviewed_sha"])
        others = [m.url for m in self.store.task_mrs(task.task_id) if m.repo != self._repo_name(path)]
        parts = [f"Ticket: [{task.task_id}]({self.tracker.issue_url(task.task_id)}) — {task.summary}"]
        if outcome:
            parts += ["", "## What was done", unattributed(outcome).strip()]
        if commits:
            parts += ["", "## Commits", *(f"- {c}" for c in commits)]
        if shots:
            parts += ["", media_section(shots)]
        parts += ["", "## Checks",
                  f"Commit `{r['reviewed_sha'][:12]}` passed review. Arceus pushed the branch and opened this MR; "
                  "merging is up to the team."]
        if others:
            parts += ["", "## Other MRs of this task", *(f"- {u}" for u in others)]
        return "\n".join(parts)
