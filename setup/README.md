# Preparing the VM

`setup.sh` prepares a VM for the runner and the Bulava engine. Everything runs as the user who owns this folder and runs `sudo ./setup.sh`: the runner, Bulava, `claude`, `codex` and the project's Docker stack. There is no user to configure. Setup creates no users and does not change anyone's sudo. It stops with an explanation if the folder belongs to someone else, if it is run directly as root, or if the services are already installed for another user.

Everything Arceus keeps on the machine is in one folder, `~/arceus`:

```
~/arceus/
  setup/          installation: setup.sh, setup.conf, checks/
  runner/         runner source (setup installs it into /opt/arceus-runner)
  config/         example configuration
  observability/  Grafana, Prometheus, Loki and Alloy (setup installs it into /opt/arceus-observability)
  data/           your settings and the runner's state: config.yaml, secrets.env, runner.db, events.jsonl
  workspace/      clones of the project's repositories (the path is paths.workspace in config.yaml)
```

`setup/`, `runner/`, `config/` and `observability/` are a copy of this repository and are replaced as a whole on update. `data/` and the workspace are never touched by an update; `data/` is the only place you edit.

```bash
rsync -a --delete --exclude .venv --exclude __pycache__ --exclude setup.conf \
  runner setup config observability <user>@<vm>:arceus/
ssh <user>@<vm>
cd ~/arceus/setup
cp setup.conf.example setup.conf     # pinned versions and the Bulava commit; nothing else to fill in
sudo ./setup.sh --check              # only checks, changes nothing
sudo ./setup.sh                      # does whatever is not done yet
```

The script runs in phases: `packages legacy docker chrome agent_tools mcp_config dirs bulava bulava_install agent_reports agent_notes runner systemd observability`. Each phase checks the machine first, acts only if the check fails, and checks again afterwards. A second run therefore changes nothing, and `--check` after an interrupted run shows which phases are unfinished (`todo`). One phase can be run alone: `--phase docker`.

Setup never overwrites `data/config.yaml`, `secrets.env` or `bulava.env`: the examples are copied there once, only while the files do not exist. It never touches `runner.db` or the workspace either. If an older layout left `~/runner` and an empty `~/workspace`, the `legacy` phase moves them into `~/arceus` without overwriting anything.

## Where things are

| Path | Owner | What it is |
|---|---|---|
| `/opt/agent-tools` | root, read-only | `claude`, `codex` and `chrome-devtools-mcp` at pinned versions, linked from `/usr/local/bin` |
| `/opt/arceus-runner` | root, read-only | the runner's code and virtualenv |
| `/opt/bulava/engine` | root, read-only | the Bulava engine at `BULAVA_REF`: a full commit (the default), a tag or a branch the server follows (for example `factory`). A new commit is installed by the next `sudo ./setup.sh`, never while a task runs. The engine's `install.sh` runs as the user and writes to their `~/.claude` (worker hooks, status line) and `~/.local/bin` |
| `/var/lib/arceus/bulava.git` | root | a mirror of the Bulava repository that keeps every commit setup installed (`refs/arceus/<commit>`), so a pinned commit stays installable after Bulava rewrites its branches |
| `/opt/bulava-local/mcp.json` | root | the headless browser for Bulava (`SUPERVISOR_AUTOMATION_MCP_CONFIG`), without `npx` |
| `/opt/arceus-observability` | root | the Docker Compose stack for Grafana, Prometheus, Loki, Alloy and node_exporter |
| `/etc/systemd/system/bulava-engine.service` | root | the tmux server the workers run in (`tmux -L bulava`). Installed, not enabled |
| `/etc/systemd/system/runner.service` | root | the runner. `KillMode=process`: restarting it does not kill the engine's watchdogs. Installed, not enabled |
| `~/arceus/data/` | the user, 700 | `config.yaml`, `secrets.env` (600), `runner.db`, `events.jsonl`, `grafana-admin-password` |
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
nano ~/arceus/data/secrets.env                          # YOUTRACK_TOKEN and GITLAB_TOKEN of the bots
/opt/arceus-runner/venv/bin/arceus-runner check         # the config against the live tracker and code host
/opt/arceus-runner/venv/bin/arceus-runner doctor        # all ok: bulava.auth.*, bulava.usage.* with numbers
SUPERVISOR_PROFILE=server night-shift doctor --server --json --probe   # one real request to each model
```

`check` prints one line per mistake and names the field: a state that is not in the workflow, a tag that does not exist, a token that belongs to someone else, a repository that is not in the group. The runner does not start while any line is `FAIL`.

`config.yaml` names the issue tracker in `tracker:` and the code host in `code_host:`, each with a `type` (`youtrack`, `gitlab`). The keys at the top of each section mean the same for any provider; the rest belong to that type ([config.example.yaml](../config/config.example.yaml)). A `config.yaml` from before this layout, with `youtrack:` and `gitlab:` sections, still loads as the same settings, and `check` shows a `WARN` line for it until it is moved over.

`runner.db` belongs to the tracker and the code host it first ran with: its tasks, pending comments and MR ids only make sense there. If `config.yaml` later names another one, the runner refuses to start and says so. A new tracker or host gets its own `paths.db`. The same one under a new address (a moved YouTrack, a renamed GitLab host) is bound again with `arceus-runner rebind tracker` or `rebind code_host`, with the runner stopped.

More checks, each exits 0 only if everything it looks at is right:

```bash
python3 -I ~/arceus/setup/checks/browser-mcp.py                                      # Chrome really opens a page via the installed chrome-devtools-mcp
/opt/arceus-runner/venv/bin/python -I ~/arceus/setup/checks/youtrack-roundtrip.py OMD-3  # YouTrack: every write the runner makes, on a test ticket, then puts it back
/opt/arceus-runner/venv/bin/python -I ~/arceus/setup/checks/gitlab-protection.py     # GitLab: the bot is a Developer and cannot push, merge or force-push to the default branch, and may push and force-push to its task branches
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

