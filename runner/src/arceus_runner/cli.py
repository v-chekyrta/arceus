"""Command line: arceus-runner check | run | probe-bulava | doctor | cleanup."""

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
from arceus_runner.db import ACTIVE_STATES, AlreadyRunning, InstanceLock, ReviewStore
from arceus_runner.engine import Engine
from arceus_runner.health import claude_checks, engine_doctor, local_checks
from arceus_runner.events import EventLog
from arceus_runner.loop import LiveModeUnavailable, make_cleanup, require_mode_ready, run

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
    probe = bulava_mod.probe(cfg.paths.bulava)
    checks.append(models_check(cfg, probe))
    checks.append(claude_checks(cfg, probe))
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


def cmd_engine_env(args) -> int:
    """The engine settings the review gate must see, one NAME=value per line, for bulava-engine.service.
    On a server a worker's gate reads the environment of the engine's tmux server, which that unit
    starts: whatever the runner passes on a dispatch does not reach it."""
    cfg = load_config(_config_path(args.config))
    for name, value in sorted(cfg.bulava.claude_checks.env().items()):
        print(f"{name}={value}")
    return EXIT_OK


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


def cmd_cleanup(args) -> int:
    """What a full cleanup pass would do now, the last pass (both read only), or (--act) a pass now."""
    cfg = load_config(_config_path(args.config))
    if args.act:
        return _cleanup_now(cfg, args.json)
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"cleanup: {e}", file=sys.stderr)
        return EXIT_CHECK_FAILED
    try:
        if args.last:
            runs = store.cleanup_runs(1)
            if not runs:
                print("no cleanup pass yet")
                return EXIT_OK
            r = runs[0]
            summary = json.loads(r["summary"] or "{}")
            planned, unknown = _last_items(store, r["id"], summary)
            if args.json:
                print(json.dumps({**dict(r), "summary": {**summary, "planned": planned, "unknown": unknown}},
                                 ensure_ascii=False, indent=2))
            else:
                print(f"{r['id']}: {r['status']} ({r['trigger']}, {r['scope']}, {r['mode']}), "
                      f"started {r['started_at']}, finished {r['finished_at'] or '-'}")
                _print_cleanup(planned, unknown, summary.get("errors") or [], summary.get("images_in_use"),
                               summary.get("unknown_bytes"))
            return EXIT_OK
        cleanup = make_cleanup(cfg, store, QuietEvents(), None)
        items, errors = cleanup.plan("full")
        if args.json:
            print(json.dumps({"mode": cleanup.mode, "errors": errors, "items": [
                {"kind": i.kind, "name": i.name, "action": i.action, "why": i.why, "task": i.task} for i in items]},
                ensure_ascii=False, indent=2))
        else:
            print(f"A full pass now (cleanup.mode: {cleanup.mode}; nothing is changed by this command):")
            _print_cleanup([vars(i) for i in items if i.action != "report"],
                           [{**vars(i), "bytes": i.detail.get("bytes")} for i in items if i.action == "report"], errors,
                           cleanup.images_in_use)
        return EXIT_OK if not errors else EXIT_CHECK_FAILED
    finally:
        store.close()


def _cleanup_now(cfg: Config, as_json: bool) -> int:
    """A full pass by hand. The runner's lock first: it must not work in the workspace (or migrate) meanwhile."""
    if cfg.mode == "observe":
        print("cleanup: the runner is in mode: observe, which removes nothing; `cleanup` alone shows the plan",
              file=sys.stderr)
        return EXIT_CHECK_FAILED
    lock = InstanceLock(cfg.paths.db.with_suffix(".lock"))
    lock.acquire()
    try:
        store = ReviewStore(cfg.paths.db)
        events = EventLog(cfg.paths.events, cfg.project)
        try:
            active = store.tasks(*ACTIVE_STATES)
            if active:
                print(f"cleanup: {active[0].task_id} holds the workspace ({active[0].state}); not now", file=sys.stderr)
                return EXIT_CHECK_FAILED
            engine = Engine(cfg) if bulava_mod.probe(cfg.paths.bulava).installed else None
            cleanup = make_cleanup(cfg, store, events, engine)
            cleanup.mode = "act"  # asked for by a person, whatever cleanup.mode says
            cleanup.recover()
            busy = cleanup.engine_busy()
            if busy:
                print(f"cleanup: {busy}; not now", file=sys.stderr)
                return EXIT_CHECK_FAILED
            result = cleanup.run("full", "manual")
            result["planned"], result["unknown"] = _last_items(store, result["run"], result)
        finally:
            events.close()
            store.close()
    finally:
        lock.release()
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"{result['run']}: {result['status']}")
        _print_cleanup(result["planned"], result["unknown"], result["errors"], result.get("images_in_use"),
                       result.get("unknown_bytes"))
    return EXIT_OK if result["status"] == "succeeded" else EXIT_CHECK_FAILED


def _last_items(store, run_id: str, summary: dict) -> tuple[list[dict], list[dict]]:
    """A pass's planned and unplaced items. The summary lists only the first of each; when it cut a list, the
    whole of it comes from cleanup_items, where every item of the pass is."""
    planned, unknown = summary.get("planned") or [], summary.get("unknown") or []
    if summary.get("planned_total", 0) <= len(planned) and summary.get("unknown_total", 0) <= len(unknown):
        return planned, unknown
    planned, unknown = [], []
    for row in store.cleanup_items(run_id):
        detail = json.loads(row["detail"] or "{}")
        entry = {"kind": row["kind"], "name": detail.get("name", row["ref"]), "why": detail.get("why", "")}
        if row["action"] == "report":
            unknown.append({**entry, **{k: detail[k] for k in ("bytes", "advice") if detail.get(k) is not None}})
        else:
            planned.append({**entry, "action": row["action"], "result": row["result"]})
    return planned, unknown


class QuietEvents:
    """A plan only reads and writes no events; the runner may be writing events.jsonl meanwhile."""

    def emit(self, *_args, **_fields) -> dict:
        return {}


def _print_cleanup(planned: list[dict], unknown: list[dict], errors: list[str], images_in_use: int | None = None,
                   unknown_bytes: int | None = None) -> None:
    if not planned and not unknown and not errors:
        print("  nothing to clean up")
    for i in planned:
        result = f" [{i['result']}]" if i.get("result") not in (None, "planned") else ""
        print(f"  {i['action']:<7} {i['kind']:<9} {i['name']}: {i['why']}{result}")
    if unknown:
        print("Not put down to any task (reported, never removed):")
        for i in unknown:
            advice = i.get("advice") or (i.get("detail") or {}).get("advice")
            note = f"  [Claude: {advice['verdict']}, {advice['reason']}]" if advice else ""
            print(f"  {i['kind']:<9} {i['name']}: {i['why']}{note}")
        total = unknown_bytes if unknown_bytes is not None else sum(i.get("bytes") or 0 for i in unknown)
        if total:
            print(f"  about {total / 1000**3:.1f} GB in all, where the size is known")
    if images_in_use:
        print(f"Images that containers use ({images_in_use}) are not listed.")
    for e in errors:
        print(f"  error: {e}")


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

    k = sub.add_parser("cleanup", help="what a cleanup pass would remove now and why (changes nothing)")
    k.add_argument("--last", action="store_true", help="the last pass the runner made")
    k.add_argument("--act", action="store_true", help="make a full pass now (stop the runner first)")
    k.add_argument("--json", action="store_true")
    k.set_defaults(fn=cmd_cleanup)

    e = sub.add_parser("engine-env", help="the check settings for bulava-engine.service (setup.sh writes them there)")
    e.set_defaults(fn=cmd_engine_env)

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
