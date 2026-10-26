from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from arceus_runner.cli import EXIT_CHECK_FAILED, EXIT_CONFIG, EXIT_OK, main
from arceus_runner.config import ConfigError, load_config, load_secrets, parse_duration

from .conftest import EXAMPLE, example_raw


def write(tmp_path: Path, raw: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True))
    return p


def write_secrets(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    p = tmp_path / "secrets.env"
    p.write_text(text)
    p.chmod(mode)
    return p


def test_example_config_is_valid():
    cfg = load_config(EXAMPLE)
    assert cfg.mode == "observe"
    assert cfg.schedule.poll_working == timedelta(minutes=2)
    assert cfg.limits.needs_input_fail_after == timedelta(days=5)
    assert cfg.gitlab.exclude_repos == []


@pytest.mark.parametrize("raw,expected", [("30s", 30), ("2m", 120), ("6h", 21600), ("5d", 432000)])
def test_durations(raw, expected):
    assert parse_duration(raw).total_seconds() == expected


@pytest.mark.parametrize("bad", ["2", "2 minutes", "-1m", "1w", ""])
def test_bad_durations(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_four_mistakes_give_four_errors(tmp_path, capsys):
    raw = example_raw()
    raw["polling"] = "2m"                                   # unknown key
    raw["schedule"]["timezone"] = "Mars/Base"               # bad zone
    del raw["youtrack"]["states"]["in_progress"]             # missing state
    raw["gitlab"]["git_author"]["email"] = "not-an-email"   # bad email
    path = write(tmp_path, raw)

    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert sorted(e.value.problems) == sorted([
        "polling: unknown key (typo?)",
        "schedule.timezone: unknown time zone 'Mars/Base'",
        "youtrack.states.in_progress: required",
        "gitlab.git_author.email: String should match pattern '^[^@\\s]+@[^@\\s]+$'",
    ])

    rc = main(["--config", str(path), "check", "--offline"])
    err = capsys.readouterr().err
    assert rc == EXIT_CONFIG
    assert len(err.strip().splitlines()) == 4
    assert "Traceback" not in err


def test_cross_field_rules(tmp_path):
    raw = example_raw()
    raw["youtrack"]["allowed_users"].append(raw["youtrack"]["bot_login"])
    with pytest.raises(ConfigError, match="must not contain the bot"):
        load_config(write(tmp_path, raw))

    raw = example_raw()
    raw["gitlab"]["default_repo"] = "acme-theme"  # gone: the session starts in the workspace folder
    with pytest.raises(ConfigError, match="gitlab.default_repo is no longer used: .*remove this line"):
        load_config(write(tmp_path, raw))

    raw = example_raw()
    raw["schedule"]["working_hours"]["from"] = "20:00"
    with pytest.raises(ConfigError, match="'from' must be earlier than 'to'"):
        load_config(write(tmp_path, raw))


def test_secrets_from_file_not_environment(tmp_path):
    raw = example_raw()
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a1\nGITLAB_TOKEN='g1'\nSLACK_BOT_TOKEN=s1\n"))}
    cfg = load_config(write(tmp_path, raw))
    s = load_secrets(cfg, environ={})
    assert (s.youtrack, s.gitlab, s.slack) == ("a1", "g1", "s1")
    assert "a1" not in repr(s)


def test_missing_secret_names_the_variable(tmp_path):
    raw = example_raw()
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a1\n"))}
    cfg = load_config(write(tmp_path, raw))
    with pytest.raises(ConfigError) as e:
        load_secrets(cfg, environ={})
    assert e.value.problems == [
        f"gitlab.token_env: GITLAB_TOKEN is not set in {tmp_path / 'secrets.env'} or the environment",
        f"slack.token_env: SLACK_BOT_TOKEN is not set in {tmp_path / 'secrets.env'} or the environment",
    ]


def test_world_readable_secrets_rejected(tmp_path):
    raw = example_raw()
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a\nGITLAB_TOKEN=b\nSLACK_BOT_TOKEN=c\n",
                                                 mode=0o644))}
    cfg = load_config(write(tmp_path, raw))
    with pytest.raises(ConfigError, match="permissions are 644, must be 600"):
        load_secrets(cfg, environ={})


def test_live_mode_is_refused_without_the_engine_contract(tmp_path, capsys):
    raw = example_raw()
    raw["mode"] = "live"
    raw["paths"] = {"bulava": str(tmp_path / "no-engine")}
    raw["paths"]["secrets"] = str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a\nGITLAB_TOKEN=b\nSLACK_BOT_TOKEN=c\n"))
    path = write(tmp_path, raw)

    assert main(["--config", str(path), "check", "--offline"]) == EXIT_CHECK_FAILED
    out = capsys.readouterr().out
    assert "[FAIL] config.mode: mode: live needs the Bulava server contract" in out
    assert "no-engine does not exist" in out

    assert main(["--config", str(path), "run", "--once"]) == EXIT_CONFIG
    assert "mode: live needs the Bulava server contract" in capsys.readouterr().err


