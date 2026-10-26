"""Runner configuration: config.yaml (no secrets) and secrets.env (tokens).

Everything is validated strictly: an unknown key is an error, so a typo in a
field name is caught at start instead of silently falling back to a default.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

DEFAULT_CONFIG = Path("~/arceus/data/config.yaml")

_DURATION = re.compile(r"^\s*(\d+)\s*(s|m|h|d)\s*$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
# A model name goes onto the CLI's command line: what the engine's run-env accepts, never a leading dash.
MODEL_NAME = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$"


def parse_duration(value: object) -> timedelta:
    if isinstance(value, timedelta):
        return value
    if isinstance(value, str):
        m = _DURATION.match(value)
        if m:
            return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})
    raise ValueError(f"expected a duration like 30s, 2m, 6h or 5d, got {value!r}")


Duration = Annotated[timedelta, BeforeValidator(parse_duration)]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


# Keys an earlier layout used. They are refused by name, so the error says what to do with them.
_GONE = {
    "repo_field": "the agent works across all repositories of gitlab.group; remove this line",
    "repo_map": "the agent works across all repositories of gitlab.group; remove this block",
    "multi_repo_tasks": "every task can change any repository of the group now; remove this line",
    "default_repo": "a session no longer starts in one repository: it starts in the workspace folder, "
                    "and the agent picks the repositories the ticket needs; remove this line",
}


def _refuse_gone(section: str):
    def check(cls, data):
        if isinstance(data, dict):
            found = [f"{section}.{key} is no longer used: {_GONE[key]}" for key in _GONE if key in data]
            if found:
                raise ValueError("; ".join(found))
        return data
    return model_validator(mode="before")(classmethod(check))


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def _env_name(v: str) -> str:
    if not _ENV_NAME.match(v):
        raise ValueError(f"{v!r} is not an environment variable name (A-Z, 0-9, _)")
    return v


class YouTrackTags(Strict):
    queue: str = "agent"
    needs_input: str = "agent-needs-input"
    failed: str = "agent-failed"

    @model_validator(mode="after")
    def _distinct(self) -> YouTrackTags:
        names = [self.queue, self.needs_input, self.failed]
        if len(set(names)) != len(names):
            raise ValueError(f"tags must be different, got {names}")
        return self


class YouTrackStates(Strict):
    queued: str
    in_progress: str
    code_review: str


class YouTrackConfig(Strict):
    url: str
    project: str = Field(min_length=1)
    token_env: str = "YOUTRACK_TOKEN"  # name of the variable in secrets.env; there is rarely a reason to change it
    bot_login: str = Field(min_length=1)
    allowed_users: list[str] = Field(min_length=1)
    tags: YouTrackTags = YouTrackTags()
    state_field: str = "State"
    assignee_field: str | None = "Assignee"  # set to the bot when a task is taken; null to leave it
    states: YouTrackStates

    _gone = _refuse_gone("youtrack")

    _check_env = field_validator("token_env")(_env_name)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith(("https://", "http://")):
            raise ValueError("must start with https://")
        return v.rstrip("/")

    @model_validator(mode="after")
    def _users(self) -> YouTrackConfig:
        if self.bot_login in self.allowed_users:
            raise ValueError(f"allowed_users must not contain the bot itself ({self.bot_login})")
        if len(set(self.allowed_users)) != len(self.allowed_users):
            raise ValueError("allowed_users has duplicates")
        return self


class GitAuthor(Strict):
    name: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")


class GitLabConfig(Strict):
    url: str
    group: str = Field(min_length=1)
    token_env: str = "GITLAB_TOKEN"  # name of the variable in secrets.env; there is rarely a reason to change it
    bot_username: str = Field(min_length=1)
    git_author: GitAuthor
    include_subgroups: bool = True
    exclude_repos: list[str] = []
    branch_prefix: str = "agent/"
    mr_label: str = "agent"

    _check_env = field_validator("token_env")(_env_name)
    _gone = _refuse_gone("gitlab")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith(("https://", "http://")):
            raise ValueError("must start with https://")
        return v.rstrip("/")

    @field_validator("group")
    @classmethod
    def _group(cls, v: str) -> str:
        return v.strip("/")

    @field_validator("branch_prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        if not v.endswith("/") or v.startswith("/") or " " in v:
            raise ValueError("must look like 'agent/'")
        return v


class SlackConfig(Strict):
    token_env: str = "SLACK_BOT_TOKEN"  # name of the variable in secrets.env; there is rarely a reason to change it
    alerts_channel: str = Field(min_length=2)

    _check_env = field_validator("token_env")(_env_name)


class WorkingHours(Strict):
    days: list[Weekday] = Field(min_length=1)
    start: time = Field(alias="from")
    end: time = Field(alias="to")

    @model_validator(mode="after")
    def _order(self) -> WorkingHours:
        if self.start >= self.end:
            raise ValueError("'from' must be earlier than 'to'")
        return self


class Schedule(Strict):
    timezone: str = "Europe/Kyiv"
    working_hours: WorkingHours
    poll_working: Duration = timedelta(minutes=2)
    poll_off_hours: Duration = timedelta(minutes=10)
    poll_local: Duration = timedelta(minutes=2)
    poll_progress: Duration = timedelta(seconds=30)  # engine steps and agent activity: local files only
    review_debounce: Duration = timedelta(minutes=5)
    repo_sync: Duration = timedelta(hours=24)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {v!r}") from None
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class Backoff(Strict):
    start: Duration = timedelta(minutes=1)
    max: Duration = timedelta(minutes=15)
    jitter: float = Field(default=0.2, ge=0, lt=1)


class HttpConfig(Strict):
    connect_timeout: Duration = timedelta(seconds=10)
    read_timeout: Duration = timedelta(seconds=30)
    slow_response: Duration = timedelta(seconds=10)
    backoff: Backoff = Backoff()


class Limits(Strict):
    max_review_rounds: int = Field(default=5, ge=1)        # rounds that changed code
    max_review_answers: int = Field(default=20, ge=1)      # rounds answered without a code change
    max_ci_fix_attempts: int = Field(default=3, ge=1)
    needs_input_reminder: Duration = timedelta(hours=24)
    needs_input_fail_after: Duration = timedelta(days=5)
    in_progress_alert_after: Duration = timedelta(hours=6)
    attempt_timeout: Duration = timedelta(hours=8)  # an attempt with no verdict this long: "agent hung"
    agent_quiet_after: Duration = timedelta(minutes=20)  # nothing in the transcript this long: agent_quiet
    # How long after a failure a `/continue` comment on the ticket still resumes the task.
    continue_window: Duration = timedelta(days=14)


class HealthConfig(Strict):
    disk_warn_percent: int = Field(default=25, ge=1, le=99)
    disk_fail_percent: int = Field(default=10, ge=1, le=99)
    ram_warn_gb: float = Field(default=2, ge=0)
    token_expiry_warn_days: int = Field(default=14, ge=1)
    devstack_check: str | None = None

    @model_validator(mode="after")
    def _order(self) -> HealthConfig:
        if self.disk_fail_percent >= self.disk_warn_percent:
            raise ValueError("disk_fail_percent must be lower than disk_warn_percent")
        return self


def _expand(v: object) -> object:
    return Path(os.path.expanduser(v)) if isinstance(v, str) else v


ExpandedPath = Annotated[Path, BeforeValidator(_expand)]


class BulavaConfig(Strict):
    _gone = _refuse_gone("bulava")
    # The agent's fallback language for its reports, commits and MR summary. Bulava follows the
    # language of each message first; this applies when a message has none of its own.
    report_language: str = Field(default="English", min_length=2)
    codex_unavailable: Literal["fail", "wait"] = "fail"
    codex_unavailable_after_hours: int = Field(default=3, ge=1)
    mcp_config: ExpandedPath | None = Path("/opt/bulava-local/mcp.json")
    # The models and their depth. `auto` passes nothing: the engine's default depth (high) and the
    # CLI's default model for the server's account. Codex settings cover every Codex call of a task:
    # review, consultations, preflight and the report. A task keeps what it started with (the
    # engine's run-env); a change here applies to tasks dispatched after it.
    claude_model: str = Field(default="auto", pattern=MODEL_NAME)
    claude_effort: Literal["auto", "none", "low", "medium", "high", "xhigh", "max", "ultracode"] = "auto"
    codex_model: str = Field(default="auto", pattern=MODEL_NAME)
    codex_effort: Literal["auto", "low", "medium", "high", "xhigh", "max", "ultra"] = "auto"

    def model_env(self) -> dict[str, str]:
        """The engine's variables for every setting that is not `auto`."""
        chosen = {"SUPERVISOR_CLAUDE_MODEL": self.claude_model, "SUPERVISOR_CLAUDE_EFFORT": self.claude_effort,
                  "SUPERVISOR_CODEX_MODEL": self.codex_model, "SUPERVISOR_CODEX_EFFORT": self.codex_effort}
        return {k: v for k, v in chosen.items() if v != "auto"}

    def model_profile(self) -> dict[str, str]:
        """What was asked for, for the event log: `auto` stays `auto`, never a guess at the default."""
        return {"claude_model": self.claude_model, "claude_effort": self.claude_effort,
                "codex_model": self.codex_model, "codex_effort": self.codex_effort}


