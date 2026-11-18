"""Command line: arceus-runner check | run | probe-bulava | doctor | cleanup | rebind | slack-test | qa | arch |
secops."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path


from arceus_runner import __version__
from arceus_runner import bulava as bulava_mod
from arceus_runner import qa_scenarios
from arceus_runner import secops
from arceus_runner.checks import Check, run_online_checks
from arceus_runner.cleanup import _human
from arceus_runner.config import DEFAULT_CONFIG, Config, ConfigError, Secrets, load_config, load_secrets
from arceus_runner.db import ACTIVE_STATES, RUN_UNDER_WAY, AlreadyRunning, InstanceLock, ReviewStore
from arceus_runner.texts import PROJECT_NOTES_LIMIT, ProjectNotesError, project_notes
from arceus_runner.engine import Engine
from arceus_runner.health import engine_doctor, local_checks
from arceus_runner.events import EventLog
from arceus_runner.integrations import build_code_host, build_tracker
from arceus_runner.loop import IntegrationChanged, LiveModeUnavailable, make_cleanup, require_mode_ready, run
from arceus_runner.notify import test_message, webhook_state
from arceus_runner.qa import scenario_name
from arceus_runner.workspace_run import read_instructions, report_address
from arceus_runner.slack import Webhook

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
    checks += [Check("config.retired", "warn", text) for text in cfg.retired]
    try:
        require_mode_ready(cfg, bulava_mod.probe(cfg.paths.bulava))
        checks.append(Check("config.mode", "ok", cfg.mode))
    except LiveModeUnavailable as e:
        checks.append(Check("config.mode", "fail", str(e)))
    probe = bulava_mod.probe(cfg.paths.bulava)
    checks.append(models_check(cfg, probe))
    checks.append(review_agent_check(cfg))
    checks += qa_check(cfg)
    checks.append(architect_check(cfg))
    checks.append(project_notes_check(cfg))
    checks += secops.check(cfg)
    if not args.offline:
        checks += run_online_checks(cfg, secrets, register=True)
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


def qa_check(cfg: Config) -> list[Check]:
    """Whether test runs of the platform can start: each scenario's instructions and when it runs (qa_scenarios.py),
    as runner.db has them; before the runner made its database, the first scenario's file. And a `qa.schedule` still
    written, which is read only once."""
    q = cfg.qa
    out = []
    if q.schedule is not None:
        out.append(Check("config.qa.schedule", "warn",
                         "qa.schedule is read only once, when the runner first starts with test scenarios and gives it "
                         "to the first one; each scenario has its own schedule now (the console's Test runs, or "
                         "`arceus-runner qa scenario`). Remove this block"))
    if not q.enabled:
        return [Check("config.qa", "ok", "off"), *out]
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError):
        scenarios = None
    else:
        try:
            scenarios = [(sc.name, qa_scenarios.file_of(q, sc), qa_scenarios.describe(qa_scenarios.plan_of(sc)))
                         for sc in store.scenarios()]
        finally:
            store.close()
    if scenarios is None:
        scenarios = [("Main", q.instructions, "by hand only")]
    if not scenarios:
        return [Check("config.qa", "warn", "on, but there are no test scenarios: nothing runs until one is made"),
                *out]
    bad = []
    for name, path, _ in scenarios:
        text, why = read_instructions(path)
        if text is None:
            bad.append(f"\u201c{name}\u201d: {why}")
    listed = "; ".join(f"{name} ({when.lower()})" for name, _, when in scenarios)
    if bad:
        return [Check("config.qa", "fail", f"on, but a run of {'; '.join(bad)} would only report that"), *out]
    return [Check("config.qa", "ok", f"on; scenarios: {listed} ({cfg.schedule.timezone}); reports on "
                                     f"http://{q.address}:{q.port}/"), *out]


def architect_check(cfg: Config) -> Check:
    """Whether architecture reviews can start: their optional instructions, what they look at and when they run
    (architect.py). The runner's own criteria stand without instructions, so an empty file is no failure."""
    a = cfg.architect
    if not a.enabled:
        return Check("config.architect", "ok", "off")
    when = (f"`arceus-runner arch run`, and {', '.join(a.schedule.days)} at {a.schedule.at:%H:%M} "
            f"({cfg.schedule.timezone})") if a.schedule else "`arceus-runner arch run` only"
    text, why = read_instructions(a.instructions, required=False)
    if text is None:
        return Check("config.architect", "fail", f"on ({when}), but {why}: a run would only report that")
    told = f"instructions {a.instructions}" if text else "the runner's criteria only (no instructions written)"
    codex = "with Codex's second opinion" if a.second_opinion else "without a second opinion"
    return Check("config.architect", "ok", f"on ({when}); {told}; {codex}; reports on http://{a.address}:{a.port}/")


