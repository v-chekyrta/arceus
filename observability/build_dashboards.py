"""Write the three Grafana dashboards as JSON into grafana/dashboards/.

    python3 observability/build_dashboards.py

1. Arceus · now        the current task, the queue, health, subscription limits, latest events
2. Arceus · tasks      what happened to tickets: outcomes, failure reasons, the timeline
3. Arceus · resources  the VM: CPU, memory, pressure, disk space and disk load, network

Colours are the validated dark-mode categorical order (blue, orange, aqua, yellow) and the reserved
status steps (good, warning, critical), always paired with a text label.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).with_name("grafana") / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500"]
GOOD, WARNING, CRITICAL, MUTED = "#0ca30c", "#fab219", "#d03b3b", "#898781"

_id = 0


def _next_id() -> int:
    global _id
    _id += 1
    return _id


def grid(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def mapping(pairs):
    """[(value, text, color)] -> Grafana value mappings."""
    return [{"type": "value", "options": {str(v): {"text": t, "color": c, "index": i}
                                          for i, (v, t, c) in enumerate(pairs)}}]


def stat(title, expr, pos, *, ds=PROM, unit="none", mappings=None, color=None, desc="", instant=True, zero=False,
         steps=None, no_value=None, name=None, decimals=None):
    """`steps` [(from, colour)] colours the value by thresholds; `name` shows a label of the series instead of
    the value (a legend format such as "{{stage}}")."""
    target = {"refId": "A", "expr": expr, "datasource": ds}
    if ds is PROM:
        target["instant"] = instant
    else:
        target["queryType"] = "instant"
    panel = {
        "id": _next_id(), "type": "stat", "title": title, "description": desc, "datasource": ds, "gridPos": pos,
        "targets": [target],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": "value", "justifyMode": "center"},
        "fieldConfig": {"defaults": {
            "unit": unit, "mappings": mappings or [], "decimals": decimals,
            "color": {"mode": "fixed", "fixedColor": color} if color else {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps] if steps
                           else [{"color": color or "text", "value": None}]},
            "noValue": no_value or ("0" if zero else "–")}, "overrides": []},
    }
    if name:
        panel["options"]["textMode"] = "name"
        target["legendFormat"] = name
    return panel


def table(title, expr, pos, *, ds=PROM, rename=None, hide=(), cell_maps=None, desc="", legend=None):
    target = {"refId": "A", "expr": expr, "datasource": ds, "format": "table"}
    if ds is PROM:
        target["instant"] = True
    else:
        target["queryType"] = "instant"
    overrides = []
    for field, pairs in (cell_maps or {}).items():
        overrides.append({"matcher": {"id": "byName", "options": field}, "properties": [
            {"id": "mappings", "value": mapping(pairs)},
            {"id": "custom.cellOptions", "value": {"type": "color-text"}}]})
    excl = {"Time": True, **{h: True for h in hide}}
    return {
        "id": _next_id(), "type": "table", "title": title, "description": desc, "datasource": ds, "gridPos": pos,
        "targets": [target],
        "transformations": [{"id": "organize", "options": {"excludeByName": excl, "renameByName": rename or {}}}],
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}},
                                     "noValue": legend or "Nothing here"}, "overrides": overrides},
    }


def timeseries(title, targets, pos, *, unit="none", bars=False, single=True, desc="", minimum=None, maximum=None):
    overrides = []
    for i, t in enumerate(targets):
        if t.get("legendFormat") and not single:
            overrides.append({"matcher": {"id": "byName", "options": t["legendFormat"]},
                              "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": SERIES[i]}}]})
    return {
        "id": _next_id(), "type": "timeseries", "title": title, "description": desc,
        "datasource": targets[0]["datasource"], "gridPos": pos, "targets": targets,
        "options": {"legend": {"showLegend": not single, "displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi" if not single else "single", "sort": "desc"}},
        "fieldConfig": {"defaults": {
            "unit": unit, "min": minimum, "max": maximum,
            "color": {"mode": "fixed", "fixedColor": SERIES[0]} if single else {"mode": "palette-classic"},
            "custom": {"drawStyle": "bars" if bars else "line", "lineWidth": 2, "fillOpacity": 80 if bars else 0,
                       "barAlignment": 0, "pointSize": 8, "showPoints": "never", "spanNulls": False,
                       "axisSoftMin": 0, "gradientMode": "none"}},
            "overrides": overrides},
    }


# One readable line per event instead of the raw JSON: "task_failed  OMD-12  — reason «summary»".
RUNNER_LINE = (' | json | line_format "{{.event}}{{with .task_id}}  {{.}}{{end}}{{with .stage}}  {{.}} · {{$.state}}{{else}}{{with .state}}  {{.}}{{end}}{{end}}'
               '{{with .author}}  by {{.}}{{end}}{{with .reason}}  — {{.}}{{end}}{{with .note}}  — {{.}}{{end}}'
               '{{with .summary}}  «{{.}}»{{end}}{{with .url}}  {{.}}{{end}}"')
# "cleanup_item  remove container web-db-1 → done  — exited, made by OMD-12, which is over".
CLEANUP_LINE = (' | json | line_format "{{.event}}{{with .status}}  {{.}}{{end}}{{with .trigger}}  {{.}}{{end}}'
                '{{with .scope}} · {{.}}{{end}}{{with .action}}  {{.}} {{$.kind}} {{$.name}} → {{$.result}}{{end}}'
                '{{with .why}}  — {{.}}{{end}}{{with .kept}}  (kept: {{.}}){{end}}{{with .error}}  error: {{.}}{{end}}'
                '{{with .reason}}  — {{.}}{{end}}{{with .deferred}}  — deferred: {{.}}{{end}}"')
BULAVA_LINE = (' | json | line_format "{{.kind}}  {{.task_id}}  attempt {{.attempt}}  state {{.state}}'
               '{{with .disposition}}  verdict {{.}}{{end}}{{with .reason}}  — {{.}}{{end}}"')
# The running task, step by step: the runner's own steps, the engine's steps it carries over from the
# run's journal, the agent's activity, and Bulava's task stream (its `kind` is the event label there).
PROGRESS_EVENTS = ("task_claimed|task_dispatched|task_published|task_failed|task_done|task_needs_input|"
                   "workspace_.*|repo_cloned|engine_.*|agent_.*|attempt_.*|stalled_nudged|answer_sent|"
                   "review_sent|dispatch_busy|send_busy|followup_waits|progress_unreadable|pushed|mr_ready|"
                   "created|attempt-started|attempt-ended|stale-verdict")
PROGRESS_LINE = (' | json | line_format `{{if .kind}}engine {{.kind}}  {{.task_id}}  attempt {{.attempt}}'
                 '{{with .disposition}}  verdict {{.}}{{end}}{{with .reason}}  — {{.}}{{end}}'
                 '{{else if eq .event "engine_step"}}step  {{.task_id}}  {{.stage}} · {{.state}}'
                 '{{with .note}}  — {{.}}{{end}}{{with .ms_taken}}  ({{.}} ms){{end}}'
                 '{{else if eq .event "agent_activity"}}agent  {{.task_id}}  {{.tools}} tool calls, last {{.last_tool}}'
                 '{{with .last_target}}: {{.}}{{end}}'
                 '{{else if eq .event "agent_quiet"}}agent  {{.task_id}}  nothing written for {{.minutes}} min'
                 '{{with .step}} (step {{.}}){{end}}'
                 '{{else if eq .event "engine_state"}}engine  {{.task_id}}  {{.state}}, attempt {{.attempt}}'
                 '{{if and (eq .state "running") (eq .attempt_live "false")}}, no live session{{end}}{{with .state_reason}}  — {{.}}{{end}}'
                 '{{else if eq .event "engine_call_started"}}{{.call}}  {{.task_id}}  started'
                 '{{else if eq .event "engine_call_finished"}}{{.call}}  {{.task_id}}  {{.result}} in {{.seconds}} s'
                 '{{with .reason}}  — {{.}}{{end}}'
                 '{{else}}{{.event}}{{with .task_id}}  {{.}}{{end}}{{with .repo}}  {{.}}{{end}}'
                 '{{with .seconds}}  {{.}} s{{end}}{{with .reason}}  — {{.}}{{end}}{{with .url}}  {{.}}{{end}}{{end}}`')

def logs(title, expr, pos, desc=""):
    return {
        "id": _next_id(), "type": "logs", "title": title, "description": desc, "datasource": LOKI, "gridPos": pos,
        "targets": [{"refId": "A", "expr": expr, "datasource": LOKI, "queryType": "range"}],
        "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True,
                    "prettifyLogMessage": False, "dedupStrategy": "none"},
    }


def bargauge(title, expr, legend, pos, desc=""):
    return {
        "id": _next_id(), "type": "bargauge", "title": title, "description": desc, "datasource": PROM, "gridPos": pos,
        "targets": [{"refId": "A", "expr": expr, "instant": True, "legendFormat": legend, "datasource": PROM}],
        "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                    "text": {"titleSize": 14, "valueSize": 22},
                    "valueMode": "text", "namePlacement": "left", "minVizHeight": 16, "maxVizHeight": 24,
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        "fieldConfig": {"defaults": {
            "unit": "percent", "min": 0, "max": 100, "decimals": 0, "noValue": "No reading",
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": SERIES[0], "value": None}, {"color": WARNING, "value": 80}, {"color": CRITICAL, "value": 95}]}},
            "overrides": []},
    }


def dashboard(uid, title, panels, *, time="now-24h", refresh="30s", desc="", annotations=()):
    return {
        "uid": uid, "title": title, "description": desc, "tags": ["arceus"], "editable": False,
        "schemaVersion": 41, "version": 1, "time": {"from": time, "to": "now"}, "refresh": refresh,
        "timezone": "browser", "graphTooltip": 1, "panels": panels,
        "links": [{"type": "dashboards", "tags": ["arceus"], "asDropdown": False, "title": "Arceus",
                   "includeVars": False, "keepTime": True}],
        "annotations": {"list": list(annotations)},
    }


def row(title, y):
    return {"id": _next_id(), "type": "row", "title": title, "collapsed": False, "gridPos": grid(0, y, 24, 1),
            "panels": []}


def state_timeline(title, expr, pos, pairs, desc=""):
    """One band over time, coloured by state, e.g. the agent working or idle."""
    return {
        "id": _next_id(), "type": "state-timeline", "title": title, "description": desc, "datasource": PROM,
        "gridPos": pos, "targets": [{"refId": "A", "expr": expr, "datasource": PROM, "legendFormat": title}],
        "options": {"showValue": "never", "mergeValues": True, "alignValue": "left", "rowHeight": 0.8,
                    "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
        # A fixed colour, not thresholds: with thresholds and mergeValues Grafana turns every value into its
        # threshold band, so a single base step merges the whole range into one "-∞+" state and hides the mappings.
        "fieldConfig": {"defaults": {"mappings": mapping(pairs), "color": {"mode": "fixed", "fixedColor": MUTED},
                                     "custom": {"lineWidth": 0, "fillOpacity": 80}}, "overrides": []},
    }


def prom(expr, legend, ref="A"):
    return {"refId": ref, "expr": expr, "datasource": PROM, "legendFormat": legend}


# Where a task started and ended, as marks on every graph of a dashboard: a load spike next to one
# is the agent's work, without one it is something else.
TASK_MARKS = {
    "name": "Tasks", "enable": True, "iconColor": MUTED, "datasource": LOKI,
    "expr": '{source="runner", event=~"task_dispatched|task_published|task_done|task_failed"}',
    "target": {"refId": "Anno", "expr": '{source="runner", event=~"task_dispatched|task_published|task_done|task_failed"}',
               "queryType": "range"},
    "titleFormat": "{{event}}", "textFormat": "{{task_id}}", "tagKeys": "event",
}


def now_dashboard():
    up = mapping([(1, "Up", GOOD), (0, "Down", CRITICAL)])
    minutes = lambda warn, crit: [(None, "text"), (warn * 60, WARNING), (crit * 60, CRITICAL)]
    panels = [
        stat("Agent", "runner_agent_busy", grid(0, 0, 4, 4),
             mappings=mapping([(1, "Working", SERIES[0]), (0, "Idle", MUTED)]),
             desc="Working while a task holds the agent: taken, in Bulava, or being published. It says nothing about "
                  "whether the agent is moving; see Step, Agent quiet and Runner loop."),
        stat("Queue", "runner_queue_length", grid(4, 0, 4, 4), color="text",
             desc="Tickets tagged by an allowed person and not taken yet."),
        stat("Takes new work", "runner_can_take", grid(8, 0, 4, 4),
             mappings=mapping([(1, "Yes", GOOD), (0, "No · see health", CRITICAL)]),
             desc="No while any health check fails or YouTrack/GitLab is paused. Started tasks are still followed."),
        stat("YouTrack", 'runner_service_up{service="youtrack"}', grid(12, 0, 4, 4), mappings=up),
        stat("GitLab", 'runner_service_up{service="gitlab"}', grid(16, 0, 4, 4), mappings=up),
        stat("Mode", "runner_info", grid(20, 0, 4, 4), color="text", name="{{mode}}",
             desc="observe: reads and logs only. live: takes tasks and opens MRs."),
        stat("Step", "runner_task_step_since_seconds", grid(0, 4, 9, 4), color=SERIES[0], name="{{stage}} · {{state}}",
             no_value="No step running",
             desc="The engine's current step of the running task, from the run's own journal: preparation stages, "
                  "deliver (the brief reached the agent), gate, gate.verify (build and tests), gate.review (Codex), "
                  "outcome, run (the verdict). waiting means a usage limit or an unavailable reviewer."),
        stat("In this step", "time() - max(runner_task_step_since_seconds)", grid(9, 4, 5, 4), unit="s", color="text",
             desc="How long the task has been in the step on the left."),
        stat("Agent quiet", "max(runner_agent_quiet_seconds)", grid(14, 4, 5, 4), unit="s",
             steps=minutes(10, 20), no_value="Not its turn",
             desc="How long the agent has written nothing to its transcript while the step is its own. Blank while "
                  "the gate, Codex or a usage limit has the turn. Red from 20 min: the runner also logs agent_quiet."),
        stat("Runner loop", "time() - runner_last_tick_seconds", grid(19, 4, 5, 4), unit="s",
             steps=minutes(5, 16), no_value="No data",
             desc="How long ago the runner finished its last pass. It passes every 30 s while a task runs; a call to "
                  "the engine can hold it up to 15 min. Older than that, the runner itself is stuck or down."),
        table("Tasks in progress", "runner_task", grid(0, 8, 14, 8),
              rename={"task_id": "Ticket", "state": "Runner state", "summary": "Summary"},
              hide=("__name__", "instance", "job", "Value"), legend="No open tasks",
              desc="Every task that is not over: taken, running, being published, waiting for an answer, in review."),
        bargauge("Subscription usage", "runner_usage_percent", "{{provider}} · {{window}}", grid(14, 8, 10, 8),
                 desc="Read by Bulava from Claude and Codex. Work stops when a provider refuses; this shows how close."),
        logs("Task progress", f'{{source=~"runner|bulava", event=~"{PROGRESS_EVENTS}"}}' + PROGRESS_LINE,
             grid(0, 16, 24, 13),
             desc="The running task step by step, newest first: what the runner did, each step of the engine, the "
                  "agent's tool calls every minute (names and its own one-line descriptions, never commands), and "
                  "Bulava's attempt stream."),
        table("Health checks", "sort_desc(runner_health)", grid(0, 29, 10, 12),
              rename={"check": "Check", "Value": "Status"}, hide=("__name__", "instance", "job"),
              cell_maps={"Status": [(0, "OK", GOOD), (1, "Warn", WARNING), (2, "Fail", CRITICAL)]},
              desc="The runner's own checks and Bulava's doctor --server. Any Fail stops new work."),
        logs("Latest runner events", '{source="runner", event!~"health_changed|agent_activity"}' + RUNNER_LINE,
             grid(10, 29, 14, 12),
             desc="events.jsonl of the runner, newest first, without the agent's minute-by-minute activity."),
    ]
    return dashboard("arceus-now", "Arceus · now", panels, time="now-6h",
                     desc="What the agent is doing right now, and whether it can take more.")


def tasks_dashboard():
    count = 'sum(count_over_time({{source="runner", event="{e}"}}[$__range]))'
    outcome = lambda e, legend: {"refId": e, "datasource": LOKI, "legendFormat": legend, "queryType": "range",
                                 "expr": f'sum(count_over_time({{source="runner", event="{e}"}}[1d]))'}
    panels = [
        stat("Taken", count.format(e="task_claimed"), grid(0, 0, 6, 4), ds=LOKI, color="text", zero=True),
        stat("MR opened", count.format(e="task_published"), grid(6, 0, 6, 4), ds=LOKI, color="text", zero=True),
        stat("Done, no code change", count.format(e="task_done"), grid(12, 0, 6, 4), ds=LOKI, color="text",
             zero=True),
        stat("Failed", count.format(e="task_failed"), grid(18, 0, 6, 4), ds=LOKI, color="text", zero=True),
        stat("Review rounds sent", count.format(e="review_sent"), grid(0, 27, 8, 4), ds=LOKI, color="text", zero=True,
             desc="Follow-ups sent to the agent from MR reviews, red CI, conflicts and ticket comments."),
        stat("MRs merged", count.format(e="task_merged"), grid(8, 27, 8, 4), ds=LOKI, color="text", zero=True),
        stat("Stopped by hand", count.format(e="task_stopped"), grid(16, 27, 8, 4), ds=LOKI, color="text", zero=True,
             desc="The ticket was moved past Code Review by a person, so the runner stopped watching it."),
        timeseries("Outcomes per day", [outcome("task_published", "MR opened"), outcome("task_failed", "Failed"),
                                        outcome("task_done", "Done, no code change")],
                   grid(0, 4, 14, 9), bars=True, single=False),
        table("Why tasks failed", 'sum by (reason) (count_over_time({source="runner", event="task_failed"} '
                                  '| json reason="reason" [$__range]))', grid(14, 4, 10, 9), ds=LOKI,
              rename={"reason": "Reason", "Value #A": "Tasks", "Value": "Tasks"}, legend="No failures in this period"),
        logs("Task timeline", '{source="runner", event=~"task_.*|pushed|mr_ready|answer_sent|attempt_resumed|'
                              'trigger_rejected|issue_skipped|notify_pending|reconcile_failed|review_.*|mr_state|'
                              'branch_.*"}' + RUNNER_LINE,
             grid(0, 13, 14, 14), desc="Every step the runner took for tickets, newest first."),
        logs("Engine events", '{source="bulava"}' + BULAVA_LINE, grid(14, 13, 10, 14),
             desc="Bulava's own task stream: attempts started and ended, verdicts."),
    ]
    return dashboard("arceus-tasks", "Arceus · tasks", panels, time="now-7d", refresh="1m",
                     desc="What happened to the tickets the agent took.")


def resources_dashboard():
    root = 'mountpoint="/",fstype!="rootfs"'
    used = f"node_filesystem_size_bytes{{{root}}} - node_filesystem_avail_bytes{{{root}}}"
    disk = 'device=~"(sd|vd|xvd|nvme)[a-z0-9]+"'  # whole disks; node_exporter already leaves partitions out
    nic = 'device!~"lo|docker.*|br-.*|veth.*"'    # the VM's own interfaces, not Docker's bridges
    cpu_mode = lambda m: f'100 * avg(rate(node_cpu_seconds_total{{mode="{m}"}}[5m]))'
    cores = 'count(node_cpu_seconds_total{mode="idle"})'
    pct = lambda warn, crit: [(None, "text"), (warn, WARNING), (crit, CRITICAL)]
    panels = [
        # At a glance: each tile turns amber, then red, where it starts to hurt a task.
        stat("CPU", f"100 - {cpu_mode('idle')}", grid(0, 0, 3, 4), unit="percent", decimals=0, steps=pct(70, 90),
             desc="All cores, averaged over 5 min."),
        stat("Memory", "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)", grid(3, 0, 3, 4),
             unit="percent", decimals=0, steps=pct(80, 90),
             desc="Used, by what the kernel counts as available. There is no swap: past 100% processes are killed."),
        stat("Disk used, /", f"100 * ({used}) / node_filesystem_size_bytes{{{root}}}", grid(6, 0, 3, 4),
             unit="percent", decimals=0, steps=pct(75, 90),
             desc="Amber and red where the runner's health check warns (25% free) and stops new work (10% free)."),
        stat("Disk busy", f"100 * max(rate(node_disk_io_time_seconds_total{{{disk}}}[5m]))", grid(9, 0, 3, 4),
             unit="percent", decimals=0, steps=pct(60, 85),
             desc="Share of time the disk had requests in flight, over 5 min. Near 100% the disk is the bottleneck."),
        stat("Waiting on I/O", "100 * rate(node_pressure_io_waiting_seconds_total[5m])", grid(12, 0, 3, 4),
             unit="percent", decimals=0, steps=pct(10, 25),
             desc="Share of time at least one task was stalled waiting for the disk (Linux pressure stall information)."),
        stat("Load per core", f"max(node_load5) / {cores}", grid(15, 0, 3, 4), decimals=2, steps=pct(0.8, 1.5),
             desc="5-minute load average divided by the number of cores. Above 1, work queues for the CPU."),
        stat("OOM kills, 7 days", "increase(node_vmstat_oom_kill[7d])", grid(18, 0, 3, 4), decimals=0, zero=True,
             steps=[(None, "text"), (1, CRITICAL)],
             desc="Processes the kernel killed for lack of memory in the last week. Any is a reason to look."),
        stat("Up for", "time() - node_boot_time_seconds", grid(21, 0, 3, 4), unit="dtdurations", color="text",
             desc="Time since the VM booted. A short one explains a gap in the graphs."),
        state_timeline("Agent", "runner_agent_busy", grid(0, 4, 24, 3),
                       [(1, "Working", SERIES[0]), (0, "Idle", MUTED)],
                       desc="When a task held the agent. The graphs below share its time axis; task starts and ends "
                            "are also marked on every graph."),

        row("CPU and memory", 7),
        timeseries("CPU by kind of work", [prom(cpu_mode("user"), "User", "A"), prom(cpu_mode("system"), "System", "B"),
                                           prom(cpu_mode("iowait"), "Waiting on disk", "C"),
                                           prom(cpu_mode("steal"), "Taken by the host", "D")],
                   grid(0, 8, 12, 8), unit="percent", single=False, minimum=0,
                   desc="User: programs (Claude, Codex, tests, the dev stack). Waiting on disk: idle CPU with I/O "
                        "outstanding. Taken by the host: time the hypervisor gave to other VMs."),
        timeseries("Memory", [prom("node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes", "Used", "A"),
                              prom("node_memory_Cached_bytes + node_memory_Buffers_bytes", "Cache and buffers", "B"),
                              prom("node_memory_MemAvailable_bytes", "Available", "C")],
                   grid(12, 8, 12, 8), unit="bytes", single=False, minimum=0,
                   desc="Cache is given back when programs need it; Available already counts it."),
        timeseries("Pressure: share of time work was stalled",
                   [prom("rate(node_pressure_cpu_waiting_seconds_total[5m])", "Waiting for CPU", "A"),
                    prom("rate(node_pressure_memory_waiting_seconds_total[5m])", "Waiting for memory", "B"),
                    prom("rate(node_pressure_io_waiting_seconds_total[5m])", "Waiting for disk", "C")],
                   grid(0, 16, 12, 8), unit="percentunit", single=False, minimum=0,
                   desc="Linux pressure stall information: of all time, how much at least one task could not run "
                        "for lack of that resource. The direct answer to which resource slows the agent down."),
        timeseries("Load average", [prom("node_load1", "1 min", "A"), prom("node_load5", "5 min", "B"),
                                    prom("node_load15", "15 min", "C"), prom(cores, "Cores", "D")],
                   grid(12, 16, 12, 8), single=False, minimum=0,
                   desc="Runnable and disk-blocked tasks. Above the Cores line, work waits."),

        row("Disk", 24),
        timeseries("Disk throughput", [prom(f"sum(rate(node_disk_read_bytes_total{{{disk}}}[5m]))", "Read", "A"),
                                       prom(f"sum(rate(node_disk_written_bytes_total{{{disk}}}[5m]))", "Written", "B")],
                   grid(0, 25, 8, 8), unit="Bps", single=False, minimum=0,
                   desc="Bytes per second. Clones, Docker images and test databases show up here."),
        timeseries("Disk operations", [prom(f"sum(rate(node_disk_reads_completed_total{{{disk}}}[5m]))", "Reads", "A"),
                                       prom(f"sum(rate(node_disk_writes_completed_total{{{disk}}}[5m]))", "Writes", "B")],
                   grid(8, 25, 8, 8), unit="iops", single=False, minimum=0,
                   desc="Requests per second. Many small writes (git, package installs) load a disk more than their bytes suggest."),
        timeseries("Disk busy", [prom(f"100 * max(rate(node_disk_io_time_seconds_total{{{disk}}}[5m]))", "Busy", "A")],
                   grid(16, 25, 8, 8), unit="percent", minimum=0, maximum=100,
                   desc="Share of time the disk had requests in flight."),
        timeseries("Time per operation",
                   [prom(f"1000 * sum(rate(node_disk_read_time_seconds_total{{{disk}}}[5m])) / "
                         f"sum(rate(node_disk_reads_completed_total{{{disk}}}[5m]))", "Read", "A"),
                    prom(f"1000 * sum(rate(node_disk_write_time_seconds_total{{{disk}}}[5m])) / "
                         f"sum(rate(node_disk_writes_completed_total{{{disk}}}[5m]))", "Write", "B")],
                   grid(0, 33, 8, 8), unit="ms", single=False, minimum=0,
                   desc="Average latency of one request. A rise without more operations means the disk (or the "
                        "host's storage) is slow, not that we ask more of it."),
        timeseries("Disk queue", [prom(f"sum(rate(node_disk_io_time_weighted_seconds_total{{{disk}}}[5m]))",
                                       "Requests waiting", "A")],
                   grid(8, 33, 8, 8), minimum=0,
                   desc="Average number of requests queued or in service."),
        timeseries("Processes blocked on I/O", [prom("node_procs_blocked", "Blocked", "A")], grid(16, 33, 8, 8),
                   minimum=0, desc="Processes in uninterruptible sleep, almost always waiting for the disk."),
        timeseries("Disk used, /", [prom(f"100 * ({used}) / node_filesystem_size_bytes{{{root}}}", "Used", "A")],
                   grid(0, 41, 12, 8), unit="percent", minimum=0, maximum=100),
        stat("Disk free, /", f"node_filesystem_avail_bytes{{{root}}}", grid(12, 41, 3, 8), unit="bytes",
             color="text"),
        stat("Growth, 7 days", f"({used}) - ({used} offset 7d)", grid(15, 41, 3, 8), unit="bytes", color="text",
             desc="How much more disk is used than a week ago. Blank until there are 7 days of data."),
        # Only with a week of history: a few hours of clones extrapolate to a full disk that is not coming.
        stat("Free in 30 days", f"predict_linear(node_filesystem_avail_bytes{{{root}}}[7d], 30 * 86400) "
                                f"and node_filesystem_avail_bytes{{{root}}} offset 7d",
             grid(18, 41, 3, 8), unit="bytes", steps=[(None, CRITICAL), (10 * 2**30, WARNING), (20 * 2**30, "text")],
             desc="Free space in 30 days if the last week's trend goes on. Red when it would run out. "
                  "Blank until there are 7 days of data."),
        stat("Inodes used, /", f"100 * (1 - node_filesystem_files_free{{{root}}} / node_filesystem_files{{{root}}})",
             grid(21, 41, 3, 8), unit="percent", decimals=0, steps=pct(75, 90),
             desc="Files and folders, not bytes: node_modules and git objects can run out of these first."),

        row("Network", 49),
        timeseries("Network traffic", [prom(f"sum(rate(node_network_receive_bytes_total{{{nic}}}[5m]))", "Received", "A"),
                                       prom(f"sum(rate(node_network_transmit_bytes_total{{{nic}}}[5m]))", "Sent", "B")],
                   grid(0, 50, 12, 8), unit="Bps", single=False, minimum=0,
                   desc="The VM's own interfaces: clones, pushes, model API calls, image pulls."),
        timeseries("Network errors and drops",
                   [prom(f"sum(rate(node_network_receive_errs_total{{{nic}}}[5m]) + rate(node_network_transmit_errs_total{{{nic}}}[5m]))",
                         "Errors", "A"),
                    prom(f"sum(rate(node_network_receive_drop_total{{{nic}}}[5m]) + rate(node_network_transmit_drop_total{{{nic}}}[5m]))",
                         "Dropped", "B")],
                   grid(12, 50, 12, 8), unit="pps", single=False, minimum=0,
                   desc="Packets per second. Errors should stay at zero: a steady line explains timeouts to GitLab, "
                        "YouTrack or the model APIs. A low, flat drop rate is usually broadcast traffic nobody listens "
                        "to; a jump during a task is not."),

        row("Cleanup", 58),
        stat("Last full cleanup", 'time() - runner_cleanup_last_success_seconds{scope="full"}', grid(0, 59, 6, 4),
             unit="s", steps=[(None, "text"), (26 * 3600, WARNING), (50 * 3600, CRITICAL)], no_value="None yet",
             desc="Since the last full pass that finished without errors. One runs at least daily, in the first free "
                  "moment; amber past 26 h, red past 50 h."),
        stat("Last pass", "runner_cleanup_last_run_seconds", grid(6, 59, 6, 4), color="text",
             name="{{status}} · {{mode}}", no_value="None yet",
             desc="partial: some removals failed and are retried later (see the log). report: the pass only logs "
                  "what it would remove (cleanup.mode: report, or the runner in observe)."),
        stat("Not put down to a task", "runner_cleanup_unknown_items", grid(12, 59, 6, 4), decimals=0, zero=True,
             steps=[(None, "text"), (1, WARNING)],
             desc="Leftovers the last pass found but could not tie to a finished task, so it left them: the dev "
                  "stack, a person's work, or something to add to cleanup.protect. `arceus-runner cleanup --last` "
                  "lists them."),
        stat("Freed by the last pass", "runner_cleanup_freed_bytes", grid(18, 59, 6, 4), unit="bytes", color="text",
             no_value="Nothing removed yet", desc="Free disk space after the last acting pass minus before it."),
        logs("Cleanup", '{source="runner", event=~"cleanup_.*"}' + CLEANUP_LINE, grid(0, 63, 24, 10),
             desc="What each pass removed, kept on a second look, failed to remove, or deferred, and why."),
    ]
    return dashboard("arceus-resources", "Arceus · resources", panels, time="now-24h", refresh="1m",
                     annotations=[TASK_MARKS],
                     desc="The VM itself: whether CPU, memory, disk or network slows the agent down, and when.")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in (("arceus-now", now_dashboard), ("arceus-tasks", tasks_dashboard),
                        ("arceus-resources", resources_dashboard)):
        (OUT / f"{name}.json").write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {OUT / name}.json")


if __name__ == "__main__":
    main()
