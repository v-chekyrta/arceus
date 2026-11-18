"""`arceus-runner assistant import`: the old ~/assistant setup becomes config.yaml's section and assistant.env, with
nothing else of config.yaml touched and nothing of the conversations moved."""

from pathlib import Path

import pytest
import yaml

from arceus_runner import assistant_import
from arceus_runner.cli import main
from arceus_runner.config import load_config

from .conftest import EXAMPLE


@pytest.fixture
def factory(tmp_path, monkeypatch):
    """A run user's home with the old assistant (as on the factory, made-up people) and the runner's config.yaml."""
    monkeypatch.setenv("HOME", str(tmp_path))
    old = tmp_path / "assistant"
    for d in ("app/deploy", "secrets", "data", "uploads", "mirror", "knowledge"):
        (old / d).mkdir(parents=True)
    (old / "app/deploy/persona.md").write_text("Олександра: хто вона.")
    (old / "data/assistant.db").write_text("conversations")
    (old / "secrets/slack-app-token").write_text("xapp-sentinel-app\n")
    (old / "secrets/slack-bot-token").write_text("xoxb-sentinel-bot\n")
    (old / "secrets/youtrack.token").write_text("YOUTRACK_TOKEN=perm:sentinel-yt\n")
    for f in (old / "secrets").iterdir():
        f.chmod(0o600)
    (old / "config.yaml").write_text("""\
# ~/assistant/config.yaml on the factory VM
project: Northwind
persona_name: Олександра Бондар
persona_file: ~/assistant/app/deploy/persona.md   # character and legend
persona_gender: f
persona_intro: "Напишіть своїми словами,
  що просить клієнт."
data_dir: ~/assistant/data
allowed_users: [Lead@Example.com, pm@example.com]
brain:
  claude_bin: /usr/local/bin/claude
  workdir: ~/assistant/mirror
  prompt_file: ~/assistant/app/deploy/prompt.md
  settings_file: ~/assistant/app/deploy/session-settings.json
  model: sonnet
  effort: low
  timeout_s: 600
transcribe:
  model: medium
youtrack:
  url: https://youtrack.example.com
  project: NWND
  token_file: ~/assistant/secrets/youtrack.token
  queue_tag: ""
  board: "104-17"
  queued_state: To Do
  type_field: Type
  types: {bug: Bug, feature: Feature, research: Task}
factory:
  config: ~/arceus/data/config.yaml
ticket_engine:
  bridge: sudo -n /usr/local/bin/fa-ticket-engine
  timeout_s: 1500
slack:
  app_token_file: ~/assistant/secrets/slack-app-token
  bot_token_file: ~/assistant/secrets/slack-bot-token
  web_url: https://console.example.com/assistant/
  read_channels: [C0AAAAAAA1, C0AAAAAAA2]
  internal_channels: [C0AAAAAAA1]
  call_names: [Александра, Саша]
ticket_creators: [lead@example.com]
expert: lead@example.com
""")
    data = tmp_path / "arceus" / "data"
    data.mkdir(parents=True)
    raw = yaml.safe_load(EXAMPLE.read_text())
    raw.pop("assistant")
    text = "# the factory's own comment, kept as it is\n" + yaml.safe_dump(raw, allow_unicode=True)
    (data / "config.yaml").write_text(text)
    (data / "config.yaml").chmod(0o640)
    return tmp_path


def run(home: Path, *extra: str) -> int:
    return main(["--config", str(home / "arceus/data/config.yaml"), "assistant", "import",
                 str(home / "assistant/config.yaml"), *extra])


def test_without_apply_nothing_is_written_and_no_token_is_printed(factory, capsys):
    before = (factory / "arceus/data/config.yaml").read_bytes()
    assert run(factory) == 0
    out = capsys.readouterr().out
    assert "Nothing written" in out and "assistant:" in out and "adds YOUTRACK_TOKEN, SLACK_APP_TOKEN" in out
    assert "api_key" not in out and "ticket_engine is not carried over" not in out   # her subscription and desk, as before
    assert "sentinel" not in out
    assert (factory / "arceus/data/config.yaml").read_bytes() == before
    assert not (factory / "arceus/data/assistant.env").exists() and not (factory / "assistant/persona.md").exists()