def test_offline_check_passes_on_example(tmp_path):
    raw = example_raw()
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a\nGITLAB_TOKEN=b\nSLACK_BOT_TOKEN=c\n"))}
    assert main(["--config", str(write(tmp_path, raw)), "check", "--offline"]) == EXIT_OK


def test_token_variable_names_need_no_config(tmp_path):
    raw = example_raw()
    assert "token_env" not in raw["youtrack"] and "token_env" not in raw["gitlab"]
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a1\nGITLAB_TOKEN=g1\nSLACK_BOT_TOKEN=s1\n"))}
    cfg = load_config(write(tmp_path, raw))
    assert (cfg.youtrack.token_env, cfg.gitlab.token_env, cfg.slack.token_env) == (
        "YOUTRACK_TOKEN", "GITLAB_TOKEN", "SLACK_BOT_TOKEN")
    assert load_secrets(cfg, environ={}).youtrack == "a1"


def test_an_older_config_naming_token_env_still_loads(tmp_path):
    raw = example_raw()
    raw["youtrack"]["token_env"] = "YT_BOT"
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YT_BOT=a1\nGITLAB_TOKEN=g1\nSLACK_BOT_TOKEN=s1\n"))}
    assert load_secrets(load_config(write(tmp_path, raw)), environ={}).youtrack == "a1"


def test_repository_mapping_keys_are_refused_with_what_to_do(tmp_path):
    raw = example_raw()
    raw["youtrack"]["repo_field"] = "Repository"
    raw["youtrack"]["repo_map"] = {"Platform": "acme-platform"}
    raw["bulava"]["multi_repo_tasks"] = False
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, raw))
    text = "\n".join(e.value.problems)
    assert "youtrack.repo_field is no longer used: the agent works across all repositories" in text
    assert "bulava.multi_repo_tasks is no longer used" in text


def test_models_default_to_auto_and_pass_nothing():
    b = load_config(EXAMPLE).bulava
    assert (b.claude_model, b.claude_effort, b.codex_model, b.codex_effort) == ("auto",) * 4
    assert b.model_env() == {}


def test_chosen_models_become_the_engine_variables(tmp_path):
    raw = example_raw()
    raw["bulava"].update(claude_model="opus", claude_effort="none", codex_model="gpt-6.1-sol", codex_effort="ultra")
    b = load_config(write(tmp_path, raw)).bulava
    assert b.model_env() == {"SUPERVISOR_CLAUDE_MODEL": "opus", "SUPERVISOR_CLAUDE_EFFORT": "none",
                             "SUPERVISOR_CODEX_MODEL": "gpt-6.1-sol", "SUPERVISOR_CODEX_EFFORT": "ultra"}


@pytest.mark.parametrize("key,value", [
    ("claude_model", "opus; rm -rf ~"), ("claude_model", "--dangerously-skip-permissions"), ("codex_model", ""),
    ("claude_effort", "ultra"), ("codex_effort", "ultracode"), ("codex_effort", "none"), ("claude_effort", ""),
])
def test_a_model_or_depth_the_engine_would_not_take_is_refused(tmp_path, key, value):
    raw = example_raw()
    raw["bulava"][key] = value
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, raw))
    assert [p.split(":")[0] for p in e.value.problems] == [f"bulava.{key}"]


def _engine(tmp_path: Path, config_sh: str, night_shift: str) -> Path:
    root = tmp_path / "engine"
    (root / "bin").mkdir(parents=True)
    (root / "supervisor").mkdir()
    (root / "supervisor" / "config.sh").write_text(config_sh)
    for name in ("dispatch.sh", "worker-send.sh", "night-shift.sh"):
        f = root / "bin" / name
        f.write_text(night_shift if name == "night-shift.sh" else "#!/bin/sh\n")
        f.chmod(0o755)
    return root


def test_a_chosen_model_needs_an_engine_that_takes_it(tmp_path, monkeypatch):
    from arceus_runner import bulava
    from arceus_runner.cli import models_check
    from arceus_runner.loop import LiveModeUnavailable, require_mode_ready
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])
    old = bulava.probe(_engine(tmp_path, "# SUPERVISOR_CLAUDE_MODEL only in a comment\n", "#!/bin/sh\n"))
    raw = example_raw()
    assert models_check(load_config(EXAMPLE), old).status == "ok"  # auto asks nothing of the engine
    raw["bulava"]["claude_effort"] = "xhigh"
    cfg = load_config(write(tmp_path, raw))
    c = models_check(cfg, old)
    assert c.status == "fail" and "NIGHT_SHIFT_SETTINGS=off" in c.detail and "Claude auto·xhigh" in c.detail
    live = cfg.model_copy(update={"mode": "live"})
    with pytest.raises(LiveModeUnavailable, match=r"SUPERVISOR_\{CLAUDE,CODEX\}"):
        require_mode_ready(live, old)

    new = bulava.probe(_engine(tmp_path / "new", ': "${SUPERVISOR_CLAUDE_MODEL:=}"\n',
                               '#!/bin/sh\ncase "${NIGHT_SHIFT_SETTINGS:-}" in off) ;; esac\n'))
    assert new.missing_for_models() == [] and models_check(cfg, new).status == "ok"