def review_agent_check(cfg: Config) -> Check:
    """Whether people can ask for a code review on a ticket, and whether it can approve (code_review.py)."""
    r = cfg.review_agent
    if not r.enabled:
        return Check("config.review_agent", "ok", "off")
    how = f"`{r.command}` or a mention of {cfg.tracker.bot_user} with `review`, on a ticket in Code Review"
    if not cfg.code_host.approver:
        return Check("config.review_agent", "warn", f"on ({how}), but it can only comment: approving needs "
                                                    "code_host.approver, a second account (the bot authors the MRs)")
    return Check("config.review_agent", "ok", f"on ({how}); approves as {cfg.code_host.approver.user}")


def project_notes_check(cfg: Config) -> Check:
    """Whether the project's notes for the agent can be given to it as they are (texts.project_notes)."""
    path = cfg.paths.project_notes
    try:
        notes = project_notes(path)
    except ProjectNotesError as e:
        return Check("config.project_notes", "fail", f"{e}; no task starts until it is fixed")
    if path is None:
        return Check("config.project_notes", "ok", "none")
    if not notes:
        return Check("config.project_notes", "ok", f"{path} is empty (comments only): the agent gets no notes")
    return Check("config.project_notes", "ok", f"{path}: {len(notes.encode())} of {PROJECT_NOTES_LIMIT} bytes")


def cmd_doctor(args) -> int:
    cfg = load_config(_config_path(args.config))
    probe = bulava_mod.probe(cfg.paths.bulava)
    checks = local_checks(cfg, probe)
    if probe.installed:
        engine = Engine(cfg)
        engine.refresh_usage()
        checks += engine_doctor(engine)
    if cfg.slack and cfg.slack.webhook_env and cfg.paths.db.exists():
        store = ReviewStore.read_only(cfg.paths.db)
        try:
            state = webhook_state(store)
        finally:
            store.close()
        checks += [state] if state else []
    return EXIT_OK if _print_checks(checks, args.json) else EXIT_CHECK_FAILED


def cmd_slack_test(args) -> int:
    """One message to the project's channel now: the webhook works, and it is the channel meant."""
    cfg, secrets = _load(args)
    if not (cfg.slack and cfg.slack.webhook_env and secrets.slack_webhook):
        print("config: no slack.webhook_env in config.yaml", file=sys.stderr)
        return EXIT_CONFIG
    hook = Webhook(secrets.slack_webhook, cfg.http)
    try:
        said = test_message(hook, cfg.project)
    finally:
        hook.close()
    print(f"slack: {said}")
    return EXIT_OK if said == "posted" else EXIT_CHECK_FAILED


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
    import hashlib
    path = _config_path(args.config)
    # The bytes it was loaded from, read first: the console compares them with the file saved since.
    config_sha = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
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
    return run(cfg, secrets, once=args.once, config_sha=config_sha)


def cmd_assistant_import(args) -> int:
    """An assistant set up before it was part of Arceus becomes config.yaml's assistant section and assistant.env."""
    from arceus_runner import assistant_import
    return assistant_import.run(_config_path(args.config), Path(args.old).expanduser(), port=args.port,
                                web=not args.no_web, apply=args.apply)


def cmd_rebind(args) -> int:
    """Bind runner.db to the tracker or code host config.yaml names now: the same one under a new address."""
    cfg, secrets = _load(args)
    lock = InstanceLock(cfg.paths.db.with_suffix(".lock"))
    lock.acquire()  # the runner must be stopped: it checks the binding when it starts
    try:
        store = ReviewStore(cfg.paths.db)
        try:
            adapter = build_tracker(cfg, secrets) if args.role == "tracker" else build_code_host(cfg, secrets)
            adapter.client.close()
            if getattr(adapter, "reactions_client", None):
                adapter.reactions_client.close()
            was = store.rebind_integration(args.role, adapter.identity())
        finally:
            store.close()
    finally:
        lock.release()
    print(f"{args.role}: {was or 'not bound'} -> {adapter.identity()}")
    return EXIT_OK


