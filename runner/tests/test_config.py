from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from arceus_runner.cli import EXIT_CHECK_FAILED, EXIT_CONFIG, EXIT_OK, main
from arceus_runner.config import ConfigError, load_config, load_secrets, parse_duration
from arceus_runner.health import autostart  # the real one: conftest stands in for it in every other test

from .conftest import EXAMPLE, example_raw

HOOK = "https://hooks.slack.com/services/T0ACME/B0ACME/XXXXsecretXXXX"
SLACK_HOOK = f"SLACK_WEBHOOK_URL={HOOK}\n"



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
    assert cfg.code_host.exclude_repos == []
    assert (cfg.tracker.type, cfg.code_host.type) == ("youtrack", "gitlab")


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
    del raw["tracker"]["states"]["in_progress"]              # missing state
    raw["code_host"]["git_author"]["email"] = "not-an-email"  # bad email
    path = write(tmp_path, raw)

    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert sorted(e.value.problems) == sorted([
        "polling: unknown key (typo?)",
        "schedule.timezone: unknown time zone 'Mars/Base'",
        "tracker.states.in_progress: required",
        "code_host.git_author.email: String should match pattern '^[^@\\s]+@[^@\\s]+$'",
    ])

    rc = main(["--config", str(path), "check", "--offline"])
    err = capsys.readouterr().err
    assert rc == EXIT_CONFIG
    assert len(err.strip().splitlines()) == 4
    assert "Traceback" not in err


def test_cross_field_rules(tmp_path):
    raw = example_raw()
    raw["tracker"]["allowed_users"].append(raw["tracker"]["bot_user"])
    with pytest.raises(ConfigError, match="must not contain the bot"):
        load_config(write(tmp_path, raw))

    raw = example_raw()
    raw["code_host"]["default_repo"] = "acme-theme"  # gone: the session starts in the workspace folder
    with pytest.raises(ConfigError, match="code_host.default_repo is no longer used: .*remove this line"):
        load_config(write(tmp_path, raw))

    raw = example_raw()
    raw["schedule"]["working_hours"]["from"] = "20:00"
    with pytest.raises(ConfigError, match="'from' must be earlier than 'to'"):
        load_config(write(tmp_path, raw))


def test_secrets_from_file_not_environment(tmp_path):
    raw = example_raw()
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a1\nGITLAB_TOKEN='g1'\n" + SLACK_HOOK))}
    cfg = load_config(write(tmp_path, raw))
    s = load_secrets(cfg, environ={})
    assert (s.tracker, s.code_host, s.slack, s.slack_webhook) == ("a1", "g1", None, HOOK)
    assert "a1" not in repr(s)
    # The webhook URL is a secret, and so is its last part alone (error texts may quote a path).
    assert HOOK in s.values() and "XXXXsecretXXXX" in s.values()


def test_missing_secret_names_the_variable(tmp_path):
    raw = example_raw()
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a1\n"))}
    cfg = load_config(write(tmp_path, raw))
    with pytest.raises(ConfigError) as e:
        load_secrets(cfg, environ={})
    assert e.value.problems == [
        f"code_host.token_env: GITLAB_TOKEN is not set in {tmp_path / 'secrets.env'} or the environment",
        f"slack.webhook_env: SLACK_WEBHOOK_URL is not set in {tmp_path / 'secrets.env'} or the environment",
    ]


def test_the_bot_token_is_needed_only_with_alerts_channel(tmp_path):
    raw = example_raw()
    raw["slack"] = {"alerts_channel": "#acme"}
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a1\nGITLAB_TOKEN=g1\n"))}
    with pytest.raises(ConfigError, match="slack.token_env: SLACK_BOT_TOKEN is not set"):
        load_secrets(load_config(write(tmp_path, raw)), environ={})
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a1\nGITLAB_TOKEN=g1\nSLACK_BOT_TOKEN=s1\n"))}
    s = load_secrets(load_config(write(tmp_path, raw)), environ={})
    assert (s.slack, s.slack_webhook) == ("s1", None)


