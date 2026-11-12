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


def test_independent_checks_turned_off_say_so_to_the_engine(tmp_path):
    raw = example_raw()
    raw["bulava"]["claude_checks"] = {"enabled": False, "rounds": 3}
    c = load_config(write(tmp_path, raw)).bulava.claude_checks
    assert c.env() == {"SUPERVISOR_CLAUDE_CHECKS": "0"} and c.profile() == {"claude_checks": "off"}


def test_independent_checks_as_chosen(tmp_path):
    raw = example_raw()
    raw["bulava"]["claude_checks"] = {"kinds": ["security", "security"], "min_severity": "medium",
                                      "rounds": 2, "timeout": "20m", "required": True}
    env = load_config(write(tmp_path, raw)).bulava.claude_checks.env()
    assert (env["SUPERVISOR_CLAUDE_CHECKS_LIST"], env["SUPERVISOR_CLAUDE_CHECKS_MIN_SEVERITY"],
            env["SUPERVISOR_CLAUDE_CHECKS_ROUNDS"], env["SUPERVISOR_CLAUDE_CHECKS_TIMEOUT"],
            env["SUPERVISOR_CLAUDE_CHECKS_REQUIRED"]) == ("security", "medium", "2", "1200", "1")


@pytest.mark.parametrize("key,value", [
    ("kinds", []), ("kinds", ["style"]), ("min_severity", "info"), ("rounds", 0), ("rounds", 6),
    ("timeout", "45m"), ("timeout", "30s"), ("required", "maybe"),
])
def test_independent_checks_the_engine_would_not_take_are_refused(tmp_path, key, value):
    raw = example_raw()
    raw["bulava"]["claude_checks"] = {key: value}
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, raw))
    assert all(p.startswith(f"bulava.claude_checks.{key}") for p in e.value.problems), e.value.problems


def test_independent_checks_need_an_engine_that_runs_them(tmp_path, monkeypatch):
    from arceus_runner import bulava
    from arceus_runner.health import claude_checks
    from arceus_runner.loop import LiveModeUnavailable, require_mode_ready
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])
    old = _engine(tmp_path, "", "#!/bin/sh\n")
    (old / "hooks").mkdir()
    (old / "hooks" / "review-gate.sh").write_text("#!/bin/bash\n# claude_checks_enabled only in a comment\n")
    old_probe = bulava.probe(old)
    cfg = load_config(EXAMPLE)
    c = claude_checks(cfg, old_probe)
    assert c.status == "fail" and "update it (setup.sh)" in c.detail
    with pytest.raises(LiveModeUnavailable, match="independent Claude checks"):
        require_mode_ready(cfg.model_copy(update={"mode": "live"}), old_probe)

    raw = example_raw()
    raw["bulava"]["claude_checks"] = {"enabled": False}
    off = load_config(write(tmp_path, raw))
    c = claude_checks(off, old_probe)                      # no engine server to compare with yet
    assert (c.status, c.detail) == ("ok", "off in config.yaml")  # turned off on purpose: not a warning

    new = _engine(tmp_path / "new", "", "#!/bin/sh\n")
    (new / "hooks").mkdir()
    (new / "hooks" / "review-gate.sh").write_text(
        '#!/bin/bash\nif [ -n "${IDIR_SCOPE:-}" ] && claude_checks_enabled; then\n  :\nfi\n')
    new_probe = bulava.probe(new)
    assert new_probe.missing_for_checks() == []
    c = claude_checks(cfg, new_probe)
    assert c.status == "ok"
    assert c.detail == "code + security, back to the agent from high, 1 round(s) per task, 15 min each, required"