def _pick_scenario(store: ReviewStore, n: int | None):
    """The scenario `qa run` queues: the one named, or the only one there is. None, and why on stderr, otherwise."""
    live = store.scenarios()
    if n is not None:
        found = next((sc for sc in live if sc.n == n), None)
        if found is None:
            print(f"qa: there is no scenario {n}; `arceus-runner qa scenario list` shows them", file=sys.stderr)
        return found
    if len(live) == 1:
        return live[0]
    if not live:
        print("qa: there are no test scenarios; make one in the console or with `arceus-runner qa scenario add`",
              file=sys.stderr)
    else:
        names = ", ".join(f"{sc.n} ({sc.name})" for sc in live)
        print(f"qa: there are {len(live)} scenarios, {names}: name one with --scenario N; nothing was queued",
              file=sys.stderr)
    return None


def cmd_scenario(args) -> int:
    """Test scenarios on the server, for an install without the console (qa_scenarios.py): list, add, change, delete.
    The runner reads them on its next pass."""
    cfg = load_config(_config_path(args.config))
    if args.op == "list":
        try:
            store = ReviewStore.read_only(cfg.paths.db)
        except (FileNotFoundError, RuntimeError) as e:
            print(f"qa: {e}", file=sys.stderr)
            return EXIT_CHECK_FAILED
    else:
        store = ReviewStore(cfg.paths.db)
    who = os.environ.get("SUDO_USER") or getpass.getuser()
    try:
        if args.op == "list":
            live = store.scenarios()
            if not live:
                print("No test scenarios. `arceus-runner qa scenario add NAME` makes one.")
            for sc in live:
                plan = qa_scenarios.plan_of(sc)
                print(f"{sc.n}: {sc.name} ({qa_scenarios.describe(plan)}, {cfg.schedule.timezone}); "
                      f"instructions {qa_scenarios.file_of(cfg.qa, sc)}")
            return EXIT_OK
        given = getattr(args, "days", None)
        days = [d.strip() for d in given.split(",") if d.strip()] if given is not None else None
        if args.op == "add":
            with store.transaction():
                sc = qa_scenarios.create(store, cfg.qa, args.name, days or [], args.at or "", who)
            path = qa_scenarios.file_of(cfg.qa, sc)
            print(f"Scenario {sc.n} \u201c{sc.name}\u201d made: {qa_scenarios.describe(qa_scenarios.plan_of(sc))}. "
                  f"Write its instructions in {path}: a run without them stops at once.")
            return EXIT_OK
        sc = store.scenario(args.n)
        if sc is None or sc.deleted_at:
            print(f"qa: there is no scenario {args.n}", file=sys.stderr)
            return EXIT_CHECK_FAILED
        if args.op == "delete":
            store.delete_scenario(sc.n, sc.version, who)
            print(f"Scenario {sc.n} \u201c{sc.name}\u201d deleted; its past runs and reports stay.")
            return EXIT_OK
        now = qa_scenarios.plan_of(sc)
        if args.by_hand:
            days, args.at = [], ""
        name, days_json, at, on = qa_scenarios.clean(
            args.name if args.name is not None else sc.name,
            days if days is not None else list(now.days),
            args.at if args.at is not None else (f"{now.at:%H:%M}" if now.at else ""))
        with store.transaction():
            if qa_scenarios.name_taken(store, name, but=sc.n):
                raise qa_scenarios.Invalid(f"A scenario named \u201c{name}\u201d exists already.")
            changed = store.update_scenario(sc.n, sc.version, who, name=name, days=days_json, at=at, scheduled=on)
        if changed is None:
            print("qa: the scenario changed meanwhile; nothing was saved, try again", file=sys.stderr)
            return EXIT_CHECK_FAILED
        print(f"Scenario {changed.n} \u201c{changed.name}\u201d: "
              f"{qa_scenarios.describe(qa_scenarios.plan_of(changed))}.")
        return EXIT_OK
    except qa_scenarios.Invalid as e:
        print(f"qa: {e}", file=sys.stderr)
        return EXIT_CHECK_FAILED
    finally:
        store.close()


# The background agents' commands: config section, what a run is called, the folder of its reports in words.
RUN_COMMANDS = {"qa": ("qa", "test run", "No test runs yet."),
                "arch": ("architect", "architecture review", "No architecture reviews yet."),
                "secops": ("secops", "triage run", "No triage runs yet.")}