def test_slack_webhook_must_be_an_incoming_webhook(tmp_path):
    raw = example_raw()
    for url in ("https://hooks.slack.com/triggers/T0/1/abc", "http://hooks.slack.com/services/T0/B0/x",
                "https://example.com/services/T0/B0/x"):
        raw["paths"] = {"secrets": str(write_secrets(tmp_path, f"YOUTRACK_TOKEN=a\nGITLAB_TOKEN=b\nSLACK_WEBHOOK_URL={url}\n"))}
        with pytest.raises(ConfigError) as e:
            load_secrets(load_config(write(tmp_path, raw)), environ={})
        assert "is not a Slack incoming webhook" in str(e.value)
        assert url not in str(e.value)  # the URL is the credential: never on a terminal


def test_slack_section_needs_a_webhook_or_a_channel_and_member_ids(tmp_path):
    raw = example_raw()
    raw["slack"] = {"people": {}}
    with pytest.raises(ConfigError, match="needs webhook_env"):
        load_config(write(tmp_path, raw))
    raw["slack"] = {"webhook_env": "SLACK_WEBHOOK_URL", "people": {"j.doe": "jdoe"}}
    with pytest.raises(ConfigError, match="Slack member IDs look like U0123ABCD"):
        load_config(write(tmp_path, raw))


def test_world_readable_secrets_rejected(tmp_path):
    raw = example_raw()
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a\nGITLAB_TOKEN=b\n" + SLACK_HOOK,
                                                 mode=0o644))}
    cfg = load_config(write(tmp_path, raw))
    with pytest.raises(ConfigError, match="permissions are 644, must be 600"):
        load_secrets(cfg, environ={})


def test_live_mode_is_refused_without_the_engine_contract(tmp_path, capsys):
    raw = example_raw()
    raw["mode"] = "live"
    raw["paths"] = {"bulava": str(tmp_path / "no-engine")}
    raw["paths"]["secrets"] = str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a\nGITLAB_TOKEN=b\n" + SLACK_HOOK))
    path = write(tmp_path, raw)

    assert main(["--config", str(path), "check", "--offline"]) == EXIT_CHECK_FAILED
    out = capsys.readouterr().out
    assert "[FAIL] config.mode: mode: live needs the Bulava server contract" in out
    assert "no-engine does not exist" in out

    assert main(["--config", str(path), "run", "--once"]) == EXIT_CONFIG
    assert "mode: live needs the Bulava server contract" in capsys.readouterr().err


def test_offline_check_passes_on_example(tmp_path):
    raw = example_raw()
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a\nGITLAB_TOKEN=b\n" + SLACK_HOOK))}
    assert main(["--config", str(write(tmp_path, raw)), "check", "--offline"]) == EXIT_OK


def test_token_variable_names_need_no_config(tmp_path):
    raw = example_raw()
    assert "token_env" not in raw["tracker"] and "token_env" not in raw["code_host"]
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YOUTRACK_TOKEN=a1\nGITLAB_TOKEN=g1\n" + SLACK_HOOK))}
    cfg = load_config(write(tmp_path, raw))
    assert (cfg.tracker.token_env, cfg.code_host.token_env, cfg.slack.token_env) == (
        "YOUTRACK_TOKEN", "GITLAB_TOKEN", "SLACK_BOT_TOKEN")
    assert load_secrets(cfg, environ={}).tracker == "a1"


def test_an_older_config_naming_token_env_still_loads(tmp_path):
    raw = example_raw()
    raw["tracker"]["token_env"] = "YT_BOT"
    raw["paths"] = {"secrets": str(write_secrets(tmp_path, "YT_BOT=a1\nGITLAB_TOKEN=g1\n" + SLACK_HOOK))}
    assert load_secrets(load_config(write(tmp_path, raw)), environ={}).tracker == "a1"


def test_repository_mapping_keys_are_refused_with_what_to_do(tmp_path):
    raw = example_raw()
    raw["tracker"]["repo_field"] = "Repository"
    raw["tracker"]["repo_map"] = {"Platform": "acme-platform"}
    raw["bulava"]["multi_repo_tasks"] = False
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, raw))
    text = "\n".join(e.value.problems)
    assert "tracker.repo_field is no longer used: the agent works across all repositories" in text
    assert "bulava.multi_repo_tasks is no longer used" in text


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


def test_the_claude_checks_bulava_removed_are_refused_by_name(tmp_path):
    """Bulava 1.19.2 removed the Claude checks before Codex. A config.yaml that still sets them is told
    to delete the block, not that it has a typo; any other unknown key stays a typo."""
    raw = example_raw()
    raw["bulava"]["claude_checks"] = {"enabled": True, "rounds": 1}
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, raw))
    [problem] = e.value.problems
    assert "bulava.claude_checks is no longer used" in problem and "remove this block" in problem
    raw = example_raw()
    raw["bulava"]["claude_check"] = {"enabled": True}
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, raw))
    assert e.value.problems == ["bulava.claude_check: unknown key (typo?)"]