class SecurityConfig(Strict):
    """The security screen (screening.py): every text from people or CI is classified before the agent gets it."""
    # YouTrack logins who release (`/security-allow <code>`) or reject a held text. Required in live mode.
    # Better a short list of people who answer for the client's data than everyone who may start the agent.
    reviewers: list[str] = []
    model: str = Field(default="auto", pattern=MODEL_NAME)  # the classifier's Claude model; auto: the CLI's default
    timeout: Duration = timedelta(minutes=3)


class MetricsConfig(Strict):
    enabled: bool = True
    address: str = "127.0.0.1"  # Prometheus scrapes it on the same machine; never open it outside
    port: int = Field(default=9464, ge=0, le=65535)  # 0: any free port (tests)


class Paths(Strict):
    workspace: ExpandedPath = Path("~/arceus/workspace").expanduser()
    bulava: ExpandedPath = Path("/opt/bulava/engine")  # the engine folder: bin/dispatch.sh lives in it
    bulava_state: ExpandedPath = Path("~/.claude/supervisor").expanduser()
    bulava_env: ExpandedPath = Path("~/arceus/data/bulava.env").expanduser()
    secrets: ExpandedPath = Path("~/arceus/data/secrets.env").expanduser()
    db: ExpandedPath = Path("~/arceus/data/runner.db").expanduser()
    events: ExpandedPath = Path("~/arceus/data/events.jsonl").expanduser()