Grafana, Prometheus and Loki listen on 127.0.0.1 only. Open them through an SSH tunnel:

```bash
ssh -L 3000:127.0.0.1:3000 <user>@<vm>        # then http://localhost:3000
```

Viewing needs no login. The admin password is in `~/arceus/data/grafana-admin-password`. There are three dashboards in the **Arceus** folder:

- **Arceus · now** (the home page): whether the agent is working, the queue, whether new work is taken, the issue tracker and the code host; the running task's current engine step and how long it has been in it, how long the agent has been quiet on its own turn, and when the runner last finished a pass; the tasks in progress, subscription usage of Claude and Codex; the task's progress step by step; every health check and the latest runner events.
- **Arceus · tasks**: tasks taken, MRs opened, finished without changes, failed; outcomes per day; why tasks failed; the timeline of every step for every ticket; Bulava's own task events.
- **Arceus · resources**: the VM at a glance (CPU, memory, disk space, how busy the disk is, time stalled on I/O, load per core, OOM kills, uptime) and a band showing when the agent was working; then CPU by kind of work, memory, pressure stall and load; disk throughput, operations, busy time, latency per operation, queue and blocked processes, space, growth, a 30-day forecast and inodes; network traffic, errors and drops; the cleanup: when the last full pass ran and how it ended, what it could not place, what it freed, and its log. Task starts and ends are marked on every graph.

