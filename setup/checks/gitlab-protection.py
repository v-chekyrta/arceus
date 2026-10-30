"""Check in GitLab that the bot can work on task branches but cannot change the default branch.

    /opt/arceus-runner/venv/bin/python -I gitlab-protection.py

The bot is a Developer in the project's group; in every repository the default
branch is protected, nobody may push to it, only Maintainers merge, and force push is off. A rule that
also covers the task branches (`agent/*`, `*`) must let the bot push and force-push there: the runner
publishes a branch the agent rebased onto its target by replacing its own last push. Read-only:
it asks GitLab, changes nothing. Exit 0 only if every repository of the group is set up that way.
For a runner whose `code_host:` is GitLab.
"""

import sys
from functools import cache
from pathlib import Path

from arceus_runner.config import load_config, load_secrets
from arceus_runner.gitlab import GitLab, gitlab_client
from arceus_runner.http import ServiceError

CONFIG = Path("~/arceus/data/config.yaml").expanduser()
LEVEL = {0: "no one", 10: "guest", 15: "planner", 20: "reporter", 30: "developer", 40: "maintainer", 50: "owner"}
DEVELOPER, MAINTAINER = 30, 40


def may_cover(pattern: str, prefix: str) -> bool:
    """A protected-branch name (GitLab's wildcard is `*`) matches some branch that starts with `prefix`."""
    for i, ch in enumerate(pattern):
        if ch == "*":
            return True  # it takes the rest of the prefix, and whatever follows matches some task branch
        if i == len(prefix):
            return True  # the prefix is used up: a task branch can go on with whatever the pattern still asks
        if ch != prefix[i]:
            return False
    return False  # the pattern ends inside the prefix or exactly at it: no task branch is that short


def covers(pattern: str, names: str) -> bool:
    """Every branch `names` matches, `pattern` matches too: both are protected-branch names, `*` their only
    wildcard; a plain branch name is the case without one. A `*` in `names` can only be stood for by a `*` in
    `pattern`, so a True is always right; the rare pair of patterns that it misses reads as not covered."""
    @cache
    def match(i: int, j: int) -> bool:
        if i == len(pattern):
            return j == len(names)
        if pattern[i] == "*":
            return match(i + 1, j) or (j < len(names) and match(i, j + 1))
        return j < len(names) and names[j] != "*" and names[j] == pattern[i] and match(i + 1, j + 1)
    return match(0, 0)


def _levels(rules: list[dict], kind: str) -> list[int]:
    return [a.get("access_level") for r in rules for a in r.get(kind, []) if a.get("access_level") is not None]


def _bot_may(levels: list[int]) -> bool:
    return any(0 < lv <= DEVELOPER for lv in levels)


def main() -> int:
    cfg = load_config(CONFIG)
    g = cfg.code_host
    if g.type != "gitlab":
        print(f"code_host.type is {g.type!r}: this check is for GitLab")
        return 2
    return check(g, GitLab(gitlab_client(g, load_secrets(cfg).code_host, cfg.http)))


def check(g, gl) -> int:
    """Every line printed, and 0 only when all of them pass. `g` is the `code_host:` section."""
    me = gl.me()
    ok = True

    def line(passed: bool, text: str) -> None:
        nonlocal ok
        ok &= passed
        print(f"[{'ok  ' if passed else 'FAIL'}] {text}")

    group = gl.group(g.group)
    try:
        level = gl.c.get_json(f"/api/v4/groups/{group['id']}/members/all/{me['id']}")["access_level"]
        line(level == DEVELOPER, f"{me['username']} in {g.group}: {LEVEL.get(level, level)} (expected developer)")
    except ServiceError as e:
        line(False, f"{me['username']} is not a member of {g.group}: {e}")

    for repo in gl.group_repos(g.group, g.include_subgroups):
        if repo.relative_path in g.exclude_repos or repo.path in g.exclude_repos:
            continue
        name = f"{repo.relative_path} ({repo.default_branch})"
        try:
            rules = gl.c.get_json(f"/api/v4/projects/{repo.id}/protected_branches")
        except ServiceError as e:
            line(False, f"{name}: cannot read protected branches: {e}")
            continue
        # A branch several rules match gets the most permissive of them (GitLab: push, merge and force push alike).
        everywhere = f"{g.branch_prefix}*"
        for r in rules:
            if not may_cover(r["name"], g.branch_prefix):
                continue
            # Every rule that also holds for each task branch this one matches adds what it allows there.
            here = [p for p in rules if covers(p["name"], r["name"]) or covers(p["name"], everywhere)] + [r]
            pushes, force = _bot_may(_levels(here, "push_access_levels")), any(p.get("allow_force_push") for p in here)
            line(pushes and force,
                 f"{repo.relative_path}: rule {r['name']!r} covers task branches ({g.branch_prefix}…): bot push "
                 f"{'allowed' if pushes else 'refused'}, force push {'on' if force else 'off'}, with every rule that "
                 "also matches them (both needed to publish a rebased branch)")
        matching = [r for r in rules if covers(r["name"], repo.default_branch)]
        if not matching:
            line(False, f"{name}: default branch is not protected, the bot could push to it")
            continue
        push, merge = _levels(matching, "push_access_levels"), _levels(matching, "merge_access_levels")
        force = any(r.get("allow_force_push") for r in matching)
        detail = (f"rules: {', '.join(r['name'] for r in matching)}; "
                  f"push: {', '.join(LEVEL.get(x, str(x)) for x in push) or '-'}; "
                  f"merge: {', '.join(LEVEL.get(x, str(x)) for x in merge) or '-'}; "
                  f"force push {'on' if force else 'off'}")
        line(not _bot_may(push) and not _bot_may(merge) and not force, f"{name}: {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
