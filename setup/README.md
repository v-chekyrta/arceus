# Preparing the VM

`setup.sh` prepares a VM for the runner and the Bulava engine. Everything runs as the user who owns this folder and runs `sudo ./setup.sh`: the runner, Bulava, `claude`, `codex` and the project's Docker stack. There is no user to configure. Setup creates no users and does not change anyone's sudo. It stops with an explanation if the folder belongs to someone else, if it is run directly as root, or if the services are already installed for another user.

Everything Arceus keeps on the machine is in one folder, `~/arceus`:

```
~/arceus/
  setup/          installation: setup.sh, setup.conf, checks/
  runner/         runner source (setup installs it into /opt/arceus-runner)
  config/         example configuration and the agent's server notes
  docs/           how the parts work (task lifecycle, reviews, QA, cleanup, security)
  observability/  Grafana, Prometheus, Loki and Alloy (setup installs it into /opt/arceus-observability)
  data/           your settings and the runner's state: config.yaml, secrets.env, project-notes.md, runner.db, events.jsonl
  workspace/      clones of the project's repositories (the path is paths.workspace in config.yaml)
```

`setup/`, `runner/`, `config/`, `docs/` and `observability/` are a copy of this repository and are replaced as a whole on update. `data/` and the workspace are never touched by an update; `data/` is the only place you edit.

```bash
rsync -a --delete --exclude .venv --exclude __pycache__ --exclude setup.conf \
  runner setup config docs observability <user>@<vm>:arceus/
ssh <user>@<vm>
cd ~/arceus/setup
cp setup.conf.example setup.conf     # pinned versions and the Bulava commit; nothing else to fill in
sudo ./setup.sh --check              # only checks, changes nothing
sudo ./setup.sh                      # does whatever is not done yet
```

The script runs in phases: `packages legacy docker chrome agent_tools mcp_config dirs bulava bulava_install agent_reports agent_notes runner systemd observability console`. Each phase checks the machine first, acts only if the check fails, and checks again afterwards. A second run therefore changes nothing, and `--check` after an interrupted run shows which phases are unfinished (`todo`). One phase can be run alone: `--phase docker`.

Setup never overwrites `data/config.yaml`, `secrets.env`, `bulava.env` or `project-notes.md`: the examples are copied there once, only while the files do not exist. It never touches `runner.db` or the workspace either. If an older layout left `~/runner` and an empty `~/workspace`, the `legacy` phase moves them into `~/arceus` without overwriting anything.

## Where things are