def test_the_review_gate_must_run_with_what_config_yaml_says(tmp_path, monkeypatch):
    """A worker's review gate reads the environment of the engine's tmux server (bulava-engine.service),
    so that is what config.yaml is compared with: an engine server started from an older unit, or never
    restarted, would run the checks on other terms, and no new task is taken until it is put right."""
    from arceus_runner import bulava
    from arceus_runner.health import claude_checks
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])
    root = _engine(tmp_path / "engine", "", "#!/bin/sh\n")
    (root / "hooks").mkdir()
    (root / "hooks" / "review-gate.sh").write_text('#!/bin/bash\nif [ -n "${IDIR_SCOPE:-}" ] && claude_checks_enabled; then\n  :\nfi\n')
    probe = bulava.probe(root)
    cfg = load_config(EXAMPLE)
    unit = {"SUPERVISOR_PROFILE": "server", **cfg.bulava.claude_checks.env()}
    assert claude_checks(cfg, probe, live_env=lambda: None).status == "ok"         # no server: the dispatch starts it
    assert claude_checks(cfg, probe, live_env=lambda: unit).status == "ok"
    old_unit = {"SUPERVISOR_PROFILE": "server"}                                    # the engine's own defaults
    c = claude_checks(cfg, probe, live_env=lambda: old_unit)
    assert c.status == "fail" and "SUPERVISOR_CLAUDE_CHECKS_REQUIRED=0 (config.yaml: 1)" in c.detail and "restart bulava-engine" in c.detail
    raw = example_raw()
    raw["bulava"]["claude_checks"] = {"enabled": False}
    off = load_config(write(tmp_path, raw))
    c = claude_checks(off, probe, live_env=lambda: old_unit)                       # the gate would still run them
    assert c.status == "fail" and "SUPERVISOR_CLAUDE_CHECKS=1 (config.yaml: 0)" in c.detail
    assert claude_checks(off, probe, live_env=lambda: {"SUPERVISOR_CLAUDE_CHECKS": "0"}).status == "ok"
    (root / "bin" / "supervisor-lib.sh").write_text('_RUN_ENV_VARS="SUPERVISOR_CODEX_EFFORT SUPERVISOR_CLAUDE_CHECKS"\n')
    carries = bulava.probe(root)                                                    # an engine that takes them per task
    assert claude_checks(cfg, carries, live_env=lambda: old_unit).status == "ok"


def test_engine_env_prints_what_setup_writes_into_the_engine_unit(capsys):
    assert main(["--config", str(EXAMPLE), "engine-env"]) == EXIT_OK
    assert sorted(capsys.readouterr().out.splitlines()) == [
        "SUPERVISOR_CLAUDE_CHECKS=1", "SUPERVISOR_CLAUDE_CHECKS_LIST=code security",
        "SUPERVISOR_CLAUDE_CHECKS_MIN_SEVERITY=high", "SUPERVISOR_CLAUDE_CHECKS_REQUIRED=1",
        "SUPERVISOR_CLAUDE_CHECKS_ROUNDS=1", "SUPERVISOR_CLAUDE_CHECKS_TIMEOUT=900"]


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


def test_the_engine_warning_on_checks_turned_off_is_ok_when_config_yaml_turned_them_off(tmp_path):
    """Bulava's doctor warns on a server whose Claude checks are off. Off in config.yaml and off in the
    engine is what was asked for; the engine running them, or config.yaml wanting them, stays a warning."""
    from types import SimpleNamespace
    from arceus_runner.health import engine_doctor
    raw = example_raw()
    raw["bulava"]["claude_checks"] = {"enabled": False}
    off, on = load_config(write(tmp_path, raw)), load_config(EXAMPLE)
    team = {"name": "engine.review_team", "status": "ok",
            "detail": {"enabled": True, "members": ["contract", "security", "state"], "calls_per_round": 3}}

    def doctor(cfg, *rows):
        engine = SimpleNamespace(cfg=cfg, doctor=lambda: {"checks": list(rows)})
        return {c.name: c for c in engine_doctor(engine)}

    def row(enabled):
        return {"name": "engine.claude_checks", "status": "warn",
                "detail": {"enabled": enabled, "list": ["code", "security"], "min_severity": "high",
                           "rounds": "1", "required": False}}

    c = doctor(off, row(False), team)["bulava.engine.claude_checks"]
    assert (c.status, c.detail) == ("ok", "off, as config.yaml says; Codex review team: contract, security, state")
    assert doctor(off, row(False))["bulava.engine.claude_checks"].detail == "off, as config.yaml says"
    assert doctor(on, row(False), team)["bulava.engine.claude_checks"].status == "warn"   # config.yaml wants them
    assert doctor(off, row(True), team)["bulava.engine.claude_checks"].status == "warn"   # the engine still has them
    odd = {"name": "engine.claude_checks", "status": "warn", "detail": "off"}            # a reading not understood
    assert doctor(off, odd)["bulava.engine.claude_checks"].status == "warn"
    failing = {**row(False), "status": "fail"}
    assert doctor(off, failing)["bulava.engine.claude_checks"].status == "fail"
    unknown = {**row(False), "status": "unexpected"}                                     # a status not understood
    assert doctor(off, unknown)["bulava.engine.claude_checks"].status == "warn"
    nameless = {k: v for k, v in row(False).items() if k != "status"}                   # no status at all
    assert doctor(off, nameless)["bulava.engine.claude_checks"].status == "warn"