class Config(Strict):
    project: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    # observe: read YouTrack/GitLab, log what would happen, change nothing.
    # live: take tasks, run Bulava, push the reviewed commit and open MRs.
    mode: Literal["observe", "live"] = "observe"
    youtrack: YouTrackConfig
    gitlab: GitLabConfig
    slack: SlackConfig | None = None
    schedule: Schedule
    http: HttpConfig = HttpConfig()
    limits: Limits = Limits()
    health: HealthConfig = HealthConfig()
    bulava: BulavaConfig = BulavaConfig()
    security: SecurityConfig = SecurityConfig()
    metrics: MetricsConfig = MetricsConfig()
    paths: Paths = Paths()

    @model_validator(mode="after")
    def _security(self) -> Config:
        s = self.security
        if self.mode == "live" and not s.reviewers:
            raise ValueError("security.reviewers: required in live mode: the YouTrack logins who decide on a text "
                             "the security screen held")
        if self.youtrack.bot_login in s.reviewers:
            raise ValueError(f"security.reviewers must not contain the bot itself ({self.youtrack.bot_login})")
        if len(set(s.reviewers)) != len(s.reviewers):
            raise ValueError("security.reviewers has duplicates")
        return self


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


def _format(err: ValidationError) -> list[str]:
    out = []
    for e in err.errors():
        where = ".".join(str(p) for p in e["loc"]) or "(top level)"
        msg = e["msg"].removeprefix("Value error, ")
        if e["type"] == "extra_forbidden":
            msg = "unknown key (typo?)"
        elif e["type"] == "missing":
            msg = "required"
        out.append(f"{where}: {msg}")
    return out


def load_config(path: Path) -> Config:
    path = path.expanduser()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError([f"{path}: file not found"]) from None
    except yaml.YAMLError as e:
        raise ConfigError([f"{path}: not valid YAML: {e}"]) from None
    if not isinstance(raw, dict):
        raise ConfigError([f"{path}: expected a mapping at the top level"])
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(_format(e)) from None


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a systemd-style KEY=VALUE file. Comments and blank lines are skipped."""
    out: dict[str, str] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if not sep or not _ENV_NAME.match(key):
            raise ConfigError([f"{path}:{n}: expected KEY=VALUE"])
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


@dataclass(frozen=True)
class Secrets:
    youtrack: str
    gitlab: str
    slack: str | None

    def values(self) -> list[str]:
        return [v for v in (self.youtrack, self.gitlab, self.slack) if v]

    def __repr__(self) -> str:  # never print tokens by accident
        return "Secrets(<redacted>)"


def load_secrets(cfg: Config, environ: dict[str, str] | None = None) -> Secrets:
    """Tokens come from paths.secrets; the process environment is a fallback for development.

    The runner reads the file itself instead of getting it through systemd's
    EnvironmentFile, so the tokens never sit in its environment and are not
    inherited by Bulava or any other child process.
    """
    environ = dict(os.environ if environ is None else environ)
    problems: list[str] = []
    file_values: dict[str, str] = {}
    path = cfg.paths.secrets
    if path.exists():
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            problems.append(f"{path}: permissions are {mode:o}, must be 600")
        file_values = read_env_file(path)

    def get(name: str) -> str | None:
        return file_values.get(name) or environ.get(name) or None

    wanted = {"youtrack": cfg.youtrack.token_env, "gitlab": cfg.gitlab.token_env}
    if cfg.slack:
        wanted["slack"] = cfg.slack.token_env
    found = {k: get(v) for k, v in wanted.items()}
    for k, v in wanted.items():
        if not found[k]:
            problems.append(f"{k}.token_env: {v} is not set in {path} or the environment")
    if problems:
        raise ConfigError(problems)
    return Secrets(youtrack=found["youtrack"], gitlab=found["gitlab"], slack=found.get("slack"))
