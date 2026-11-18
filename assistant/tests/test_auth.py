"""Who pays for her Claude: the team's key from assistant.env reaches Claude and nothing else of this process's
does; without the key no Claude starts, and nothing falls back to anyone's own sign-in."""

import asyncio
import json
from pathlib import Path

from assistant import brain
from assistant.brain import BAD_KEY, NO_KEY, BrainConfig, error_words
from tests.test_chat import keyed

# What the parent process might carry: a subscription token, someone's own key, the Slack tokens.
PARENT = {"PATH": "/usr/bin:/bin", "HOME": "/home/factory", "LANG": "uk_UA.UTF-8",
          "ANTHROPIC_API_KEY": "sk-ant-parent", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-owner",
          "ANTHROPIC_AUTH_TOKEN": "bearer-x", "SLACK_BOT_TOKEN": "xoxb-1", "CLAUDE_CONFIG_DIR": "/home/factory/.claude"}


def recording_claude(tmp: Path) -> Path:
    """A stand-in claude that writes down the environment it got, then answers like `claude -p` does."""
    fake = tmp / "claude"
    fake.write_text("#!/usr/bin/env python3\nimport json, os, sys\nsys.stdin.read()\n"
                    f"json.dump(dict(os.environ), open({str(tmp / 'env.json')!r}, 'w'))\n"
                    "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'так', 'session_id': 's'}))\n")
    fake.chmod(0o755)
    return fake


def turn(cfg: BrainConfig) -> list:
    async def go():
        return [e async for e in brain.run_turn(cfg, "hi", None, "s")]
    return asyncio.run(go())


def test_the_team_key_from_assistant_env_is_all_claude_gets(tmp_path, monkeypatch):
    for k, v in PARENT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    c = BrainConfig(claude_bin=str(recording_claude(tmp_path)), workdir=tmp_path, prompt_file=tmp_path / "p",
                    settings_file=tmp_path / "s", **keyed(tmp_path, "sk-ant-team-1"))
    evs = turn(c)
    assert evs[-1].kind == "done"
    env = json.loads((tmp_path / "env.json").read_text())
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-team-1"
    for gone in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "SLACK_BOT_TOKEN"):
        assert gone not in env
    # Its own home: no stored sign-in of the run user's can win over the key, or stand in when the key is gone.
    assert env["HOME"] == str(tmp_path / "claude-home") and env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-home/.claude")
    assert (tmp_path / "claude-home").stat().st_mode & 0o777 == 0o700
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1" and env["LANG"] == "uk_UA.UTF-8"


def test_a_replaced_key_is_used_on_the_next_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    c = BrainConfig(claude_bin=str(recording_claude(tmp_path)), workdir=tmp_path, prompt_file=tmp_path / "p",
                    settings_file=tmp_path / "s", **keyed(tmp_path, "sk-ant-old"))
    turn(c)
    (tmp_path / "assistant.env").write_text("ANTHROPIC_API_KEY=sk-ant-new\n")
    turn(c)
    assert json.loads((tmp_path / "env.json").read_text())["ANTHROPIC_API_KEY"] == "sk-ant-new"


def test_without_the_key_no_claude_starts_and_she_says_why(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-owner")   # the fallback that must never happen
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-parent")
    fake = recording_claude(tmp_path)
    kw = keyed(tmp_path)
    (tmp_path / "assistant.env").write_text("# no key yet\n")
    for cfg in (BrainConfig(str(fake), tmp_path, tmp_path / "p", tmp_path / "s", **kw),
                BrainConfig(str(fake), tmp_path, tmp_path / "p", tmp_path / "s")):   # nothing configured at all
        evs = turn(cfg)
        assert [e.kind for e in evs] == ["error"] and evs[0].text == NO_KEY
    assert not (tmp_path / "env.json").exists()
    (tmp_path / "assistant.env").write_text("ANTHROPIC_API_KEY=sk-ant-x\n")
    (tmp_path / "assistant.env").chmod(0o644)                              # readable by others: not used
    assert turn(BrainConfig(str(fake), tmp_path, tmp_path / "p", tmp_path / "s", **kw))[0].text == NO_KEY
    assert not (tmp_path / "env.json").exists()


def test_a_refused_key_is_said_as_such_never_as_a_sign_in_to_fix_by_logging_in():
    refused = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
               "result": "Invalid API key · Fix external API key"}
    assert error_words(refused) == BAD_KEY
    assert "sk-ant" not in BAD_KEY


def test_subscription_is_the_developers_own_sign_in_as_before_and_never_an_api_key(tmp_path, monkeypatch):
    for k, v in PARENT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    c = BrainConfig(claude_bin=str(recording_claude(tmp_path)), workdir=tmp_path, prompt_file=tmp_path / "p",
                    settings_file=tmp_path / "s", auth="subscription")
    assert turn(c)[-1].kind == "done"
    env = json.loads((tmp_path / "env.json").read_text())
    # Their HOME (the stored sign-in) and their setup-token, if the service was given one: that is the subscription.
    assert env["HOME"] == "/home/factory" and env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-owner"
    for gone in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SLACK_BOT_TOKEN", "CLAUDE_CONFIG_DIR"):
        assert gone not in env
