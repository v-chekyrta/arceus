"""The assistant's settings are config.yaml's `assistant:` section, read by the runner's own code; its tokens are in
assistant.env and nowhere else."""

import os
from pathlib import Path

import pytest
import yaml

from arceus_runner.config import ConfigError
from assistant import secrets
from assistant.server import PROMPT, SESSION_SETTINGS, Settings

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "config.example.yaml"

# The shapes of the two assistants on the factory (Northwind in a web chat and Slack; Contoso in Slack only, with a
# client channel it only listens to), with made-up people and channels.
NORTHWIND = {
    "enabled": True, "project": "Northwind", "name": "Олександра", "gender": "f", "persona_file": "~/assistant/persona.md",
    "intro": "Напишіть своїми словами.", "home": "~/assistant", "port": 8190, "web": True,
    "allowed_users": ["Lead@Example.com", "pm@example.com"], "ticket_creators": ["lead@example.com"],
    "expert": "lead@example.com", "model": "sonnet", "effort": "low", "reply_timeout": "10m",
    "claude_bin": "/usr/local/bin/claude", "voice_model": "medium",
    "youtrack": {"project": "NWND", "board": "104-17", "queue_tag": "", "queued_state": "To Do",
                 "type_field": "Type", "types": {"bug": "Bug", "feature": "Feature", "research": "Task"}},
    "ticket_engine": {"bridge": "sudo -n /usr/local/bin/fa-ticket-engine", "timeout": "25m"},
    "slack": {"web_url": "https://console.example.com/assistant/", "read_channels": ["C0AAAAAAA1", "C0AAAAAAA2"],
              "internal_channels": ["C0AAAAAAA1"], "call_names": ["Саша"]},
}
CONTOSO = {
    "enabled": True, "project": "Contoso", "name": "Мирон", "gender": "m", "knowledge_file": "~/assistant/k.md",
    "home": "~/assistant", "port": 8191, "web": False, "ticket_creators": ["lead@example.com"],
    "youtrack": {"project": "SHOP"},
    "slack": {"read_channels": ["C0BBBBBBB1", "C0BBBBBBB2"], "listen_channels": ["C0BBBBBBB2"],
              "partner_channels": ["C0BBBBBBB1"], "creator_channels": ["C0BBBBBBB1"],
              "creator_domain": "example.com", "history_days": 365, "call_names": ["Мироне"]},
}


def write(tmp: Path, section: dict | None) -> Path:
    raw = yaml.safe_load(EXAMPLE.read_text())
    if section is None:
        raw.pop("assistant", None)
    else:
        raw["assistant"] = section
    p = tmp / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True))
    return p


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_the_northwind_shape_loads_from_the_section(home):
    s = Settings.load(write(home, NORTHWIND))
    assert (s.project, s.name, s.gender, s.port) == ("Northwind", "Олександра", "f", 8190)
    assert s.data_dir == home / "assistant" / "data" and s.uploads_dir == home / "assistant" / "uploads"
    assert s.brain.workdir == home / "assistant" / "mirror" and s.brain.timeout_s == 600
    assert s.brain.prompt_file == PROMPT and s.brain.settings_file == SESSION_SETTINGS and PROMPT.is_file()
    assert s.allowed_users == ["lead@example.com", "pm@example.com"]   # e-mails compare in lower case
    assert s.youtrack.url == "https://youtrack.example.com" and s.youtrack.project == "NWND"
    assert s.youtrack.board == "104-17" and s.youtrack.secrets == home / "arceus" / "data" / "assistant.env"
    assert s.ticket_engine.bridge == ["sudo", "-n", "/usr/local/bin/fa-ticket-engine"]   # her Night Shift desk
    assert s.ticket_engine.timeout_s == 1500 and s.brain.auth == "subscription"          # as before
    assert s.slack.read_channels == ("C0AAAAAAA1", "C0AAAAAAA2") and s.slack.call_names == ("Саша",)
    # The factory section of her prompt is this same file: the runner's tracker settings.
    assert s.factory_config == s.config_path == (home / "config.yaml").resolve()


