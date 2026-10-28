"""Prometheus metrics on localhost: what Grafana's "now" dashboard shows.

Everything here is a gauge refreshed from the runner's own state on every tick, so a scrape
never waits on the tracker, the code host or the engine. Task history and reasons live in events.jsonl and
reach Grafana through Loki; this file is only the current picture.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from prometheus_client import CollectorRegistry, Gauge, start_http_server

from arceus_runner import __version__

HEALTH_VALUE = {"ok": 0, "warn": 1, "fail": 2}
USAGE_FILES = {"claude": "usage.json", "codex": "codex-usage.json"}
WINDOWS = {"five_hour": "5h", "seven_day": "7d"}


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None):
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.info = Gauge("runner_info", "Runner build and mode", ["version", "mode", "bulava_commit"], registry=r)
        self.health = Gauge("runner_health", "Health check: 0 ok, 1 warn, 2 fail", ["check"], registry=r)
        self.tasks = Gauge("runner_tasks", "Tasks the runner took, by runner state", ["state"], registry=r)
        self.queue = Gauge("runner_queue_length", "Accepted tickets waiting to be taken", registry=r)
        self.busy = Gauge("runner_agent_busy", "1 while a task holds the agent", registry=r)
        self.can_take = Gauge("runner_can_take", "1 when health allows new work", registry=r)
        # role: tracker or code_host, so a dashboard reads them whichever provider config.yaml names.
        self.service_up = Gauge("runner_service_up", "1 when the service answers", ["service", "role"], registry=r)
        self.backoff = Gauge("runner_backoff_seconds", "Seconds left in a service's pause", ["service", "role"],
                             registry=r)
        self.usage = Gauge("runner_usage_percent", "Subscription usage read by Bulava", ["provider", "window"],
                           registry=r)
        self.usage_resets = Gauge("runner_usage_resets_at_seconds", "When the usage window resets (epoch)",
                                  ["provider", "window"], registry=r)
        self.task = Gauge("runner_task", "1 for each task that is not over, with its state",
                          ["task_id", "state", "summary"], registry=r)
        self.last_tick = Gauge("runner_last_tick_seconds", "When the runner last finished a tick (epoch): "
                               "a stale value means the loop is stuck in a long call", registry=r)
        self.step = Gauge("runner_task_step_since_seconds", "The engine step a running task is in, and when it "
                          "began (epoch)", ["task_id", "stage", "state"], registry=r)
        self.agent_active = Gauge("runner_agent_last_activity_seconds", "When the agent's transcript last changed "
                                  "(epoch); absent when it is not known", ["task_id"], registry=r)
        self.agent_quiet = Gauge("runner_agent_quiet_seconds", "How long the agent has written nothing while the "
                                 "step is its own; absent while the gate, Codex or a usage limit has the turn",
                                 ["task_id"], registry=r)
        self.cleanup_ok = Gauge("runner_cleanup_last_success_seconds", "When the last successful cleanup pass "
                                "started (epoch), by scope: full or independent (while a task works)", ["scope"],
                                registry=r)
        self.cleanup_last = Gauge("runner_cleanup_last_run_seconds", "When the last cleanup pass started (epoch), "
                                  "with its status (succeeded, partial, failed, interrupted) and mode",
                                  ["status", "mode"], registry=r)
        self.cleanup_unknown = Gauge("runner_cleanup_unknown_items", "Leftovers the last pass could not put down "
                                     "to any task: reported, never removed", registry=r)
        self.cleanup_freed = Gauge("runner_cleanup_freed_bytes", "Disk space the last acting pass freed", registry=r)
        self.observed = Gauge("runner_engine_observed_seconds", "When the runner last read a running task's engine "
                              "files (epoch)", ["task_id"], registry=r)

    def serve(self, address: str, port: int):
        """Start the /metrics server. Returns a function that stops it and frees the port."""
        server, thread = start_http_server(port, addr=address, registry=self.registry)

        def stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        return stop

    def update(self, runner) -> None:
        cfg = runner.cfg
        probe_commit = getattr(runner, "bulava_commit", "") or ""
        self.info.clear()
        self.info.labels(__version__, cfg.mode, probe_commit[:12]).set(1)

        self.health.clear()
        for name, status in runner.health.items():
            self.health.labels(name).set(HEALTH_VALUE.get(status, 1))

        counts: dict[str, int] = {}
        self.task.clear()
        for t in runner.store.tasks():
            counts[t.state] = counts.get(t.state, 0) + 1
            if t.state not in ("failed", "done"):
                self.task.labels(t.task_id, t.state, t.summary[:80]).set(1)
        self.tasks.clear()
        for state, n in counts.items():
            self.tasks.labels(state).set(n)
        self.queue.set(len(runner.store.accepted_untaken()))
        self.busy.set(1 if runner.store.tasks("claimed", "dispatched", "publishing") else 0)
        self.can_take.set(1 if runner.can_take() else 0)

        for role, client in (("tracker", runner.tracker.client), ("code_host", runner.code_host.client)):
            self.service_up.labels(client.name, role).set(
                1 if runner.health.get(f"service.{client.name}") == "ok" else 0)
            self.backoff.labels(client.name, role).set(round(client.seconds_left()))

        self.update_usage(cfg.paths.bulava_state)
        self.update_progress(runner)
        self.update_cleanup(getattr(runner, "cleanup", None))

    def update_cleanup(self, cleanup) -> None:
        self.cleanup_ok.clear()
        self.cleanup_last.clear()
        if not cleanup:
            return
        runs = cleanup.last_runs()
        for scope in ("full", "independent"):
            r = runs.get(f"ok:{scope}")
            if r:
                self.cleanup_ok.labels(scope).set(_epoch(r["started_at"]))
        last = runs.get("last")
        if last:
            self.cleanup_last.labels(last["status"], last["mode"]).set(_epoch(last["started_at"]))
            summary = json.loads(last["summary"] or "{}")
            self.cleanup_unknown.set(len(summary.get("unknown") or []))
            if "freed_bytes" in summary:
                self.cleanup_freed.set(summary["freed_bytes"])

    def update_progress(self, runner) -> None:
        if getattr(runner, "last_tick", None):
            self.last_tick.set(runner.last_tick)
        self.step.clear()
        self.agent_active.clear()
        self.agent_quiet.clear()
        self.observed.clear()
        progress = getattr(runner, "progress", None)
        for p in progress.snapshot() if progress else []:
            if p.step:
                self.step.labels(p.task_id, p.step.stage, p.step.state).set(p.step.since)
            if p.agent_active_at is not None:
                self.agent_active.labels(p.task_id).set(p.agent_active_at)
            quiet = progress.quiet_seconds(p)
            if quiet is not None:
                self.agent_quiet.labels(p.task_id).set(round(quiet))
            if p.observed_at:
                self.observed.labels(p.task_id).set(p.observed_at)

    def update_usage(self, state_dir: Path) -> None:
        self.usage.clear()
        self.usage_resets.clear()
        for provider, name in USAGE_FILES.items():
            try:
                data = json.loads((state_dir / name).read_text())
            except (OSError, ValueError):
                continue
            for key, window in WINDOWS.items():
                part = data.get(key) or {}
                used = part.get("used_percentage")
                if isinstance(used, (int, float)):
                    self.usage.labels(provider, window).set(used)
                    resets = part.get("resets_at")
                    if isinstance(resets, (int, float)) and resets > 0:
                        self.usage_resets.labels(provider, window).set(resets)


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
