"""Check in GitLab that the bot can work on task branches but cannot change the default branch.

    /opt/arceus-runner/venv/bin/python -I gitlab-protection.py

The bot is a Developer in the project's group; in every repository the default
branch is protected, nobody may push to it, only Maintainers merge, and force push is off. Read-only:
it asks GitLab, changes nothing. Exit 0 only if every repository of the group is set up that way.
For a runner whose `code_host:` is GitLab.
"""

import sys
from pathlib import Path

from arceus_runner.config import load_config, load_secrets
from arceus_runner.gitlab import GitLab, gitlab_client
from arceus_runner.http import ServiceError

CONFIG = Path("~/arceus/data/config.yaml").expanduser()
LEVEL = {0: "no one", 10: "guest", 15: "planner", 20: "reporter", 30: "developer", 40: "maintainer", 50: "owner"}
DEVELOPER, MAINTAINER = 30, 40


def main() -> int:
    cfg = load_config(CONFIG)
    g = cfg.code_host
    if g.type != "gitlab":
        print(f"code_host.type is {g.type!r}: this check is for GitLab")
        return 2
    gl = GitLab(gitlab_client(g, load_secrets(cfg).code_host, cfg.http))
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
        rule = next((r for r in rules if r["name"] == repo.default_branch), None)
        if not rule:
            line(False, f"{name}: default branch is not protected, the bot could push to it")
            continue
        push = [a.get("access_level") for a in rule.get("push_access_levels", [])]
        merge = [a.get("access_level") for a in rule.get("merge_access_levels", [])]
        bot_can_push = any(lv is not None and 0 < lv <= DEVELOPER for lv in push)
        bot_can_merge = any(lv is not None and 0 < lv <= DEVELOPER for lv in merge)
        detail = (f"push: {', '.join(LEVEL.get(x, str(x)) for x in push) or '-'}; "
                  f"merge: {', '.join(LEVEL.get(x, str(x)) for x in merge) or '-'}; "
                  f"force push {'on' if rule.get('allow_force_push') else 'off'}")
        line(not bot_can_push and not bot_can_merge and not rule.get("allow_force_push"), f"{name}: {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
