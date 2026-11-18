"""The assistant's tokens: one file of its own (assistant.secrets in config.yaml, ~/arceus/data/assistant.env), never
config.yaml, never the runner's secrets.env, never this process's environment.

    SLACK_APP_TOKEN=xapp-…     Socket Mode (connections:write)
    SLACK_BOT_TOKEN=xoxb-…     the bot's Web API calls
    YOUTRACK_TOKEN=perm:…      the account the tickets are made from
    ANTHROPIC_API_KEY=sk-ant-… provider claude, auth api_key: what her replies are billed to
    OPENAI_API_KEY=sk-…        provider codex, auth api_key: the same, on OpenAI

Read afresh at every use, so a key replaced from the console takes effect on the next reply. Whichever process needs
a value reads it here (the web server, the tools server); Claude's session is denied the whole data folder. The file
must be the run user's alone (600): a token readable by others is refused rather than used.
"""

from __future__ import annotations

import os
from pathlib import Path

from arceus_runner.config import ConfigError, read_env_file

NAMES = ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "YOUTRACK_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


class SecretsError(Exception):
    """The file is there but cannot be trusted or read: said to the administrator, never with a value in it."""


def read(path: Path) -> dict[str, str]:
    """Every value of the file, empty ones left out. A missing file is no values at all."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {}
    if st.st_mode & 0o077:
        raise SecretsError(f"{path}: permissions are {st.st_mode & 0o777:o}, must be 600")
    try:
        values = read_env_file(path)
    except ConfigError as e:
        raise SecretsError(str(e)) from None
    return {k: v for k, v in values.items() if v}


def get(path: Path, name: str) -> str | None:
    return read(path).get(name)
