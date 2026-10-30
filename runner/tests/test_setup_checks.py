"""setup/checks/gitlab-protection.py against a fake GitLab: what it passes and what it reports."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "setup" / "checks" / "gitlab-protection.py"


@pytest.fixture(scope="module")
def protection():
    spec = importlib.util.spec_from_file_location("gitlab_protection", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DEFAULT_RULE = {"name": "main", "push_access_levels": [{"access_level": 0}],
                "merge_access_levels": [{"access_level": 40}], "allow_force_push": False}


class FakeGitLab:
    def __init__(self, rules: list[dict]):
        self.rules = rules
        self.c = self

    def me(self):
        return {"id": 7, "username": "acme-agent-bot"}

    def group(self, path):
        return {"id": 3}

    def group_repos(self, group, subgroups):
        return [SimpleNamespace(id=1, path="acme-platform", relative_path="acme-platform", default_branch="main")]

    def get_json(self, path, params=None):
        return {"access_level": 30} if "/members/" in path else self.rules


def run(protection, rules: list[dict], capsys) -> tuple[int, str]:
    g = SimpleNamespace(group="clients/acme", include_subgroups=True, exclude_repos=[], branch_prefix="agent/")
    rc = protection.check(g, FakeGitLab(rules))
    return rc, capsys.readouterr().out


def task_rule(name: str, force: bool, push: int = 30) -> dict:
    return {"name": name, "push_access_levels": [{"access_level": push}], "merge_access_levels": [],
            "allow_force_push": force}


@pytest.mark.parametrize("pattern,covers", [
    ("agent/*", True), ("agent/ACME-*", True), ("agent/ACME-12-fix", True), ("*", True), ("ag*", True),
    ("*-hotfix", True), ("main", False), ("release/*", False), ("agent", False), ("agent/", False), ("agents/*", False),
])
def test_which_rules_can_cover_a_task_branch(protection, pattern, covers):
    assert protection.may_cover(pattern, "agent/") is covers


def test_a_protected_default_branch_alone_passes(protection, capsys):
    rc, out = run(protection, [DEFAULT_RULE], capsys)
    assert rc == 0 and "FAIL" not in out


def test_a_rule_for_some_task_branches_without_force_push_fails(protection, capsys):
    rc, out = run(protection, [DEFAULT_RULE, task_rule("agent/ACME-*", force=False)], capsys)
    assert rc == 1 and "[FAIL] acme-platform: rule 'agent/ACME-*' covers task branches" in out
    assert "force push off" in out


def test_a_rule_that_refuses_the_bot_push_on_task_branches_fails(protection, capsys):
    rc, out = run(protection, [DEFAULT_RULE, task_rule("agent/*", force=True, push=40)], capsys)
    assert rc == 1 and "bot push refused" in out


def test_a_rule_that_lets_the_bot_force_push_task_branches_passes(protection, capsys):
    rc, out = run(protection, [DEFAULT_RULE, task_rule("agent/*", force=True)], capsys)
    assert rc == 0 and "covers task branches" in out


def test_task_branch_rules_are_reported_when_the_default_branch_is_not_protected(protection, capsys):
    rc, out = run(protection, [task_rule("agent/ACME-*", force=False)], capsys)
    assert rc == 1 and "'agent/ACME-*' covers task branches" in out and "default branch is not protected" in out


def test_a_narrow_rule_is_overruled_by_a_wider_one_that_allows_force_push(protection, capsys):
    """GitLab gives a branch the most permissive of the rules it matches."""
    rules = [DEFAULT_RULE, task_rule("agent/*", force=True), task_rule("agent/ACME-*", force=False, push=40)]
    rc, out = run(protection, rules, capsys)
    assert rc == 0 and "FAIL" not in out


def test_a_wider_rule_that_allows_nothing_does_not_help_some_task_branches(protection, capsys):
    rules = [DEFAULT_RULE, task_rule("agent/ACME-*", force=True), task_rule("agent/*", force=False)]
    rc, out = run(protection, rules, capsys)
    assert rc == 1 and "[FAIL] acme-platform: rule 'agent/*'" in out
    assert "[ok  ] acme-platform: rule 'agent/ACME-*'" in out


def test_a_rule_for_other_task_branches_does_not_lift_a_narrow_restriction(protection, capsys):
    rules = [DEFAULT_RULE, task_rule("agent/OPS-*", force=True), task_rule("agent/ACME-*", force=False)]
    rc, out = run(protection, rules, capsys)
    assert rc == 1 and "[FAIL] acme-platform: rule 'agent/ACME-*'" in out


def test_a_wildcard_that_lets_the_bot_force_push_everywhere_fails_for_the_default_branch(protection, capsys):
    rc, out = run(protection, [DEFAULT_RULE, task_rule("*", force=True)], capsys)
    assert rc == 1 and "[FAIL] acme-platform (main): rules: main, *" in out and "force push on" in out


@pytest.mark.parametrize("pattern,names,expected", [
    ("agent/*", "agent/ACME-*", True), ("*", "agent/ACME-*", True), ("agent/ACME-*", "agent/*", False),
    ("agent/*", "agent/ACME-12", True), ("main", "main", True), ("*", "main", True), ("release/*", "main", False),
    ("agent/*-fix", "agent/*", False), ("agent/*", "agent/*", True),
])
def test_which_rule_holds_wherever_another_does(protection, pattern, names, expected):
    assert protection.covers(pattern, names) is expected