def cmd_runs(args) -> int:
    """Runs of a background agent (workspace_run.py): queue one, show the latest, or stop one. The runner does the
    rest."""
    if args.action == "scenario":
        return cmd_scenario(args)
    kind = args.kind
    section, noun, none = RUN_COMMANDS[kind]
    cfg = load_config(_config_path(args.config))
    c = getattr(cfg, section)
    if args.action == "run" and not c.enabled:
        print(f"{kind}: off in config.yaml ({section}.enabled: false); nothing was queued", file=sys.stderr)
        return EXIT_CHECK_FAILED
    if args.action == "status":
        try:
            store = ReviewStore.read_only(cfg.paths.db)
        except (FileNotFoundError, RuntimeError) as e:
            print(f"{kind}: {e}", file=sys.stderr)
            return EXIT_CHECK_FAILED
    else:
        store = ReviewStore(cfg.paths.db)
    try:
        if args.action == "run":
            scenario, label = 0, ""
            if kind == "qa":
                picked = _pick_scenario(store, args.scenario)
                if picked is None:
                    return EXIT_CHECK_FAILED
                scenario, label = picked.n, f" of the scenario \u201c{picked.name}\u201d"
            who = os.environ.get("SUDO_USER") or getpass.getuser()
            try:
                run, busy = store.queue_run(kind, "command", who, scenario=scenario)
            except LookupError as e:
                print(f"{kind}: {e}; nothing was queued", file=sys.stderr)
                return EXIT_CHECK_FAILED
            if busy is not None:
                print(f"{kind}: run {busy.n}{label} is {'queued' if busy.state == 'pending' else 'under way'}; nothing "
                      f"more was queued. `arceus-runner {kind} stop {busy.n}` stops it.", file=sys.stderr)
                return EXIT_CHECK_FAILED
            assert run is not None
            if cfg.mode != "live":
                print(f"{kind}: the runner is in mode: {cfg.mode}, which starts nothing; run {run.n} waits until it is "
                      "live", file=sys.stderr)
            url, tunnel = report_address(cfg, kind, c.port, run.n)
            print(f"Run {run.n}{label} queued. The runner starts it once the workspace is free; the report will be at "
                  f"{url}{' (through the SSH tunnel)' if tunnel else ''}.")
            return EXIT_OK
        if args.action == "stop":
            runs = [store.run(f"{kind}-{args.n}")] if args.n else store.runs(kind, *RUN_UNDER_WAY)
            runs = [r for r in runs if r and r.state in RUN_UNDER_WAY]
            if not runs:
                print(f"{kind}: no such run is queued or under way", file=sys.stderr)
                return EXIT_CHECK_FAILED
            for r in runs:
                store.update_run(r.id, stop_requested=1)
                print(f"Run {r.n}: asked to stop. The runner stops it on its next pass and writes its report.")
            return EXIT_OK
        runs = store.runs(kind, limit=args.last)
        if args.json:
            print(json.dumps([vars(r) | {"result": json.loads(r.result) if r.result else None} for r in runs],
                             ensure_ascii=False, indent=2))
            return EXIT_OK
        if not runs:
            print(f"{none} `arceus-runner {kind} run` queues one.")
            return EXIT_OK
        for r in runs:
            found = len(json.loads(r.result or "{}").get("findings") or [])
            what = r.outcome if r.state == "done" else r.state
            what += f", {found} finding{'s' if found != 1 else ''}" if r.state == "done" and found else ""
            what += ", stop asked" if r.stop_requested and r.state != "done" else ""
            by = "schedule" if r.source == "schedule" else r.requested_by
            of = f" [{scenario_name(store, r)}]" if kind == "qa" else ""
            print(f"run {r.n}{of}: {what} (by {by}, queued {r.created_at}"
                  + (f", finished {r.finished_at}" if r.finished_at else "") + ")"
                  + (f": {r.reason}" if r.reason and r.state == "done" else ""))
        url, tunnel = report_address(cfg, kind, c.port)
        print(f"Reports: {url}{' (through the SSH tunnel)' if tunnel else ''}")
        return EXIT_OK
    finally:
        store.close()


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
                               summary.get("unknown_bytes"), summary.get("freed_by_rule"))
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
            review = store.review_holding()
            if review:
                print(f"cleanup: the code review {review.id} holds the workspace ({review.state}); not now",
                      file=sys.stderr)
                return EXIT_CHECK_FAILED
            holding = store.run_holding()
            if holding:
                print(f"cleanup: the run {holding.id} holds the workspace ({holding.state}); not now",
                      file=sys.stderr)
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
                       result.get("unknown_bytes"), result.get("freed_by_rule"))
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


RULE_NAMES = {"build_cache": "build cache", "dangling_images": "untagged images", "unused_images": "unused images"}