The runner serves Prometheus metrics on `127.0.0.1:9464`; Alloy ships `~/arceus/data/events.jsonl` and Bulava's `~/.claude/supervisor/tasks/*/events.jsonl` to Loki. Bulava's step-by-step journal of a run (`run-events.jsonl` in the run's instance folder) is not shipped as it is: the runner reads it for the running task and writes each step into its own `events.jsonl` as `engine_step`, along with `agent_activity` (the agent's tool calls once a minute: tool names and the agent's one-line descriptions, never commands or output) and `agent_quiet`. The dashboards are generated by `observability/build_dashboards.py`; edit that file, not the JSON.

## Starting

Start with `mode: observe`. In this mode the runner only reads the queue and records in `events.jsonl` (and Grafana) what it would take:

```bash
sudo systemctl enable --now bulava-engine runner
```

When observing shows what you expect, set `mode: live` and run `sudo systemctl restart runner`. If the installed engine lacks a contract the runner needs, `live` does not start and lists what is missing (`arceus-runner probe-bulava`).

## Independent checks before Codex

After the build and tests pass and before Codex reviews, Bulava runs two Claude sessions side by side, a code review and a security review (engine/SERVER.md, "Independent checks before Codex"). They never see the ticket or the agent's conversation, only the diff of every repository with work in it, and they cannot change anything. Findings at `min_severity` or above go back to the agent, which fixes each one or says why not. Codex then decides with all the findings and the agent's answers in front of it. The checks only advise: an empty list is not a pass, and a reviewer who always finds something cannot keep the task going, because the number of returns per task is fixed. A red build skips them.

```yaml
bulava:
  claude_checks:
    enabled: true
    kinds: [code, security]
    min_severity: high     # critical | high | medium | low
    rounds: 1              # returns to the agent per task, 1–5; a follow-up does not reset it
    timeout: 15m           # per check, 1m–30m
    required: true         # a check that did not run stops the task for a person; false: Codex alone
```

A worker's review gate reads these from the environment of the engine's tmux server, which `bulava-engine.service` starts; whatever the runner passes on a dispatch does not reach it (the engine carries none of `SUPERVISOR_CLAUDE_CHECKS_*` on the worker's launch line, `tests/test_real_engine.py` pins that). So `setup.sh` writes them into that unit (`arceus-runner engine-env` prints them from `config.yaml`) and restarts `bulava-engine` when they changed and no task is running; with a task running it says so, and you restart it between tasks. `check` and `doctor` compare `config.yaml` with the running tmux server and show `FAIL` on `config.claude_checks` while they differ, so no new task starts on other terms than `config.yaml` says. They also show `FAIL` when the installed engine has no checks at all. The runner drops these names from `bulava.env`.

A `config.yaml` written from an earlier example has `required: false` in it, and that stays until you change it.

What to know:

- **Spent rounds stop the checks for the task.** Once the findings have gone back `rounds` times, the engine does not run the checks again for that task. Fixes after MR comments and red CI then go to Codex alone, and Codex is shown the last reading.
- **A check that did not run** (time, a Claude limit, no answer, the network, a write) is shown as `gate.check failed` in the engine steps (Grafana, `engine_step`). With `required: true` the task then fails before Codex, nothing is published, and the ticket says "a required pre-review check (code or security) did not run"; `/continue` runs it again. With `required: false` the work goes on to Codex alone, and only the engine steps show it.
- **Time and quota.** Two more Claude sessions per review on the same subscription, up to `timeout` each, inside the review gate's hour, together with the build and Codex. A diff over 400 000 characters is cut, and the checks read the rest from the files.
- **A start that waits for its session.** The engine rolls a start back when the new session's hooks do not confirm the run in time (`SUPERVISOR_HANDSHAKE_WAIT`, 5 s in the engine), and the ticket then fails. A cold Claude on a busy machine is slower than that, so the runner passes `bulava.start_handshake_wait` (30 s by default, 5 s–2 m).
- **The agent's model and depth.** Both checks run on `bulava.claude_model` and `bulava.claude_effort`, the ones the task started with; there is no setting of their own. With `auto` no `--model` is passed (the Claude CLI's default for the server's account) and the engine's `high` is the depth. A change in `config.yaml` reaches new tasks only.
- **Not the slash commands.** The checks run Bulava's own prompts, not `/code-review` and `/security-review`. The engine builds the diff itself, because the commands choose their own base and do not see the other repositories.

## How a task goes in `live`

1. Someone in `allowed_users` puts the `agent` tag on a ticket in the queued state. The tag's author is read from the ticket's history. A tag from anyone else gets one comment on the ticket and is not taken.
2. The runner takes one task at a time. The ticket's text, as it is at that moment, first goes through the security screen (see below); a held ticket is not claimed and its workspace is not touched. Tickets do not name a repository: the agent gets every repository of the code host's group (`code_host.group` on GitLab) except `exclude_repos`. The runner clones or fetches each into the workspace and runs `night-shift trust` and `mcp-decide skip` in it once. In every one of them it creates the task branch from the default branch and hands them all to the agent as writable (`SUPERVISOR_EXTRA_DIRS_FILE`); if any of them has uncommitted changes the task does not start. No repository is the main one: the session starts in the workspace folder itself. Bulava needs a git project to run in, so the runner keeps the workspace folder as a small repository of its own: a `.gitignore` that ignores every clone, and an `AGENTS.md` (read through `CLAUDE.md`) that tells the agent what the folder is and lists the repositories with their descriptions on the code host. It is committed on its local `main`, has no remote and is never published; whatever the agent commits there stays local, and the next task starts from the runner's notes again. Then the ticket gets In Progress, the bot as assignee and a comment, and `dispatch.sh` starts the work on branch `agent/<KEY>-<slug>`. Follow-ups get the same repositories.
3. The task is reconciled with `night-shift task show` every `poll_local`:
   - `waiting_review` with `review: passed`. MRs are opened only in repositories the agent changed (`reviewed_sha != base_sha`). Each repository in `receipt.repos` is checked first: branch, clean tree, `HEAD == reviewed_sha`, part of the code host's group, and `base_sha` on the repository's default branch (otherwise the commits below it were never reviewed). If any check fails, nothing is pushed. Otherwise `reviewed_sha:refs/heads/<branch>` is pushed without force, unless the code host already has exactly that commit, and the remote SHA is compared afterwards. If the branch there already holds commits on top of the reviewed one, the runner leaves it alone, opens no MR and says so on the ticket. Then the MR is created (or the existing one for that branch is reused), the ticket gets a comment with the links and moves to Code Review.
   - `needs_input` / `blocked`. The question and its options go to the ticket, mentioning the tag's author, with the `agent-needs-input` tag. The first comment after it from someone in `allowed_users` is sent to the agent with `worker-send --answers`. Without an answer there is a reminder after `needs_input_reminder` and the task fails after `needs_input_fail_after`.
   - `done`. The ticket gets a comment with the result; there is nothing to push.
   - `failed`, `debt`, a dirty tree, or an attempt with no verdict for longer than `attempt_timeout`. The ticket gets a comment with the reason and the `agent-failed` tag. Whatever the failed attempt left uncommitted is committed on the task branch as a `WIP [KEY]` commit, so nothing is lost and the next ticket starts on clean trees; it is reviewed with the rest before anything is published. While Bulava reads a subscription limit as used up, the attempt's clock stands still: a limit pause is not a hang.
   - A failed task is not a dead end. A comment from someone in `allowed_users` that starts with `/continue` (within `continue_window`, 14 days by default) resumes it: the request is journaled, then sent to Bulava as `worker-send --relation continue`, a new attempt of the same task with the same goal, branches and commits, plus the comment's text. It goes before new tickets, waits while Bulava is busy and survives a restart; it is sent once. The `agent-failed` tag comes off, the ticket goes back to In Progress, and the review and CI budgets start again, since a person asked. Other comments on a failed ticket wake nothing. If Bulava no longer has the task (its start was refused), `/continue` starts it again from the ticket on the same branches.
   - `running` with no live session (for example after a reboot). The work is continued with `worker-send --relation continue`.
4. **After the MR** the runner keeps watching every MR of the task on each poll of the code host:
   - review notes by people that mention the bot (`@` and `code_host.bot_user`), in every discussion, so a late reply in an old thread counts too. Anyone with access to the repository may ask the agent this way; what they write goes through the security screen like everything else. A note without the mention is people talking to each other and wakes nothing; the bot's own notes and the host's system notes never count;
   - the head pipeline: a failure of the commit the runner published is sent with the failed jobs and the end of their logs; a running, cancelled or older pipeline is not, and a red pipeline that turns green or is replaced before it is sent is dropped;
   - a conflict the code host has confirmed (on GitLab `detailed_merge_status: conflict`; `checking` does not count);
   - comments on the ticket by people in `allowed_users`, except a request for a code review (next section) and a security reviewer's `/security-allow` or `/security-reject`, which are the runner's.

   Each signal is stored once. When `review_debounce` has passed since the newest one, they go to the agent as one follow-up of the same Bulava task (`worker-send --relation continue --source mr|ci|youtrack`), the ticket goes back to In Progress, and Codex reviews the fix against the task's original goal. Each MR thread goes to the agent whole, earlier notes included, with the new notes marked, so a short "why?" keeps its context. The agent decides per thread whether it asks for a code change or only for an answer (a question about the implementation, or why one approach was chosen over another), and writes one answer per thread to `.arceus/replies/<ticket key>/<round>.json` in the workspace; for the reasons behind its choices it reads the notes it kept while working, `.arceus/notes/<ticket key>/decisions.md`. The runner then pushes the new reviewed commit to the same branch (fast-forward, never force), updates the MR description and posts the agent's answer in each thread: with the commit where the answer is a code change, without one where it only answers. Threads are left for people to resolve. If the round changed no code, the answers go to the threads and the task stays in Code Review; a thread the agent left unanswered gets its overall explanation. Before each follow-up every repository of the task is put back on the task branch and caught up with commits people pushed to the MR; if someone pushes while the agent is fixing, the agent is asked to merge their commits in on the next round. Fixes for open MRs are sent before any new ticket is taken.

   The task is **done** when all its MRs are merged; the ticket then moves to `tracker.states.merged` (`Testing` unless the board calls it otherwise; `null` leaves it in Code Review), unless a person already moved it out of Code Review. `check` warns when the board has no such state, and a merged task is finished all the same. An MR closed without merge stops the loop with a note on the ticket; a ticket moved past Code Review by hand stops it too (state `stopped`). `max_review_rounds` (rounds that ended in published code), `max_review_answers` (rounds answered without a code change, so questions do not use up the rounds for fixes) and `max_ci_fix_attempts` (CI repairs in a row, reset by a green pipeline) are kept across restarts; when one runs out the task fails with one comment saying why.
5. Every external action can be repeated safely. Comments go through a journal in SQLite and carry an `[arceus:…]` marker; dispatches and follow-ups have stable message ids; a push is compared with the remote first and an MR is looked up before it is created. After a crash the next tick finishes what is missing and duplicates nothing.

## Code review on request

Off by default (`review_agent.enabled: false`); with it off, `/review` is an ordinary comment and goes to the agent like any other. With it on, someone in `allowed_users` writes on a ticket in Code Review a comment that starts with `/review` (`review_agent.command`), or that mentions the tracker bot first and then says `review` (`@agent-runner review, mostly the migration`). Whatever follows is passed on as what to look at.

- **Who it is for.** The runner tells a request from everything else by those words alone, before any other handler reads the comment: it never goes to the agent that wrote the code, not as a review comment and not as the answer to its question. Any other comment, and a note on the MR that mentions the bot, still goes to the author. The command written on an MR starts nothing and is not sent to the author either: a review is asked for on the ticket, where the allowlist applies. Someone not on the list, a ticket outside Code Review, a review already under way, or `max_per_ticket` reached each get a one-line answer on the ticket and no review. Only reviews that were taken count towards `max_per_ticket`: refused requests do not use it up.
- **A task of its own.** The review is the Bulava task `<KEY>.review-<n>`, dispatched like a ticket, never a follow-up of the author's session: the author does not review itself. Its text is the ticket whole, description and every comment oldest first, nothing cut (it goes through the security screen first; a text longer than the screen reads is held for a person, never shortened), plus the open MRs. Each MR's repository is checked out at the commit the code host has, on a local branch `arceus-review/<KEY>-<n>` that is never pushed, and listed for the agent to read but not to write: it builds and runs the tests, it does not change the code. It asks nobody anything and writes its verdict to `.arceus/reviews/<KEY>/<id>.json`: per MR, whether it meets the ticket, the build and the tests as it saw them, blockers, suggestions (at most `max_suggestions`) and the changes a person should look at. A run that ends asking a question, hangs past `review_agent.timeout` or leaves no readable verdict ends as "could not be confirmed"; nothing of it reaches the ticket as a question.
- **The workspace.** From the switch to the end of the run the review holds the shared workspace like a task does: the author's follow-ups, a `/continue` and new tickets wait, and cleanup counts the time as the ticket's. Afterwards the runner stashes what the run left, checks every repository out where it was, deletes the review branch and frees the workspace. A crash at any point is finished from where it stopped.
- **What the runner approves.** The model advises; the runner decides. An MR is approved only when the reviewer found it ready (requirements met, build passed, tests passed or the repository has none, no blocker), the code host's newest CI run is for the reviewed commit and passed (`require_green_ci`; a run still going is waited for), the MR still points at that commit and the same target branch, and the ticket is still in Code Review with the description and comments the review read. Then the approver approves exactly that commit, and the MR gets a note: ready to merge, no further review needed. When a changed file matches `attention_paths` (by default the security boundary and secrets: auth, permissions, crypto, `.env`; ordinary test files and docs are left out of the default) or the reviewer named something a person has to decide, the MR is approved all the same, and the note says instead that a person has to look at those changes before merge, which files and why. The reviewer names only a decision it cannot settle itself: a change of substance to a security boundary, a CI check removed or weakened, widened privileges or exposure, a migration that drops, rewrites or narrows existing data or a choice about existing data, behaviour other systems rely on changed in a way the ticket did not ask for, a hard-to-reverse product decision the ticket left open. What the people who release it should know or do (an environment variable or proxy setting, a runtime or framework upgrade the ticket asked for, a new dependency, an additive schema change at start-up, a version out of upstream support) goes in the note under "For whoever deploys it" and leaves the MR approved without calling a person. Changes the ticket asked for that it verified and that are none of these (build and test tooling, lock files, styling, covered refactors) appear in neither list. Files under `review_hint_paths` (CI, Docker, Makefile, dependencies, settings, migrations, SQL) are pointed out to the reviewer to read with care and reach a person only through it. Otherwise the note says what has to change or why the review could not confirm it, and an approval the approver gave in an earlier review is taken back. The ticket gets one summary with a line per MR.
- **The approver.** The bot opens the MRs, and GitLab does not let an author approve its own by default. Approvals therefore come from a second GitLab account, `code_host.approver`, with its own token in `secrets.env` (`api` scope, at least Developer in the group's repositories). `check` verifies the token belongs to it. The same account signs the review's notes on the MR, so the review does not read as the author checking its own work; the bot posts a note only when the approver's token is refused. A note that account writes by hand and that mentions the bot still goes to the author like anyone's. Without an approver the review still runs, the bot posts its notes, and they say it could not approve. A refusal by GitLab (its role, the project's approval rules) is reported on the MR as it is; it is never shown as an approval.
- **The fix loop.** When the review says changes are needed, the runner sends what has to change (the blockers, a failed build or tests, an unmet requirement; not the suggestions) to the agent that wrote the code as a round of its own, and the MR note and the ticket say so ("Sent to the agent to fix, round 1 of 3"). The round counts against `limits.max_review_rounds` like any other and waits for the shared workspace. The agent fixes each finding or says why one is wrong. Once it has pushed new commits and the ticket is back in Code Review, the runner asks for the same review again on them, in the name of whoever asked first; nobody writes `/review`. This repeats until the review approves or `review_agent.fix_rounds` (default 3) is used up; then the note says the rounds are used up and people pass findings on the usual way, in a thread or on the ticket. A round in which the agent changes no code ends the loop too: its answer is for a person to read. `fix_rounds: 0` turns the loop off. Asking again for commits that already have a final review (approved, approved with changes for a person to look at, or changes) gets that review back; new commits, or a review that could not confirm, can be reviewed again.
- **Switched off** while a review runs: the run finishes, nothing is posted on the MRs and nothing is approved, and the ticket says so. A request recorded before is never handed to the author afterwards.

## Cleanup

Agents leave things behind: stopped containers and their test databases, dev servers whose session is gone, files in the repositories, screenshots. A dirty tree also stops the next ticket (`START_REFUSED`). The runner cleans up itself, by rules; nothing an agent or a model says decides what is removed.

**What goes.** Only what belongs to a task that is over: done, stopped, or failed longer ago than `continue_window` (a `/continue` still needs its branches, notes and containers before that). A task that waits for an answer, a review or a security reviewer keeps everything. Ownership comes from:

- the label `arceus.task=<KEY>` that the agent puts on containers, volumes and networks it makes outside a compose file (`config/agent-notes.md`);
- the place: `.arceus/media/<KEY>` (after `keep_media`), `.arceus/notes/<KEY>` (after `keep_notes`), the task's branch;
- the time: one task at a time holds the workspace and the runner records when (`workspace_holds` in SQLite), so a stopped container that mounts the workspace, a `docker compose run` container of a workspace project, or a dev server running in the workspace, made while task X held it is X's. A compose service's container is never removed by time alone: a dev stack the agent recreated looks exactly the same.

What that means per kind:

| Kind | Removed | Never |
|---|---|---|
| containers | stopped ones of a finished task (`docker rm -v`: anonymous volumes with them, unless another container uses one, it is labelled for a task that is not over, or it cannot be read: then plain `docker rm`, the volume stays), after `stopped_after`; running ones and compose services only with the task's label | foreign projects, `arceus-observability`, `protect`, `arceus.keep=true`, a running container or a compose service tied to a task by time only (it may be the dev stack the agent recreated; a service keeps its name, so a test stack left like this is one set per project and is replaced when it next starts), a container holding a protected volume (`rm -v` would take it) |
| volumes | with a finished task's label, when no container uses them | named volumes without a label: dependency caches and the dev stack's data |
| networks | empty ones (the containers this pass removes do not count) labelled for a finished task, of a compose project whose containers were all labelled for it, or of a workspace compose project made while it held the workspace; the projects are remembered after their containers are gone | networks made by hand (compose `external:` ones); empty ones nobody can be tied to (reported) |
| images | built with a finished task's label (`docker build --label arceus.task=<KEY>`), when no container uses them | everything else: untagged images are only counted in the report (`docker image prune` by hand), other projects' images are never looked at |
| repositories | uncommitted files on a finished task's branch go to `git stash` (`arceus-cleanup …` message); a finished task's local branch once the code host has its commits, or it is exactly what was published to a merged MR | ignored files (`node_modules`, builds); anything on a branch that is no finished task's |
| processes | dev servers started in the workspace while a finished task held it, with no `tmux`, `claude` or `codex` above them (SIGTERM, then SIGKILL, by pid handle) | anything below a live session |

Everything else is **reported** and never removed, so a person can clean it by hand (for now, once a week): containers, volumes nobody labelled and no container uses (a dependency cache or a task's test data), empty networks, images no container uses (with their size, the biggest first), untagged images and unused anonymous volumes (counts), the build cache's size, and files or folders in the workspace beside the repositories (`artifacts/`). Each line has a size where Docker gives one (`docker system df -v`); images share layers, so the last image line says what removing all of them frees, as Docker counts it. A name that spells a task (`edcmp7173-mysql`) gets that task and its state as a hint: a name proves nothing, so nothing is removed for it. Things of a task that still works or waits are not listed: they are not garbage yet. `cleanup_finished` counts it, Grafana counts it, `arceus-runner cleanup --last` shows all of it (a pass's summary keeps the first 200 lines and the totals; the full list is in `cleanup_items`). With `llm: advise` Claude (no tools, as the security screen) adds a one-line opinion to each reported item for the person reading it.

**When.** Inside the runner's tick, so no task, follow-up or `/continue` switches the workspace meanwhile:

- the workspace has been free with no work waiting for `idle_after`, and the last full pass is `min_interval` old;
- `force_every` (24 h) without a pass: a full pass in the first free moment, before the next ticket. While a task works all day, an *independent* pass instead: only labelled non-compose containers, labelled volumes, networks and images, and `.arceus` folders of finished tasks; the repositories, compose containers, images and processes wait for a free workspace, and the report says so;
- low disk (the `disk_free` check warns or fails): a pass every `retry_after`, whether or not new work is allowed;
- a full pass also waits while Bulava still has a live session for a task the runner sees as waiting, or does not answer.

A pass has `max_duration` (10 min): every docker and git command gets only what is left of it, a command cut at the deadline leaves the rest `skipped`, and the pass ends `partial`. A pass with errors is `partial`: it does not count, and it is tried again after `retry_after`, doubling up to `force_every`. A pass cut short by a crash is marked `interrupted` at the next start; every removal is checked again right before it is made (still there, still stopped, not protected since, a volume still the same one by its creation time and labels and mounted by no container, same branch head, the folder not moved behind a link, nothing mounted on or inside it). A folder is opened, compared with the one checked and emptied through that handle, never by a path that could be swapped meanwhile; one cut at the deadline stays under its own name. So the next pass just goes on.

**Starting it.** `cleanup.mode: report` first. It logs every pass and what it would remove, and changes nothing (`mode: observe` is always report, and `cleanup --act` refuses to run in it). Read a few reports:

```bash
arceus-runner cleanup            # what a full pass would do now, and why; changes nothing
arceus-runner cleanup --last     # the runner's last pass
```

Add the dev stack's compose project to `cleanup.protect.compose_projects` if it lives in the workspace, then set `mode: act` and restart the runner. `arceus-runner cleanup --act` makes a full pass by hand while the runner is stopped. Test data in the dev stack's own database is not touched: nothing tells which rows a task wrote.

`ARCEUS_REAL_DOCKER=1 pytest tests/test_cleanup.py` checks the rules against the VM's Docker with objects of its own; it prunes no images and touches nothing else.

## Who opens and describes the MR

The runner. Bulava refuses `git push` inside a run, so the agent only commits; once Codex has passed the work the runner pushes exactly the reviewed commit and creates the MR through the code host's API with the bot's token. The MR says:

- the ticket, with a link;
- **What was done**: the agent's own summary of the attempt (the `summary` it reports to Bulava);
- **Commits**: the task branch's commits since its base;
- **Checks**: which commit passed review (no model or tool names, no round counters: those stay in the metrics);
- the other MRs of the same task, when it changed more than one repository.

After every review round the description is rewritten to match the branch.

## What the agent is told by us

Bulava writes the agent's rules. Two things are added from this side, both by setup and both only for the run user:

- **Notes** (`config/agent-notes.md`, phase `agent_notes`): a block between `arceus:agent-notes` markers in `~/.claude/CLAUDE.md`, which Claude Code reads in every session, the engine's workers included. The phase creates the file when it is missing, and `--check` reports `todo` when the file or the block is missing or out of date. English for commits, the run summary and anything posted to the MRs or the ticket; dev servers started in the background and stopped with a pattern that cannot match the agent's own shell (`pkill -f "[m]anage.py runserver"`); every changed repository gets a `make verify` (or `verify.sh`) that runs its CI checks in Docker, registered with `add-check` by the repository's absolute path, so Bulava's verifier never falls back to a bare-shell `make test`; for visible changes, screenshots and recordings in the workspace's `.arceus/media/<ticket key>/` with a `media.json`, which the runner uploads to the MR description and attaches to its comment on the ticket when it publishes; the reasons behind its choices kept in `.arceus/notes/<ticket key>/decisions.md`, for reviewers' questions on the MR; the `arceus.task=<ticket key>` label on containers, volumes and networks it makes outside a compose file, and dev servers stopped before it finishes, so cleanup can tell what is its; screenshots under the engine's reports folder may be opened; files in the workspace stay world-readable for the containers. The rest of that file is left alone.
- **Reading screenshots** (phase `agent_reports`): `Read(//<home>/.claude/supervisor/reports/**)` in `~/.claude/settings.json`. The engine's tools save screenshots there but do not open the folder to the agent, so on a server the read needed an approval nobody could give. Read only.

The runner itself runs with umask 077 (its tokens and database), while git and the engine commands it starts use 022, so clones are 644/755 like any developer's and the dev stack's containers can read them. On every task start the runner also restores the modes git records for tracked files.

## Git for the agent

The runner writes the code host bot's credentials (`CodeHost.git_auth`) to `~/.git-credentials` for that host only and enables git's credential store for that host only. Plain `git fetch`, `pull` and `push` therefore work for the runner and for the agent alike, and the token is never in a URL, on a command line or in a repository's `.git/config`.

Pushing from inside a run is decided by the engine: Bulava's server profile refuses `git push` today. Allowing the agent to push its task branch is requested from Bulava. Either way the runner checks after the review that the code host holds exactly the reviewed commit, pushes it itself if it is not there, and opens the MR.

## Security screen

Anything people or CI wrote is read by a classifier before the agent gets it: the ticket when the task starts, an answer to the agent's question, the text of a `/continue`, and every review round (MR threads, CI logs, ticket comments). The runner's own messages (a resume after a reboot, the nudge after a stall, a bare `/continue`) are not screened. `Engine.dispatch` and `Engine.send` refuse a text that has no clearance, so a new path to the agent cannot skip the screen by accident.

- **The classifier** is `claude -p` with no tools, no MCP servers, no settings and no session, in an empty temporary folder, with the policy as its system prompt and the message as a JSON field. It answers `allow`, `review` or `block` with categories and a short reason, checked against a strict schema. It uses the same Claude sign-in as the agent (`bulava.env`), so no new provider sees the client's text. Codex is not used: its sandbox still lets it read files, and a message could ask it to read the server's secrets into its answer. The policy and the model are set in `screening.py` and `security.model`.
- **What it holds:** sending code, data or credentials outside the project; collecting everything the agent can reach; personal data of employees or users; reading or exposing secrets; irreversible damage; weakening the agent's or the server's controls; backdoors; claimed authority ("the CTO approved"); instructions aimed at the agent instead of the task, hidden or encoded ones included. Ordinary work and security work pass: fixing a vulnerability, a test with an attack string in it, a CI failure. Hidden Unicode (tag characters, bidi controls) is held whatever the model says, and so is a text longer than 100,000 characters.
- **Only `allow` lets a text through.** `review` and `block` move the task to `security_hold`, with one comment on the ticket that mentions `security.reviewers`, says what was found (never the text itself) and gives a code. A reviewer answers `/security-allow <code>` to send exactly that text, or `/security-reject <code>` to drop it and fail the task. A reviewer whose words are in the text cannot decide on it (for a ticket: the tag's author, the reporter and whoever edited it last). An approval is for that task's hold only. A text that changes after the hold is screened again, and a rejected text is refused from then on, in any task, without a new hold. `/continue` does not lift a hold. A held task keeps nothing else waiting: new tickets and other tasks go on, and a released ticket starts once the workspace is free.
- **When the classifier does not answer** (a limit, a sign-in, a timeout), nothing is sent: the item is tried again on the next tick, `screening_unavailable` is logged and the `security.screening` health check fails, which stops new tasks. A text it fails on three times goes to a reviewer. A refusal to classify counts as `review`.
- **Logs:** `events.jsonl` gets the verdict, its categories, the model and a 12-character code of the text (`screened`, `security_hold`, `security_released`, `security_rejected`), never the text or the classifier's reason. The database keeps the verdicts and holds, not the texts.
- **Checking the policy on the real model:** `ARCEUS_SCREEN_LIVE=1 pytest tests/test_screen_live.py` (one Claude call per case, Russian and English attacks and ordinary tasks; `ARCEUS_SCREEN_MODEL` picks the model). Run it after changing the policy or `security.model`.

The screen is one layer, and it lowers the risk without removing it: a classifier can be talked past, and instructions can also reach the agent later, from files in a repository, a web page or a tool's output. What the agent can actually read and reach on the VM is what protects the data then; see the next section.

## Trust boundary

Claude's workers run as the same user as the runner, and `ai` has passwordless sudo and the `docker` group. So the agent can technically read the runner's tokens and do anything on the VM. Bulava's server-profile denies (reading `~/arceus/data/*`, `~/.ssh`, `~/.git-credentials`, the Claude and Codex credentials; `glab`, `gh`, `git push`) and the root-owned `/opt/*` protect against accidents, not against intent. The isolation boundary is the VM itself. That is why the bots' tokens have minimal rights, the default branch must be protected in GitLab (check it with `gitlab-protection.py`), and outbound traffic is to be put on an allowlist.

## Not included yet

- **An outbound nftables allowlist.** It will be a phase of its own, with a timed rollback so SSH is never lost.
- **Alerts** (Slack or another channel; none is configured yet) and the metrics that need MR history (merged without rework, first CI green, reverts).