def test_the_contoso_shape_loads_from_the_section(home):
    s = Settings.load(write(home, CONTOSO))
    assert (s.project, s.gender, s.port, s.allowed_users) == ("Contoso", "m", 8191, [])
    assert s.knowledge_file == home / "assistant" / "k.md"
    assert s.slack.listen_channels == ("C0BBBBBBB2",) and s.slack.partner_channels == ("C0BBBBBBB1",)
    assert s.slack.history_days == 365 and s.creators_from_slack
    assert not s.ticket_engine.enabled


def test_no_section_is_an_assistant_that_is_off(home):
    raw = yaml.safe_load(EXAMPLE.read_text())
    assert raw["assistant"]["enabled"] is False        # the example documents the section and leaves it off


@pytest.mark.parametrize("change, words", [
    ({"slack": {"read_channels": ["general"]}}, "Slack channel IDs"),
    ({"tokens": "x"}, "unknown key"),
    ({"slack": {"app_token_file": "~/a"}}, "unknown key"),          # the old way of naming tokens is refused
    ({"web": True, "allowed_users": []}, "allowed_users"),
    ({"slack": {"creator_channels": ["C0BBBBBBB1"]}}, "creator_domain"),
    ({"secrets": "~/arceus/data/secrets.env"}, "file of its own"),
])
def test_a_wrong_section_is_refused_with_the_runners_words(home, change, words):
    with pytest.raises(ConfigError) as e:
        Settings.load(write(home, {**NORTHWIND, **change}))
    assert words in str(e.value)


def test_tokens_come_from_assistant_env_only(home):
    s = Settings.load(write(home, NORTHWIND))
    env = home / "arceus" / "data" / "assistant.env"
    env.parent.mkdir(parents=True)
    assert s.youtrack.token() is None                  # no file: no token, she still drafts
    with pytest.raises(secrets.SecretsError, match="SLACK_APP_TOKEN, SLACK_BOT_TOKEN not set"):
        s.slack.tokens()
    env.write_text("SLACK_APP_TOKEN=xapp-1\nSLACK_BOT_TOKEN=xoxb-2\nYOUTRACK_TOKEN=perm:3\n")
    env.chmod(0o644)
    with pytest.raises(secrets.SecretsError, match="must be 600") as e:
        s.slack.tokens()
    assert "xapp" not in str(e.value) and "xoxb" not in str(e.value)
    assert s.youtrack.token() is None                  # a file others can read is not used
    env.chmod(0o600)
    assert s.slack.tokens() == ("xapp-1", "xoxb-2") and s.youtrack.token() == "perm:3"
    env.write_text("SLACK_APP_TOKEN=xapp-new\nSLACK_BOT_TOKEN=xoxb-2\nYOUTRACK_TOKEN=perm:4\n")
    assert s.youtrack.token() == "perm:4"              # read at every use: a replaced token applies at once
    assert "xapp" not in (home / "config.yaml").read_text()
    assert not any(v.startswith(("xapp", "xoxb", "perm:")) for v in os.environ.values())


def test_who_pays_and_whom_she_names_come_from_the_section(home):
    s = Settings.load(write(home, {**NORTHWIND, "expert_name": "Олени Петрівни", "auth": "api_key"}))
    assert s.brain.auth == "api_key" and s.ticket_engine.enabled and s.brain.secrets == home / "arceus" / "data" / "assistant.env"
    assert s.brain.claude_home == home / "assistant" / "data" / "claude"   # in the folder the session may not read
    assert s.expert_name == "Олени Петрівни"
    personal = {"enabled": True, "allowed_users": ["lead@example.com"], "auth": "subscription",
                "ticket_engine": {"bridge": "sudo -n /usr/local/bin/fa-ticket-engine"}}
    s = Settings.load(write(home, personal))
    assert s.brain.auth == "subscription" and s.ticket_engine.bridge == ["sudo", "-n", "/usr/local/bin/fa-ticket-engine"]


def test_the_slack_manifest_is_this_projects_assistant(home):
    from assistant.server import slack_manifest
    text = slack_manifest(Settings.load(write(home, {**NORTHWIND, "name": 'Олена "Ліза"'})))
    raw = yaml.safe_load(text)
    assert raw["display_information"]["name"] == 'Олена "Ліза"' and raw["features"]["bot_user"]["display_name"] == 'Олена "Ліза"'
    assert "Northwind" in raw["display_information"]["description"] and "@NAME@" not in text and "@PROJECT@" not in text
    assert raw["settings"]["socket_mode_enabled"] is True