def _print_cleanup(planned: list[dict], unknown: list[dict], errors: list[str], images_in_use: int | None = None,
                   unknown_bytes: int | None = None, freed_by_rule: dict | None = None) -> None:
    if not planned and not unknown and not errors:
        print("  nothing to clean up")
    for i in planned:
        result = f" [{i['result']}]" if i.get("result") not in (None, "planned") else ""
        print(f"  {i['action']:<7} {i['kind']:<9} {i['name']}: {i['why']}{result}")
    if freed_by_rule:
        print("Freed, as Docker counts it: " + "; ".join(
            f"{RULE_NAMES.get(rule, rule)} " + (_human(n) if n is not None else "unknown (Docker did not say)")
            for rule, n in freed_by_rule.items()))
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
    p = argparse.ArgumentParser(prog="arceus-runner",
                                description="Arceus bridge between an issue tracker, a code host and Bulava")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help=f"config.yaml (default: $ARCEUS_RUNNER_CONFIG or {DEFAULT_CONFIG})")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="validate config.yaml and secrets, then check them against the tracker, "
                                     "the code host and Slack")
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

    rb = sub.add_parser("rebind", help="bind runner.db to the tracker or code host config.yaml names now "
                                       "(the same one under a new address; stop the runner first)")
    rb.add_argument("role", choices=["tracker", "code_host"])
    rb.set_defaults(fn=cmd_rebind)

    a = sub.add_parser("assistant", help="the project's assistant (docs/assistant.md)")
    asub = a.add_subparsers(dest="action", required=True)
    ai = asub.add_parser("import", help="carry an assistant set up before it was part of Arceus (~/assistant) into "
                                        "config.yaml and assistant.env; its conversations stay where they are")
    ai.add_argument("old", help="its config, e.g. ~/assistant/config.yaml")
    ai.add_argument("--port", type=int, default=8190, help="its port on 127.0.0.1 (the old unit's ASSISTANT_PORT)")
    ai.add_argument("--no-web", action="store_true", help="no web chat at /assistant/: Slack only")
    ai.add_argument("--apply", action="store_true", help="write it (without: only say what would be written)")
    ai.set_defaults(fn=cmd_assistant_import)

    st = sub.add_parser("slack-test", help="post one test message to the project's channel through the webhook")
    st.set_defaults(fn=cmd_slack_test)

    for kind, help_, run_help in (
            ("qa", "test runs of the platform: queue one, see the latest, stop one",
             "queue a test run of a scenario"),
            ("arch", "architecture reviews: queue one, see the latest, stop one",
             "queue a review of the repositories that changed since the last one"),
            ("secops", "advisory triage: queue a run, see the latest, stop one",
             "queue a triage run of the code against the advisory register's open advisories")):
        q = sub.add_parser(kind, help=help_)
        qsub = q.add_subparsers(dest="action", required=True)
        qr = qsub.add_parser("run", help=run_help)
        if kind == "qa":
            qr.add_argument("--scenario", type=int, help="the scenario's number (`qa scenario list`); needed when "
                                                          "there is more than one")
        qs = qsub.add_parser("status", help="the latest runs and how they ended")
        qs.add_argument("--last", type=int, default=10, help="how many runs (default 10)")
        qs.add_argument("--json", action="store_true")
        qx = qsub.add_parser("stop", help="stop the queued or running one (its report says what it found so far)")
        qx.add_argument("n", nargs="?", type=int, help="the run's number (default: the one under way)")
        q.set_defaults(fn=cmd_runs, kind=kind)
        if kind == "qa":
            sc = qsub.add_parser("scenario", help="test scenarios: list, add, change, delete (the console does the "
                                                  "same)")
            ssub = sc.add_subparsers(dest="op", required=True)
            ssub.add_parser("list", help="every scenario with its schedule and instructions file")
            for op, op_help in (("add", "make a scenario; write its instructions in the file it names"),
                                ("set", "change a scenario's name or schedule")):
                o = ssub.add_parser(op, help=op_help)
                if op == "add":
                    o.add_argument("name")
                else:
                    o.add_argument("n", type=int)
                    o.add_argument("--name")
                    o.add_argument("--by-hand", action="store_true", help="clear the days and the time: it runs "
                                                                          "only when someone starts it")
                o.add_argument("--days", help="mon,tue,...,sun; with --at, it runs by itself then")
                o.add_argument("--at", help="HH:MM in schedule.timezone; with --days, it runs by itself then")
            sd = ssub.add_parser("delete", help="delete a scenario; its past runs and reports stay")
            sd.add_argument("n", type=int)
            sc.set_defaults(fn=cmd_scenario)

    from arceus_runner.console import cli as console_cli
    console_cli.add_parser(sub)

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
    except (LiveModeUnavailable, IntegrationChanged) as e:
        print(f"config: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except AlreadyRunning as e:
        print(f"runner: {e}", file=sys.stderr)
        return EXIT_LOCKED