| Path | Owner | What it is |
|---|---|---|
| `/opt/agent-tools` | root, read-only | `claude`, `codex` and `chrome-devtools-mcp` at pinned versions, linked from `/usr/local/bin`; `workspace-mcp/`, the Google MCP server in a virtualenv of its own. The only `claude` and `codex` on the machine: setup removes the copies Claude's native installer (`~/.local/bin/claude`, `~/.local/share/claude`) and `npm i -g` under nvm leave in the user's home, and the services find `/usr/local/bin` first and run with `DISABLE_AUTOUPDATER=1`. To update them, change the versions in `setup.conf` and re-run setup; `claude update` and `codex update` do not apply here |
| `/opt/arceus-runner` | root, read-only | the runner's code and virtualenv |
| `/opt/bulava/engine` | root, read-only | the Bulava engine at `BULAVA_REF`: a full commit (the default), a tag or a branch the server follows (for example `factory`). A new commit is installed by the next `sudo ./setup.sh`, never while a task runs. The engine's `install.sh` runs as the user and writes to their `~/.claude` (worker hooks, status line) and `~/.local/bin` |
| `/var/lib/arceus/bulava.git` | root | a mirror of the Bulava repository that keeps every commit setup installed (`refs/arceus/<commit>`), so a pinned commit stays installable after Bulava rewrites its branches |
| `/opt/bulava-local/mcp.json` | root | the MCP servers of the agent's sessions (`SUPERVISOR_AUTOMATION_MCP_CONFIG`): the headless browser without `npx`, and `google` once a service account's key is in place |
| `/opt/arceus-observability` | root | the Docker Compose stack for Grafana, Prometheus, Loki, Alloy and node_exporter |
| `/etc/systemd/system/arceus-ops.*`, `arceus-ops-tick.*` | root | the host's side of the console's operations: a path unit and a oneshot that take its requests from a fixed list, and a timer that marks lost operations interrupted; the ledger is `/var/lib/arceus/ops` ([docs/console.md](../docs/console.md#operations)) |
| `/opt/arceus-console` | root | the web console's Docker Compose project (the console and nginx), its `.env`, `nginx.conf` and `mounts.yaml` written from `config.yaml`, and the image's build folder. Only while `console.enabled` is true ([docs/console.md](../docs/console.md)) |
| `/etc/systemd/system/bulava-engine.service` | root | the tmux server the workers run in (`tmux -L bulava`). Installed; the operator starts it, and setup keeps a running one enabled for reboots |
| `/etc/systemd/system/runner.service` | root | the runner. `KillMode=process`: restarting it does not kill the engine's watchdogs. Installed; the operator starts it, and setup keeps a running one enabled for reboots |
| `~/arceus/data/` | the user, 700 | `config.yaml`, `secrets.env` (600), `runner.db`, `events.jsonl`, `grafana-admin-password`; `google/` (700): the Google service account's key |
| `~/.git-credentials` | the user, 600 | the code host bot's credentials, for that host only; written by the runner at start |
| `/var/lib/arceus/facts.json` | root | what is actually installed: OS, versions, the user, the status of each phase |

## Signing in

Claude and Codex are signed in once, the usual way, as the user that ran setup. No tokens need copying anywhere: the runner and Bulava use the sign-in in that user's home.

```bash
claude                             # inside: /login with the subscription account (~/.claude/.credentials.json, refreshed by Claude)
codex login --device-auth          # ~/.codex/auth.json
```

Do not use `claude setup-token` or the `CLAUDE_CODE_OAUTH_TOKEN` variable for this. Such a token can call the model but cannot read the subscription's usage: the usage endpoint answers `403` (it needs the `user:profile` scope). Without usage readings Bulava still stops when Claude reports that a limit is reached, but it does not know when the limit resets, so it retries every 30–60 minutes instead of at the reset time. If the variable is set it also overrides `/login`, so it must be in neither `~/.bashrc` nor `~/arceus/data/bulava.env`. That file stays an empty example.

## Checks before taking work

```bash
nano ~/arceus/data/config.yaml                          # tracker (YouTrack project), code_host (GitLab group), allowed_users, mode
nano ~/arceus/data/secrets.env                          # YOUTRACK_TOKEN and GITLAB_TOKEN of the bots, SLACK_WEBHOOK_URL
/opt/arceus-runner/venv/bin/arceus-runner check         # the config against the live tracker and code host
/opt/arceus-runner/venv/bin/arceus-runner slack-test    # one test message in the project's Slack channel
/opt/arceus-runner/venv/bin/arceus-runner doctor        # all ok: bulava.auth.*, bulava.usage.* with numbers
SUPERVISOR_PROFILE=server night-shift doctor --server --json --probe   # one real request to each model
```

`check` prints one line per mistake and names the field: a state that is not in the workflow, a tag that does not exist, a token that belongs to someone else, a repository that is not in the group. The runner does not start while any line is `FAIL`.

`config.yaml` names the issue tracker in `tracker:` and the code host in `code_host:`, each with a `type` (`youtrack`, `gitlab`). The keys at the top of each section mean the same for any provider; the rest belong to that type ([config.example.yaml](../config/config.example.yaml)).

`runner.db` belongs to the tracker and the code host it first ran with: its tasks, pending comments and MR ids only make sense there. If `config.yaml` later names another one, the runner refuses to start and says so. A new tracker or host gets its own `paths.db`. The same one under a new address (a moved YouTrack, a renamed GitLab host) is bound again with `arceus-runner rebind tracker` or `rebind code_host`, with the runner stopped.

More checks, each exits 0 only if everything it looks at is right:

```bash
python3 -I ~/arceus/setup/checks/browser-mcp.py                                      # Chrome really opens a page via the installed chrome-devtools-mcp
/opt/arceus-runner/venv/bin/python -I ~/arceus/setup/checks/youtrack-roundtrip.py ACME-3  # YouTrack: every write the runner makes, on a test ticket, then puts it back
/opt/arceus-runner/venv/bin/python -I ~/arceus/setup/checks/gitlab-protection.py     # GitLab: the bot may push and force-push its task branches (FAIL otherwise); warns where it could also push, merge or force-push the default branch
```

While it runs, the runner calls `night-shift doctor --server` at start and every 10 minutes, checks the code host token's expiry every hour (where the host reports it, as GitLab does), and runs `health.devstack_check` (if set) every `poll_local`. While any check fails it takes no new tasks; tasks already started are still followed.

## Models and depth

Four keys in the `bulava` section of `config.yaml` choose them for new tasks:

```yaml
bulava:
  claude_model: auto     # auto | opus | sonnet | haiku | fable | an exact id
  claude_effort: auto    # auto | none | low | medium | high | xhigh | max | ultracode
  codex_model: auto      # auto | a slug from `night-shift models codex`
  codex_effort: auto     # auto | low | medium | high | xhigh | max | ultra
```

`auto` passes nothing to the engine. Depth is then `high`, and the model is whatever the CLI picks by default for the server's account, or `model` from `~/.claude/settings.json` / `~/.codex/config.toml` if one is set there. Any other value goes to the engine as `SUPERVISOR_{CLAUDE,CODEX}_{MODEL,EFFORT}`. The Codex pair applies to every Codex call in a task: review, consultations, preflight and the report. `none` sends no `--effort` (Haiku has no depth). An empty value is refused, because the engine would quietly turn it into `high`.

`config.yaml` is the only place these are set. The runner starts the engine with `NIGHT_SHIFT_SETTINGS=off`, so `~/.config/night-shift/config.json` is not read, and it drops these four names from `bulava.env`. A task keeps the settings it started with: the engine saves them in its `run-env`, and follow-ups, `/continue` and resumes after a limit read them back from there. A change takes effect for the next task after `sudo systemctl restart runner`. If a model or depth is set and the installed engine does not take these variables, `check` shows `FAIL` on `config.models` and `live` does not start.

Each `task_dispatched` event in `events.jsonl` records what was asked for. What actually ran is in the CLIs' transcripts. This reads them and groups the sessions by ticket, and it also works for tasks that ran before these keys existed:

```bash
/opt/arceus-runner/venv/bin/python -I ~/arceus/setup/checks/model-history.py            # per ticket: model·depth, share of answers
/opt/arceus-runner/venv/bin/python -I ~/arceus/setup/checks/model-history.py --sessions --since 2026-10-01
```

`?` means the CLI recorded no depth (older Claude Code versions do not write `effort`). Treat it as unknown, not as the default.

## Watching it: Grafana

With the web console on, Grafana is its **Monitoring** (`/grafana/`), behind its sign-in ([docs/console.md](../docs/console.md)). Without it, Grafana, Prometheus and Loki listen on 127.0.0.1 only. Open them through an SSH tunnel:

```bash
ssh -L 3000:127.0.0.1:3000 <user>@<vm>        # then http://localhost:3000
ssh -L 3000:127.0.0.1:3000 -L 3300:127.0.0.1:3300 <user>@<vm>   # with the test runs' reports (qa) too
ssh -L 3000:127.0.0.1:3000 -L 3301:127.0.0.1:3301 <user>@<vm>   # with the architecture reviews (architect) too
```

Viewing needs no login. The admin password is in `~/arceus/data/grafana-admin-password`. There are three dashboards in the **Arceus** folder:

- **Arceus · now** (the home page): whether the agent is working and on what engine step, the queue, whether new work is taken, the tracker and the code host, subscription usage, health checks and the latest events.
- **Arceus · tasks**: tasks taken, MRs opened, outcomes per day, why tasks failed, every ticket's timeline.
- **Arceus · resources**: CPU, memory, disk, network and the cleanup's passes, with task starts and ends marked on every graph.

The runner serves Prometheus metrics on `127.0.0.1:9464`; Alloy ships `~/arceus/data/events.jsonl` and Bulava's task events to Loki. The runner copies the running task's engine steps into its own events as `engine_step`, with `agent_activity` (tool names and the agent's one-line descriptions once a minute, never commands or output) and `agent_quiet`. The dashboards are generated by `observability/build_dashboards.py`; edit that file, not the JSON.

## The project's Slack channel

The runner reports to the project's channel through a Slack incoming webhook: what the agent starts, asks, finishes and fails, what it has been doing, when it has nothing to do and when something is broken. Create the webhook in a Slack app (Incoming Webhooks → Add New Webhook to Workspace → the channel), put its URL into `secrets.env` as `SLACK_WEBHOOK_URL` and keep `slack.webhook_env` in `config.yaml`. The URL is the credential: anyone who has it can post there, and Slack revokes one it finds leaked. The runner keeps it out of every log, event and error. A Workflow Builder trigger is refused: it takes another kind of message.

| What | When | How often |
|---|---|---|
| Started, back at work (after an answer, review notes, a security reviewer, `/continue`) | the task's state changes | once per change |
| Question from the agent: its words, the options, the link to answer on the ticket, a mention of who tagged the ticket | the task waits for an answer | at once |
| Merge requests out, done (merged, no code changes, research answered), stopped | the task's state changes | once per change |
| Failed, with the reason and how to resume; held by the security screen, with a mention of `security.reviewers` | the task's state changes | at once |
| What a running task did: tool calls and how many came back with an error, the engine's step, the latest target | the agent or the engine did something | at most every `summary_every` (30m), working hours only by default |
| The agent has written nothing on its own turn | `limits.agent_quiet_after` (20m) | once per quiet spell |
| Paused for a usage limit | the limit runs out | once per pause |
| Reminder of an unanswered question or a held text, with the mention | every `question_reminder` (24h) | working hours only |
| Nothing to take: the queue is empty, what waits on people, and what the agent can be given (the queue tag, research, `/continue`, code review on request: how to ask and what it does, or that it is off and how to turn it on; with MRs waiting on people, that the agent can review them) | `idle_after` (15m) with nothing running, queued or waiting on a person | once per idle spell, working hours only; a tracker outage does not start a new spell |
| Status: in work, waiting for answers, in code review, failed in the last day, the queue | the first pass of each working day | once a day |
| A health check fails; the tracker or the code host does not answer for `service_down_after` (10m); a warning such as low disk or an expiring token; each one back to normal | the change | once per change |
| A pass that raised | the error | at most once an hour |
| Runner started (and how long it was down), stopped: off by default, `runner_lifecycle: true` turns it on | start and stop; restarts closer than `restart_quiet` (10m) are counted into the next start | once per start or stop |

The settings are under `slack.notify` in [config.example.yaml](../config/config.example.yaml). Mentions come only from `slack.people`, an optional map of tracker login → Slack member ID: the webhook's text carries `<@U012AB3CD>` and Slack pings that member, no bot or token involved; left empty, people are named as plain text and nobody is pinged; text from tickets and from the agent is escaped, so an `<!channel>` in it stays text. Questions are still answered on the ticket: the webhook only posts.

Messages are queued in `runner.db`, so a Slack outage stops no work: they go out when Slack answers again, at most one a second. A question whose task has moved on is dropped, and routine messages not delivered in 2 hours are dropped too. A post that timed out may arrive twice; each message carries a number (`#123`). A webhook Slack refuses (revoked, channel archived) shows as `WARN slack.webhook` in `doctor` and on the dashboards and never stops new tasks; replace the URL and restart.

A runner that is not running cannot say so. A working day without the morning status means it is not running (with `runner_lifecycle: true` the next start also says how long it was down); an alert on `runner_last_tick_seconds` in Grafana catches it sooner.

## Google documents

Teams often keep the requirements outside the ticket. The description may be only a link to a row of a planning sheet. That row's cells hold the definition of done and the tech notes, and a link inside a cell leads on to the client's specification in a Google Doc. The agent reads these itself through a Google MCP server, [workspace-mcp](https://pypi.org/project/workspace-mcp/) at `WORKSPACE_MCP_VERSION` from `setup.conf`. It is an agent that reads them, not the runner, because only an agent can tell which part of a free-form sheet or document the ticket means and which links to follow.

- **The account:** a Google service account. Its key is `~/arceus/data/google/service-account.json`, and the server acts as the account itself (`USER_GOOGLE_EMAIL` is its own address; no domain-wide delegation). It reads only what is shared with its address (`…@….iam.gserviceaccount.com`, view is enough), so every document the agent needs has to be shared with it, the client's included. The key does not expire.
- **What the agent can do:** the server runs with `--read-only --tools sheets docs drive`: sheet values with each cell's links and notes, a doc as Markdown, Drive search and files. It cannot start a sign-in of its own (`start_google_auth` is disabled).
- **Permissions:** an unattended run may call the server (`SUPERVISOR_ALLOW_TOOLS=google`, set by both the runner and the engine unit). `~/arceus/data/google` is one of the server's secret paths, so the agent's own tools do not read the key. The server process reads it, running as the same user.
- **What is not checked:** what the documents say is not screened. They are the client's words.

To set it up:

1. In the Google Cloud project of the service account, enable the Google Sheets API, the Google Docs API and the Google Drive API. Reading a doc as Markdown uses both Docs and Drive. A disabled API answers 403 "has not been used in project".
2. Put the key at `~/arceus/data/google/service-account.json` (`chmod 600`) and run `sudo ./setup.sh`. It installs the server and adds `google` to `mcp.json`. Restart `bulava-engine` and `runner` between tasks.
3. Share the project's planning sheet and specifications with the account's address.
4. Tell the agent how the project keeps its documents in `project-notes.md`: which account to ask for sharing, and that a link set on a cell shows only when `read_sheet_values` is called with `include_hyperlinks`.

## Starting

Start with `mode: observe`. In this mode the runner only reads the queue and records in `events.jsonl` (and Grafana) what it would take:

```bash
sudo systemctl enable --now bulava-engine runner
```

When observing shows what you expect, set `mode: live` and run `sudo systemctl restart runner`. If the installed engine lacks a contract the runner needs, `live` does not start and lists what is missing (`arceus-runner probe-bulava`).

## Reboots

A reboot ends the agent's session: Claude, Codex and their tmux server stop with the machine. The task itself survives. Its record, the workspace with uncommitted files and the runner's database are all on disk. When the machine is back, `bulava-engine` and then `runner` start on their own, and on its first pass the runner finds the task `running` with no live session. It sends it on with `continue` in a fresh session, in the same workspace and on the same branch. A cut-off code review is sent on the same way, and whatever it had written before the reboot does not count as a verdict. The hours the machine was down do not count toward `attempt_timeout`. Nothing gets published twice: a push, MR or comment cut off halfway is repeated only after checking what the code host already has.

This needs both services enabled, not just started. `setup.sh` enables for good any that are running but not enabled, or enabled only with `--runtime` (that one is forgotten at the next boot), and `--check` lists them as `todo`. `arceus-runner doctor` and the `autostart` health check warn about a service that will not come back after a reboot.

## How it works

The parts the operator configures but rarely reads end to end:

- [docs/task-lifecycle.md](../docs/task-lifecycle.md): how a ticket goes in `live`, from the tag to the merged MR, `/continue`, research tickets, who pushes and describes the MR;
- [docs/reviews.md](../docs/reviews.md): Codex as the only reviewer before a merge request, and code review on request (`review_agent`);
- [docs/qa.md](../docs/qa.md): testing the platform (`qa`);
- [docs/architect.md](../docs/architect.md): architecture reviews (`architect`);
- [docs/console.md](../docs/console.md): the web console, its people and roles (`console`);
- [docs/cleanup.md](../docs/cleanup.md): what the runner removes between tasks and how to switch it on (`cleanup`);
- [docs/security.md](../docs/security.md): the security screen (`security`) and the trust boundary;
- [docs/agent-instructions.md](../docs/agent-instructions.md): the server notes, the project's notes and how they reach the agent;
- [docs/integrations.md](../docs/integrations.md): trackers and code hosts, and adding one.

## Not included yet

- **An outbound nftables allowlist**: a phase of its own, with a timed rollback so SSH is never lost.
- **An alert on the runner itself not running** (Grafana on `runner_last_tick_seconds`).
- The metrics that need MR history: merged without rework, first CI green, reverts.