def test_the_engine_is_given_no_claude_check_settings(tmp_path):
    """Nothing the runner starts the engine with names the removed checks, a bulava.env that still has
    them included: only the tokens and the proxy pass from there."""
    from arceus_runner.engine import Engine
    env_file = tmp_path / "bulava.env"
    env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-test\nSUPERVISOR_CLAUDE_CHECKS=1\n"
                        "SUPERVISOR_CLAUDE_CHECKS_REQUIRED=1\n")
    env_file.chmod(0o600)
    cfg = load_config(EXAMPLE)
    cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"bulava_env": env_file})})
    env = Engine(cfg).env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"
    assert not [k for k in env if k.startswith("SUPERVISOR_CLAUDE_CHECKS")], sorted(env)


def test_the_engine_command_for_the_old_check_settings_is_gone(capsys):
    with pytest.raises(SystemExit):
        main(["--config", str(EXAMPLE), "engine-env"])


def test_an_engine_that_still_runs_claude_checks_is_a_warning(tmp_path):
    """Bulava 1.19.2 reads its removed checks as ok and removed. An engine that says they are on is
    older than the pinned one and would still run them before Codex."""
    from types import SimpleNamespace
    from arceus_runner.health import engine_doctor
    cfg = load_config(EXAMPLE)

    def doctor(*rows):
        engine = SimpleNamespace(cfg=cfg, doctor=lambda: {"checks": list(rows)})
        return {c.name: c for c in engine_doctor(engine)}

    removed = {"name": "engine.claude_checks", "status": "ok", "detail": {"enabled": False, "removed": True}}
    c = doctor(removed)["bulava.engine.claude_checks"]
    assert c.status == "ok" and "removed" in c.detail
    old = {"name": "engine.claude_checks", "status": "ok",
           "detail": {"enabled": True, "list": ["code", "security"], "min_severity": "high", "rounds": "1",
                      "required": False}}
    c = doctor(old)["bulava.engine.claude_checks"]
    assert c.status == "warn" and "install the pinned one (setup.sh)" in c.detail
    failing = {**removed, "status": "fail"}
    assert doctor(failing)["bulava.engine.claude_checks"].status == "fail"
    unknown = {**removed, "status": "unexpected"}                                          # a status not understood
    assert doctor(unknown)["bulava.engine.claude_checks"].status == "warn"


def test_a_service_that_would_not_come_back_after_a_reboot_is_named(monkeypatch):
    from subprocess import CompletedProcess
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")

    def systemctl(**states):
        # systemctl is-enabled exits 0 for enabled-runtime too: only its word tells the two apart.
        def run(argv, **kw):
            state = states[argv[-1].replace("-", "_")]
            return CompletedProcess(argv, 0 if state.startswith("enabled") else 1, state + "\n", "")
        return run

    [c] = autostart(run=systemctl(bulava_engine="enabled", runner="disabled"))
    assert c.status == "warn"
    assert c.detail == "runner is disabled: it will not start after a reboot (sudo systemctl enable runner)"
    [c] = autostart(run=systemctl(bulava_engine="enabled-runtime", runner="enabled"))
    assert c.status == "warn" and c.detail.startswith("bulava-engine is enabled-runtime")
    [c] = autostart(run=systemctl(bulava_engine="enabled", runner="not-found"))
    assert c.status == "warn" and c.detail.startswith("runner is not-found")
    [c] = autostart(run=systemctl(bulava_engine="enabled", runner="enabled"))
    assert c.status == "ok"
    assert autostart(run=systemctl(bulava_engine="not-found", runner="not-found")) == []   # a developer's machine
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: None)
    assert autostart(run=systemctl(bulava_engine="disabled", runner="disabled")) == []


def test_slack_notify_idle_repeat_is_no_longer_a_setting(tmp_path):
    raw = example_raw()
    raw["slack"].setdefault("notify", {})["idle_repeat"] = "2h"
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, raw))
    assert e.value.problems == ["slack.notify.idle_repeat: unknown key (typo?)"]
