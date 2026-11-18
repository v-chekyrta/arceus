"""Config checks against the real issue tracker, code host and Slack.

Every mistake gets its own line naming the exact field, so four typos in the
config produce four errors, not one. What is checked in the tracker and the host
themselves is the provider's (youtrack.py, gitlab.py); the allowlists are checked here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from arceus_runner.config import Config, Secrets
from arceus_runner.http import ServiceClient, ServiceError
from arceus_runner.integrations import (CodeHost, IssueTracker, build_code_host, build_register, build_tracker,
                                        usable_repos)
from arceus_runner.slack import Slack, SlackApiError

Status = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def slack_client(cfg: Config, secrets: Secrets, transport=None) -> ServiceClient:
    return ServiceClient(
        "slack", "https://slack.com", {"Authorization": f"Bearer {secrets.slack}"}, cfg.http, transport=transport
    )


def check_tracker(cfg: Config, tracker: IssueTracker) -> list[Check]:
    """The provider's own checks, then the allowlists when the tracker could be read at all."""
    found = tracker.check()
    if any(c.status == "fail" and c.name.endswith((".auth", ".reachable")) for c in found):
        return found
    return found + check_users(cfg, tracker)


def check_users(cfg: Config, tracker: IssueTracker) -> list[Check]:
    """Everyone on the allowlists is an active user of the tracker."""
    out: list[Check] = []
    for key, logins in (("tracker.allowed_users", cfg.tracker.allowed_users),
                        ("security.reviewers", cfg.security.reviewers)):
        for login in logins:
            if tracker.user_exists(login):
                out.append(Check(f"{key}.{login}", "ok", login))
            else:
                out.append(Check(f"{key}.{login}", "fail", f"no active {tracker.label} user {login!r}"))
    return out


def check_slack(cfg: Config, slack: Slack) -> list[Check]:
    assert cfg.slack and cfg.slack.alerts_channel
    try:
        slack.auth_test()
    except SlackApiError as e:
        return [Check("slack.auth", "fail", str(e))]
    except ServiceError as e:
        return [Check("slack.reachable", "fail", str(e))]
    out = [Check("slack.auth", "ok", "token valid")]
    try:
        found = slack.channel_exists(cfg.slack.alerts_channel)
        out.append(Check("slack.alerts_channel", "ok" if found else "fail",
                         cfg.slack.alerts_channel if found else f"{cfg.slack.alerts_channel} not found"))
    except SlackApiError as e:
        # chat:write alone cannot list channels; say so instead of guessing.
        out.append(Check("slack.alerts_channel", "warn", f"cannot list channels ({e}); add channels:read to verify"))
    return out


def run_online_checks(cfg: Config, secrets: Secrets, register: bool = False) -> list[Check]:
    """The tracker, the code host and Slack; with `register` (`arceus-runner check` only, never before `run`: an
    optional service that is down keeps no ticket waiting), the advisory register too."""
    results: list[Check] = []
    tracker = build_tracker(cfg, secrets)
    host = build_code_host(cfg, secrets)
    try:
        results += _guard(tracker.name, lambda: check_tracker(cfg, tracker))
        results += _guard(host.name, lambda: host.check(cfg.health.token_expiry_warn_days))
        if register and cfg.secops.enabled:
            from arceus_runner.secops import check_register  # it imports this module
            advisories = build_register(cfg, secrets)
            try:
                results += check_register(cfg, advisories, host.scope)
            finally:
                advisories.close()
        if cfg.tracker.platforms:
            results += _guard(host.name, lambda: check_platforms(cfg, host))
        if cfg.slack and cfg.slack.alerts_channel:
            sc = slack_client(cfg, secrets)
            try:
                results += _guard("slack", lambda: check_slack(cfg, Slack(sc)))
            finally:
                sc.close()
        if cfg.slack and cfg.slack.webhook_env:
            # Posting is the only test a webhook has, and a check that runs on every start must not post.
            results.append(Check("slack.webhook", "ok", f"{cfg.slack.webhook_env} is an incoming webhook URL; "
                                                        "`arceus-runner slack-test` posts a test message"))
    finally:
        tracker.client.close()
        host.client.close()
        for extra in ("approver_client", "reactions_client"):
            if getattr(host, extra, None):
                getattr(host, extra).close()
    return results


def check_platforms(cfg: Config, host: CodeHost) -> list[Check]:
    """Every repository `tracker.platforms` names is one the agent gets: a wrong path would hold back every
    ticket tagged for that platform."""
    have = {r.relative_path for r in usable_repos(cfg, host.repos())}
    out = []
    for name, repos in cfg.tracker.platforms.items():
        lost = [r for r in repos if r not in have]
        out.append(Check(f"tracker.platforms.{name}", "fail" if lost else "ok",
                         f"not repositories the agent gets: {', '.join(lost)}" if lost else ", ".join(repos)))
    return out


def _guard(service: str, fn) -> list[Check]:
    try:
        return fn()
    except ServiceError as e:
        return [Check(f"{service}.request", "fail", str(e))]
