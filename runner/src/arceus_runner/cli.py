"""Command line: arceus-runner check | run | probe-bulava | doctor."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from arceus_runner import __version__
from arceus_runner import bulava as bulava_mod
from arceus_runner.checks import Check, run_online_checks
from arceus_runner.config import DEFAULT_CONFIG, Config, ConfigError, Secrets, load_config, load_secrets
from arceus_runner.db import AlreadyRunning
from arceus_runner.engine import Engine
from arceus_runner.health import engine_doctor, local_checks
from arceus_runner.loop import LiveModeUnavailable, require_mode_ready, run

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_CONFIG = 2
EXIT_LOCKED = 3

MARK = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}


def _config_path(arg: str | None) -> Path:
    return Path(arg or os.environ.get("ARCEUS_RUNNER_CONFIG") or DEFAULT_CONFIG).expanduser()


def _load(args) -> tuple[Config, Secrets]:
    cfg = load_config(_config_path(args.config))
    return cfg, load_secrets(cfg)


def _print_checks(checks: list[Check], as_json: bool) -> bool:
    ok = all(c.status != "fail" for c in checks)
    if as_json:
        print(json.dumps({"ok": ok, "checks": [c.as_dict() for c in checks]}, ensure_ascii=False, indent=2))
    else:
        for c in checks:
            print(f"[{MARK[c.status]}] {c.name}: {c.detail}")
    return ok


def cmd_check(args) -> int:
    cfg, secrets = _load(args)
    checks = [Check("config", "ok", str(_config_path(args.config)))]
    try:
        require_mode_ready(cfg, bulava_mod.probe(cfg.paths.bulava))
        checks.append(Check("config.mode", "ok", cfg.mode))
    except LiveModeUnavailable as e:
        checks.append(Check("config.mode", "fail", str(e)))
    checks.append(models_check(cfg, bulava_mod.probe(cfg.paths.bulava)))
    if not args.offline:
        checks += run_online_checks(cfg, secrets)
    return EXIT_OK if _print_checks(checks, args.json) else EXIT_CHECK_FAILED


def models_check(cfg: Config, probe: bulava_mod.Probe) -> Check:
    """Which models and depth new tasks get, and whether the installed engine can be told."""
    b = cfg.bulava
    shown = (f"Claude {b.claude_model}·{b.claude_effort}, Codex {b.codex_model}·{b.codex_effort}"
             " (auto: the engine's depth, high, and the CLI's default model)")
    if not b.model_env():
        return Check("config.models", "ok", shown)
    missing = probe.missing_for_models()
    if missing:
        return Check("config.models", "fail", f"{shown}; the engine in {cfg.paths.bulava} does not take "
                                              f"{', '.join(missing)}: update it (setup.sh) or set them to auto")
    return Check("config.models", "ok", shown)


def cmd_doctor(args) -> int:
    cfg = load_config(_config_path(args.config))
    probe = bulava_mod.probe(cfg.paths.bulava)
    checks = local_checks(cfg, probe)
    if probe.installed:
        engine = Engine(cfg)
        engine.refresh_usage()
        checks += engine_doctor(engine)
    return EXIT_OK if _print_checks(checks, args.json) else EXIT_CHECK_FAILED


def cmd_probe(args) -> int:
    root = Path(args.root) if args.root else load_config(_config_path(args.config)).paths.bulava
    p = bulava_mod.probe(root)
    if args.json:
        print(json.dumps(p.as_dict(), indent=2))
    else:
        print(f"engine: {p.root}  installed: {p.installed}  version: {p.version}  commit: {p.commit}")
        for cap in bulava_mod.CAPABILITIES:
            mark = "yes" if p.advertised.get(cap.name) else "no "
            print(f"  wave {cap.wave}  {mark}  {cap.meaning}")
        for problem in p.problems:
            print(f"  problem: {problem}")
        print("advertised = the text is in the engine's scripts; contract tests on the server confirm it")
    return EXIT_OK if p.installed and not p.problems else EXIT_CHECK_FAILED


def cmd_run(args) -> int:
    cfg, secrets = _load(args)
    require_mode_ready(cfg, bulava_mod.probe(cfg.paths.bulava))
    if not args.skip_checks:
        # Config mistakes are cheaper to catch before the loop than on the first task.
        checks = run_online_checks(cfg, secrets)
        failed = [c for c in checks if c.status == "fail"]
        if failed:
            _print_checks(failed, False)
            print("runner not started: fix the checks above", file=sys.stderr)
            return EXIT_CHECK_FAILED
    return run(cfg, secrets, once=args.once)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arceus-runner", description="Arceus bridge: YouTrack, GitLab and Bulava")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help=f"config.yaml (default: $ARCEUS_RUNNER_CONFIG or {DEFAULT_CONFIG})")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="validate config.yaml and secrets, then check them against YouTrack/GitLab/Slack")
    c.add_argument("--offline", action="store_true", help="only validate the files, no network")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)

    r = sub.add_parser("run", help="start the loop (mode from config.yaml)")
    r.add_argument("--once", action="store_true", help="one tick, then exit")
    r.add_argument("--skip-checks", action="store_true", help="do not run the online config checks first")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("doctor", help="local health: disk, RAM, CLIs, and the engine's doctor (Claude/Codex sign-in)")
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_doctor)

    b = sub.add_parser("probe-bulava", help="which agreed contracts the installed engine advertises")
    b.add_argument("--root", help="engine directory (default: paths.bulava from config)")
    b.add_argument("--json", action="store_true")
    b.set_defaults(fn=cmd_probe)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        for problem in e.problems:
            print(f"config: {problem}", file=sys.stderr)
        return EXIT_CONFIG
    except LiveModeUnavailable as e:
        print(f"config: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except AlreadyRunning as e:
        print(f"runner: {e}", file=sys.stderr)
        return EXIT_LOCKED