def test_apply_appends_the_section_keeps_every_other_byte_and_moves_the_tokens(factory, capsys):
    cfg_path = factory / "arceus/data/config.yaml"
    before = cfg_path.read_bytes()
    assert run(factory, "--apply") == 0
    after = cfg_path.read_bytes()
    assert after.startswith(before) and cfg_path.stat().st_mode & 0o777 == 0o640
    assert b"sentinel" not in after and b"token_file" not in after
    a = load_config(cfg_path).assistant
    assert a.enabled and a.project == "Northwind" and a.name == "Олександра Бондар"
    assert a.home == factory / "assistant" and a.port == 8190 and a.web
    assert a.allowed_users == ["lead@example.com", "pm@example.com"] and a.expert == "lead@example.com"
    assert a.reply_timeout.total_seconds() == 600 and a.claude_bin == "/usr/local/bin/claude"
    assert a.youtrack.project == "NWND" and a.youtrack.board == "104-17"
    # As before: the developer's own subscription, and her Night Shift ticket desk.
    assert a.auth == "subscription" and a.ticket_engine.bridge == "sudo -n /usr/local/bin/fa-ticket-engine"
    assert a.slack.internal_channels == ["C0AAAAAAA1"] and a.slack.call_names == ["Александра", "Саша"]
    # The persona leaves the old code folder; the conversations stay where they are.
    assert a.persona_file == factory / "assistant/persona.md"
    assert a.persona_file.read_text() == "Олександра: хто вона."
    assert (factory / "assistant/data/assistant.db").read_text() == "conversations"
    env = factory / "arceus/data/assistant.env"
    assert env.stat().st_mode & 0o777 == 0o600
    assert {line.split("=", 1)[0]: line.split("=", 1)[1] for line in env.read_text().splitlines()
            if "=" in line and not line.startswith("#")} == {
        "YOUTRACK_TOKEN": "perm:sentinel-yt", "SLACK_APP_TOKEN": "xapp-sentinel-app", "SLACK_BOT_TOKEN": "xoxb-sentinel-bot"}
    assert "sentinel" not in capsys.readouterr().out
    # Once there, the section is never imported over.
    assert run(factory, "--apply") == 1
    assert cfg_path.read_bytes() == after


def test_a_token_assistant_env_already_has_is_kept(factory):
    env = factory / "arceus/data/assistant.env"
    env.write_text("SLACK_BOT_TOKEN=xoxb-newer\nANTHROPIC_API_KEY=sk-ant-x\n")
    env.chmod(0o600)
    assert run(factory, "--apply") == 0
    text = env.read_text()
    assert "xoxb-newer" in text and "xoxb-sentinel-bot" not in text and "ANTHROPIC_API_KEY=sk-ant-x" in text
    assert "SLACK_APP_TOKEN=xapp-sentinel-app" in text


def test_the_slack_only_project_on_its_own_port(factory):
    assert run(factory, "--port", "8191", "--no-web", "--apply") == 0
    a = load_config(factory / "arceus/data/config.yaml").assistant
    assert a.port == 8191 and not a.web


@pytest.mark.parametrize("edit, words", [
    (("data_dir: ~/assistant/data", "data_dir: ~/elsewhere/db"), "<home>/data"),
    (("url: https://youtrack.example.com", "url: https://other.example.com"), "not this factory's tracker"),
    (("expert: lead@example.com", "expert: lead@example.com\nmystery: 1"), "mystery"),
    (("read_channels: [C0AAAAAAA1, C0AAAAAAA2]", "read_channels: [general]"), "Slack channel IDs"),
])
def test_what_cannot_be_carried_over_is_said_and_nothing_is_written(factory, capsys, edit, words):
    old = factory / "assistant/config.yaml"
    old.write_text(old.read_text().replace(*edit))
    before = (factory / "arceus/data/config.yaml").read_bytes()
    assert run(factory, "--apply") == 1
    assert words in capsys.readouterr().out
    assert (factory / "arceus/data/config.yaml").read_bytes() == before
    assert not (factory / "arceus/data/assistant.env").exists()


def test_plan_names_every_old_key_it_knows():
    """A key of the old config this import does not carry is refused, never dropped unseen."""
    assert {"slack", "youtrack", "brain", "ticket_engine", "persona_file"} <= assistant_import.KNOWN


def test_one_persons_own_assistant_keeps_its_subscription_and_its_desk(factory):
    old = factory / "assistant/config.yaml"
    text = old.read_text()
    text = text[:text.index("slack:\n")] + "ticket_creators: [lead@example.com]\nexpert: lead@example.com\n"
    old.write_text(text.replace("allowed_users: [Lead@Example.com, pm@example.com]", "allowed_users: [lead@example.com]"))
    assert run(factory, "--apply") == 0
    a = load_config(factory / "arceus/data/config.yaml").assistant
    assert a.auth == "subscription" and a.ticket_engine.bridge == "sudo -n /usr/local/bin/fa-ticket-engine"
