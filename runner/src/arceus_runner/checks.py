"""Config checks against the real YouTrack, GitLab and Slack.

Every mistake gets its own line naming the exact field, so four typos in the
config produce four errors, not one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from arceus_runner.config import Config, Secrets
from arceus_runner.gitlab import GitLab
from arceus_runner.http import AuthError, ServiceClient, ServiceError
from arceus_runner.slack import Slack, SlackApiError
from arceus_runner.youtrack import YouTrack

Status = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def youtrack_client(cfg: Config, secrets: Secrets, transport=None) -> ServiceClient:
    return ServiceClient(
        "youtrack",
        cfg.youtrack.url,
        {"Authorization": f"Bearer {secrets.youtrack}", "Accept": "application/json"},
        cfg.http,
        transport=transport,
    )


def gitlab_client(cfg: Config, secrets: Secrets, transport=None) -> ServiceClient:
    return ServiceClient("gitlab", cfg.gitlab.url, {"PRIVATE-TOKEN": secrets.gitlab}, cfg.http, transport=transport)


def slack_client(cfg: Config, secrets: Secrets, transport=None) -> ServiceClient:
    return ServiceClient(
        "slack", "https://slack.com", {"Authorization": f"Bearer {secrets.slack}"}, cfg.http, transport=transport
    )


def check_youtrack(cfg: Config, yt: YouTrack) -> list[Check]:
    c = cfg.youtrack
    out: list[Check] = []
    try:
        me = yt.me()
    except AuthError as e:
        return [Check("youtrack.auth", "fail", str(e))]
    except ServiceError as e:
        return [Check("youtrack.reachable", "fail", str(e))]
    if me.get("login") != c.bot_login:
        out.append(Check("youtrack.bot_login", "fail",
                         f"token belongs to {me.get('login')!r}, config says {c.bot_login!r}"))
    else:
        out.append(Check("youtrack.bot_login", "ok", c.bot_login))

    project = yt.find_project(c.project)
    if not project:
        out.append(Check("youtrack.project", "fail", f"project {c.project!r} not found or not visible to the bot"))
        return out
    if project.get("archived"):
        out.append(Check("youtrack.project", "fail", f"project {c.project!r} is archived"))
        return out
    out.append(Check("youtrack.project", "ok", f"{c.project} ({project.get('name')})"))

    fields = {f["field"]["name"]: f for f in yt.project_fields(project["id"]) if f.get("field")}

    def bundle_values(field: dict) -> list[str]:
        return [v["name"] for v in ((field.get("bundle") or {}).get("values") or []) if not v.get("archived")]

    state = fields.get(c.state_field)
    if not state:
        out.append(Check("youtrack.state_field", "fail",
                         f"field {c.state_field!r} not in project; fields: {', '.join(sorted(fields))}"))
    else:
        values = bundle_values(state)
        for key in ("queued", "in_progress", "code_review"):
            want = getattr(c.states, key)
            if want in values:
                out.append(Check(f"youtrack.states.{key}", "ok", want))
            else:
                out.append(Check(f"youtrack.states.{key}", "fail",
                                 f"{want!r} is not a {c.state_field} value; available: {', '.join(values)}"))

    tags = yt.tag_names()
    for key in ("queue", "needs_input", "failed"):
        want = getattr(c.tags, key)
        if want in tags:
            out.append(Check(f"youtrack.tags.{key}", "ok", want))
        else:
            out.append(Check(f"youtrack.tags.{key}", "fail",
                             f"tag {want!r} does not exist or is not shared with {c.bot_login}"))

    for key, logins in (("youtrack.allowed_users", c.allowed_users), ("security.reviewers", cfg.security.reviewers)):
        for login in logins:
            if yt.user_exists(login):
                out.append(Check(f"{key}.{login}", "ok", login))
            else:
                out.append(Check(f"{key}.{login}", "fail", f"no active YouTrack user {login!r}"))
    return out


def check_gitlab(cfg: Config, gl: GitLab, today: date | None = None) -> list[Check]:
    g = cfg.gitlab
    out: list[Check] = []
    today = today or datetime.now(UTC).date()
    try:
        me = gl.me()
    except AuthError as e:
        return [Check("gitlab.auth", "fail", str(e))]
    except ServiceError as e:
        return [Check("gitlab.reachable", "fail", str(e))]
    if me.get("username") != g.bot_username:
        out.append(Check("gitlab.bot_username", "fail",
                         f"token belongs to {me.get('username')!r}, config says {g.bot_username!r}"))
    else:
        out.append(Check("gitlab.bot_username", "ok", g.bot_username))

    try:
        tok = gl.token_self()
        scopes = tok.get("scopes") or []
        if "api" not in scopes:
            out.append(Check("gitlab.token_scopes", "fail", f"token needs scope 'api', has {scopes}"))
        else:
            out.append(Check("gitlab.token_scopes", "ok", ", ".join(scopes)))
        out.append(_expiry_check(cfg, tok, today))
    except ServiceError as e:
        out.append(Check("gitlab.token_expiry", "warn", f"cannot read token info: {e}"))

    try:
        repos = gl.group_repos(g.group, g.include_subgroups)
    except ServiceError as e:
        out.append(Check("gitlab.group", "fail", f"group {g.group!r}: {e}"))
        return out
    usable = [r for r in repos if r.relative_path not in g.exclude_repos and r.path not in g.exclude_repos]
    out.append(Check("gitlab.group", "ok", f"{g.group}: {len(usable)} repositories ({len(repos) - len(usable)} excluded)"))

    if not usable:
        out.append(Check("gitlab.repositories", "fail", f"no repositories left in {g.group} after exclude_repos"))
    for name in g.exclude_repos:
        if not any(name in (r.relative_path, r.path) for r in repos):
            out.append(Check(f"gitlab.exclude_repos.{name}", "warn", f"{name!r} is not in the group"))
    return out


def _expiry_check(cfg: Config, tok: dict, today: date) -> Check:
    exp = tok.get("expires_at")
    if not exp:
        return Check("gitlab.token_expiry", "warn", "token has no expiry date; set one")
    left = (date.fromisoformat(exp) - today).days
    warn = cfg.health.token_expiry_warn_days
    status: Status = "fail" if left < 0 else "warn" if left < warn else "ok"
    return Check("gitlab.token_expiry", status, f"expires {exp} ({left} days)")


def token_expiry(cfg: Config, gl: GitLab, today: date | None = None) -> Check:
    """The bot token's expiry, for the runner's own health while it runs."""
    try:
        return _expiry_check(cfg, gl.token_self(), today or datetime.now(UTC).date())
    except ServiceError as e:
        return Check("gitlab.token_expiry", "warn", f"cannot read token info: {e}")


def check_slack(cfg: Config, slack: Slack) -> list[Check]:
    assert cfg.slack
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


def run_online_checks(cfg: Config, secrets: Secrets) -> list[Check]:
    results: list[Check] = []
    yc = youtrack_client(cfg, secrets)
    gc = gitlab_client(cfg, secrets)
    try:
        results += _guard("youtrack", lambda: check_youtrack(cfg, YouTrack(yc)))
        results += _guard("gitlab", lambda: check_gitlab(cfg, GitLab(gc)))
        if cfg.slack:
            sc = slack_client(cfg, secrets)
            try:
                results += _guard("slack", lambda: check_slack(cfg, Slack(sc)))
            finally:
                sc.close()
    finally:
        yc.close()
        gc.close()
    return results


def _guard(service: str, fn) -> list[Check]:
    try:
        return fn()
    except ServiceError as e:
        return [Check(f"{service}.request", "fail", str(e))]
