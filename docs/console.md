# The web console

One address with a sign-in for everything people do with the factory, instead of SSH tunnels. It runs on the VM
in Docker beside the observability stack and is set up by `setup.sh` (the `console` phase).

## How it is put together

```
browser ──https──▶ load balancer (ends TLS) ──http──▶ nginx :console.port ──▶ console 127.0.0.1:8180
                                                          │
                                                          └─ /grafana/ ─▶ Grafana 127.0.0.1:3000, once the console says who asks
console ──reads/writes──▶ ~/arceus/data: runner.db, config.yaml, console/console.db
console ──reads──▶ the runner's metrics (127.0.0.1:9464)
```

- **The console** (`runner/src/arceus_runner/console`) is part of the runner's package and is built into an image
  of its own (`console/Dockerfile`) from the same source, on the same Ubuntu as the VM, so it opens `runner.db`
  with the same SQLite as the runner. It runs as the run user, with a read-only root, no capabilities and
  `no-new-privileges`; it has no sudo, no Docker socket and no D-Bus.
- **What it sees.** The data folder at its own path, because SQLite keeps `runner.db`'s journal beside it. Every
  file or folder there that holds a credential is replaced by an empty one: `secrets.env`, `bulava.env`, any
  `*.env` or `secrets*`, `grafana-admin-password`, keys, `google/`. Setup lists them from the folder each time it
  runs; a new secret file put there later is hidden from the next `setup.sh` on, and `setup.sh --check` shows the
  `console` phase as `todo` until then. Report folders that `config.yaml` puts outside the data folder are mounted
  read-only, never the run user's home, a folder above it, its hidden folders or the machine's own folders
  (`/etc`, `/var`, `/usr`, `/opt/arceus-*` and the like); setup says so and leaves such a folder out.
- **What the container protects, and what it does not.** On the host the console would run as the run user,
  who has passwordless sudo. In the container a bug that reads files (a path the code did not mean to open) finds
  no sudo and no tokens: `secrets.env` and the rest are empty there, the home's folders are not mounted at all.
  What it does not stop is someone who runs code in the console's process. The console may write `config.yaml`
  and `runner.db`, and both steer the runner: `health.devstack_check` is a shell command the runner runs,
  `tracker.url`/`code_host.url` decide where its tokens go, and a run's brief in `runner.db` reaches the agent,
  which has sudo. Treat the console's process, and an **admin** account (which may change those settings), as
  control of the VM; give the admin role accordingly, and keep its passwords long.
- **The security boundary is still the VM.** The agents run as the same user with sudo (`docs/security.md`): they
  can read `console.db` and change anything the console can. The console keeps strangers out; it does not keep
  the agents in. People with the QA or architect role write instructions the agent follows (the test and review
  instructions); the architecture reviews' decision notes are screened, those files are not.

## Its data

The console works with two SQLite databases in the data folder, both opened directly (no API in between, no
copy):

- **`console/console.db`, its own.** People and roles, sessions, invitations and reset links, failed sign-ins,
  the journal of what people did, and every version of `config.yaml` and of the instruction files saved from the
  console. Only the console writes it, and it migrates it itself.
- **`runner.db`, the runner's, shared.** The pages read the runner's state from it (tasks, runs, findings, the
  health it recorded, the config it started with) with read-only connections. The console also writes into it,
  and only what a person decided: a test run or an architecture review queued or asked to stop, a decision on a
  finding. The host's executor, as the run user, sets and lifts the drain before an engine restart. The console
  never migrates `runner.db`: it writes only when its schema is the one this code knows (the runner and the
  console come from the same setup), and otherwise says the two are of different versions.

`config.yaml` and the instruction files stay files; the console edits them in place. Operation requests and their
output are files too (`console/ops`), with the host's own ledger in `/var/lib/arceus/ops`.

**Backups.** A backup of `runner.db` alone keeps the work, not the people: keep `console/console.db` as well.
Both run in WAL mode, so copy them with SQLite's own backup while they are in use
(`sqlite3 runner.db ".backup /backup/runner.db"`), not with `cp` of the file alone.

## Turning it on

In `config.yaml`:

```yaml
console:
  enabled: true
  public_url: https://console.example.com   # the address people open
  port: 80                                  # what nginx listens on; the load balancer forwards public_url here
  # listen: 172.18.0.1                      # the address it listens on; unset: every address of the machine
  tls: false                                # true: the load balancer talks HTTPS to nginx (see below)
  trusted_proxies: [10.0.0.0/8]             # who may say who the visitor is (X-Forwarded-For)
```

Some load balancers talk HTTPS to their backends even on port 80. A Kubernetes ingress with
`backend-protocol: HTTPS` does: nginx then sees a TLS handshake where it expects HTTP, answers 400, and the
visitor gets a 502. `tls: true` makes nginx speak TLS on `port` with a certificate setup makes for it once
(`/opt/arceus-console/tls`, ten years). Such load balancers do not check the backend's certificate. The other way
out is to have the load balancer talk plain HTTP to the backend.

nginx runs in the host's network and listens on `port` on every address of the machine. That suits a load
balancer elsewhere, with the machine's firewall closed to everyone else. When the reverse proxy runs on the
same machine in Docker (Traefik, Caddy) and nothing filters the machine's ports, set `listen` to the gateway of
the proxy's Docker network (`docker network inspect <net> -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}'`): the
proxy reaches nginx there, and the port is not open on the public address. The proxy is then in
`trusted_proxies` (its network, for example `172.18.0.0/16`).

Then `sudo ./setup.sh` (or `--phase console`). The first admin comes from the server:

```bash
/opt/arceus-runner/venv/bin/arceus-runner console invite --admin you@example.com
```

It prints a one-time link (48 hours). Open it, choose a name and a password, and sign in.

To check an installation from outside and over SSH (HTTPS, the cookie, closed inner ports, the services, the
console reading the VM's own database, the operations it ran, nothing failed since a moment, an owner's
invitation; exits 0 only if all of it holds):

```bash
bash setup/checks/console-live.sh https://console.example.com user@vm -p 22 --owner you@example.com --since 2026-11-08T23:20
```

While the console is on, the runner no longer serves the old report pages on `qa.port`, `architect.port` and
`secops.port`: the Slack notice of a finished test run, architecture review or triage run links the report's page in
the console instead (`public_url` + `/qa/<n>/`, `/architecture/<n>/` or `/secops/<n>/`), and so do `arceus-runner
qa`, `architect` and `secops`. Signed out, the link opens the sign-in and then that report.

## Home

What the factory does now, in one line at the top: working on a ticket (and which), running a platform test or
an architecture review, idle, waiting on people, held by a failing health check, observing, or that the runner
does not answer. Under it: whether the runner answers and when it last finished a pass, the queue, the tickets
waiting on people, those in code review, what runs now, the latest test run and architecture review, the health
checks that fail with their reason, and Claude's and Codex's subscription usage. A ticket waiting on a question
shows the question's first line. Every ticket's key, in any of these lists, opens its page under **Tasks**, with a
separate link to the ticket itself; every run opens its page under **Test runs**, **Architecture** or **Advisory
triage**. The part refreshes itself every 15 seconds.

It comes from `runner.db` (read-only) and the runner's metrics. The runner writes its health checks with their
detail into `runner.db` (`runner_state`) whenever one changes, so a failure that began a week ago still shows
why. Whatever cannot be read is said as such, never shown as fine: no database yet, metrics off or not
answering, a runner that answers but has not finished its first pass ("starting": health and usage unknown), a
pass that ended more than 15 minutes ago ("stuck"), a check without a recorded reason, a `config.yaml` that does
not load.

## Tasks

**Tasks** lists the tickets the runner has queued and every task it has taken, newest change first: development,
research, and people's merge requests it was asked to review. A ticket opens at `/tasks/<KEY>/`, queued ones
included (before the runner has a record of the task), and shows:

- **Why it is where it is**, as one of a fixed set of reasons, each in its own words: queued for the next pass, for
  the shared workspace (and what has it), held by a failing health check, by an engine restart asked from the
  console or by mode `observe`, behind work the runner starts first (review comments for an open merge request, a
  `/continue`, a code review or a run waiting); the same for a code review or a run that waits to start; the agent
  working, the gate, the verifier or Codex checking, the engine pausing a step,
  a subscription window used up; opening merge requests; waiting for an answer or a decision on the ticket; held by
  the security screen; waiting for people to review and merge, review comments on their way to the agent, a code
  review queued or running; failed (with the runner's reason, and until when a `/continue` on the ticket resumes it),
  stopped, done. Only a wait for an answer asks for a comment on the ticket. A used-up subscription window is said
  to be the whole factory's, never this task's spend, and usage that is high but not used up does not make a task
  wait. Only a development attempt is said to stop its clock while a window is used up; a code review or a run is
  said to wait for one only when the engine's step waits, and its time limit keeps running.
- **The agent's question**, whole, with its options, beside a link to the ticket where it is answered. The runner
  keeps the question as the ticket got it; a question kept before it did so (its first 1500 characters) says the
  ticket has the rest. Only the question the task waits on now is shown, never an earlier one's words.
- **Now:** the state and when the record last changed; for a task the engine is running, its step and since when,
  when the agent last wrote anything, how long it has been quiet (only while the step is the agent's own; while the
  gate, the verifier or Codex has the turn, or the engine waits, it says so), and when the runner last read the
  engine's files; and whether the runner answers.
- **Limits:** the time left to answer before `needs_input_fail_after`, counted from when the question was posted
  (unknown when that time was not recorded), and for development the review rounds that changed code, those
  answered without a change and the CI repairs in a row, against `max_review_rounds`, `max_review_answers` and
  `max_ci_fix_attempts`. The figures are config.yaml's now; the page says when the file has changed since the runner
  started, since it counts with the version it started with.
- **Merge requests:** one per repository, each with the commit Codex passed, and the branch. A research ticket says
  nothing is published.
- **Code reviews asked for** on the ticket, newest first, each under its own engine task (`<KEY>.review-<n>`) with its
  state, outcome and, while it runs, its own live figures.
- **The security screen's hold:** what kind of text was held, its code, the categories found and when, with the
  commands a reviewer answers on the ticket. Never the text, the screen's reason, or people's comments waiting for the
  agent.

Live figures come from the runner's metrics and are as old as its last pass. When the runner does not answer, has not
finished its first pass, or its last pass ended more than 15 minutes ago, the page says the figures are unknown or
out of date. When the runner has not read the task's engine files for three progress polls (`poll_progress`, at least
five minutes), or not at all yet, it says that too. A task that is over is never marked out of date: its record is
final. Figures are matched to the task by its engine task id, so another task's or a code review's are never shown
as this one's.

An open page follows the task every 15 seconds without a reload: each part (the reason, the question, the security
hold, the live figures and limits, the merge requests, the code reviews) is put in again only when it changed. A
question still waiting therefore stays exactly as it is while it is read; an answered one goes, the next one comes,
and merge requests and reviews appear as the runner records them. A task that is over is still followed, since a
`/continue` on the ticket can take it up again. It asks the way a run's page does: one request at a time, a request
that hangs given up after one round (so an older answer never lands after a newer one), at once when a hidden tab is
shown again, and with the same notice when the console has not answered for two rounds. Nothing on the page acts: answers, `/continue` and security decisions stay on the
ticket, and reloading the page repeats nothing. An unknown or malformed key is not found.

## Test runs

**Test runs** starts with the test scenarios ([qa.md](qa.md)): each with its name, its schedule in words ("Weekdays
at 07:00", "By hand only") and when it is next due, in `schedule.timezone`, and its run if one is queued or under way.
People with the QA role and admins make a scenario with **New scenario**: a name, the days and the time it runs by
itself, and its instructions, all saved together. Days and a time are the schedule; leave both empty for a scenario
run only by hand. A line under them says which it will be as they are picked, and **Clear schedule** empties both. The page of a
scenario changes its name and schedule and deletes it; a save over a scenario someone changed since the page was opened
is refused, with what is saved now said and the person's own changes still in the form. A deleted scenario leaves the
list; its past runs keep its name. **Run now** queues a run of that scenario, refused while one of the same scenario is
queued or under way; runs of different scenarios wait behind each other. At `qa.max_scenarios` the page says so instead
of offering a new one. What a scenario runs and when is read by the runner on its next pass, without a restart.

Below the scenarios, the list of every run of the platform test, newest first, queued and running ones included, each
with its scenario. Each opens at
`/qa/<n>/`: before its report, as where the run is (queued, and for what; under way, with its step, the agent's
activity and the same freshness marks as a task's page; the stop it was asked for), refreshing itself until the report
is written; a finished run whose report is missing says so instead of showing a result. Under way, the page also shows
how long the run has been going and how long it waited in the queue, the time its agent stops testing to write the
report and its time limit, each with what is left; a run still going past the limit is stopped on the runner's first
pass after it. These are the times the run was given
when it was prepared, the same ones its brief names and the runner stops it by. The run's page shows no estimate of
when it ends: the limit is not an end time, and putting the workspace back and writing the report come after it. What
the run is doing is said only from the runner's state and whose turn the engine reports (the agent's, the checks', or
waiting for the engine), never from the plan or the clock. Beside
the times is the agent's test plan as the runner last counted it: checks done out of all of them (a check that found a
problem is done), each result's count, the checks not done yet, whether the plan grew since it was first counted, and
when it was counted. The runner reads `plan.md` in the workspace on each pass and keeps only the counts with the run,
because the console cannot see the workspace and the plan goes with it. A plan not written yet, not in its form, or
unreadable is said so, never shown as numbers. The stop button refreshes with the page and goes once the run is over
or a stop was asked. When the console has not answered for two rounds, whether its answers fail or never come, a notice says since when
the page has not updated; a request that hangs is given up after one round, so they never pile up. A tab shown again
after being hidden asks at once, and the notice, if the console still does not answer, names the time of the last
answer it really gave. A finished one opens as its
report: the verdict, what was checked, each problem with its steps, its screenshots and videos
at the step they show, expected against actual and the suspected cause with links into the code. The list stays
when `qa.enabled` is false; it says that none start. Only the report's own files are served: a run's media by its
exact name, never the copy of the instructions a run keeps (it holds the test accounts), `result.json` or any other
path.

People with the QA role and admins start a run (the run records their email as who asked) and stop one under way.
They also edit each scenario's **instructions** (`/qa/scenarios/<n>/instructions/`; the old `/qa/instructions/` opens
the first scenario's), which nobody else can open, since the file holds the test accounts. Each scenario's file has
a history of its own. The editor saves atomically and keeps the file's mode. A save over a
version that changed meanwhile (another person, an edit over SSH) is refused with the difference shown. Every
saved version is kept with who saved it and can be opened again in the editor. The page also says what the agent
would get: an instructions file that is empty without its comments, or too large, would stop a run at once.

## Architecture

**Architecture** lists the reviews and a count of open, decided and fixed findings; **All findings** shows every
finding kept. A review queued or under way opens at `/architecture/<n>/` as where it is, as a test run does. A
finished review opens as its report, built from `runner.db` on every visit. Each finding shows Codex's
second check apart from the person's decision.

People with the architect role and admins decide: **Accept the risk** or **False positive**, each with a reason,
or **Take the decision back**. Everyone else sees the decisions without the buttons, and a direct POST from them
is refused. The finding shows who decided, when and why, and **Decisions on it** lists every decision before.
The form carries the version of the finding the page showed. If someone else decided in the meantime, the second
decision is refused and the page says what stands now. The next review gets the reason only after the security
screen cleared it, quoted as data ([architect.md](architect.md)). Architects also start and stop a review and edit
the review instructions, the same way as test runs.

## Advisory triage

**Advisory triage** lists every triage run ([secops.md](secops.md)), newest first: a finished one with its result in
its report's words and the agent's summary, one under way with its step (in `publishing` it is sending its verdicts to
the register) and a stop if one was asked, when it started, and the schedule or who asked. A run queued or under way
opens at `/secops/<n>/` as where it is, as a test run does; a finished one opens as its report, rendered from the
run's `result.json` as the runner's own page is: each advisory with its verdicts by repository, their evidence and
links into the code at the commit read, and whether each went to the register. The list says when `secops.enabled` is
false or the runner's mode starts nothing.

It is for reading only. A run is started with `arceus-runner secops run` or by `secops.schedule` and stopped with
`secops stop`, on the server, and the team's notes on its stack are edited there too.

## Settings

**Settings** is `config.yaml` as a form, a page per section (tracker, code host, schedule, models, test runs and
so on). Every setting of the runner's schema is there with its current value: what the file says, as written, or
the default when the file does not say it. A setting the form cannot hold as a field (a map such as
`review_agent.repos` or `slack.people`, or an optional block such as `architect.schedule`) is edited as YAML in place.
The whole file can also be edited as YAML.

Admins change it in two steps. **Check the change** shows the exact difference in `config.yaml` and what it needs
to take effect: a runner restart, or for `console`, `paths`, `metrics`, the engine's checks and a reports or
instructions folder moved outside the data folder (the console's container has to have it mounted), `setup.sh`
and then a restart. The result is checked by the runner's own code (`parse_config`): a value it would refuse shows its
error and cannot be saved. **Save** writes only the lines of the settings that changed: it finds each one's lines in
the file and replaces those alone, so comments, order, quoting, the way lists are indented and every other line
stay byte for byte; a setting the file did not have is added as its own line under its section. A text value YAML would read as something else (`off`, `09:00`) is written quoted. The
write is atomic and keeps the file's mode. If `config.yaml` changed since the page was opened (another admin, an
edit over SSH), nothing is written: the page shows what changed on the server, then the person's own changes put
on top of the file as it is now, to save or drop. Two people saving from the same page at the same moment get the
same treatment: the check and the write hold one lock (beside `console.db`), so one save goes through and the other is
shown the difference. Saving restarts nothing.

**History** lists every version saved from the console, with who saved it and why (the version is recorded before the
file is replaced and removed again only if the file was not replaced, so the text a save replaced is always there to
bring back; a failure after the replacement, such as syncing the folder, counts as a save and is noted in the journal), and shows which one is in the
file now and which one the runner runs with. Any version, or what was there before it, opens in the YAML editor
and goes back through the same check. The runner records which `config.yaml` it started with (`runner_state` in
`runner.db`), so Settings says when the file has changed since the runner started and needs a restart to apply.

Operators see all of it read-only. Nobody else opens it. Secrets are not in `config.yaml` (only the names of
the variables in `secrets.env`), and the console never reads `secrets.env`.

## Assistant

**Settings → Assistant** is the project's assistant ([assistant.md](assistant.md)) on one page. Operators see it;
admins change it.

- **How it is now:** its service (`arceus-assistant@<run user>`, as the host's executor last saw it), Slack (the
  workspace and the bot as Slack's `auth.test` named them, or why it is not connected), who pays for its Claude and
  how its last reply went, the web chat. The assistant writes that itself into `assistant-status.json` beside
  config.yaml, without a secret in it, every 30 seconds, with the Slack connection checked anew (a drop shows as «Connection lost» until Slack's client is back). A status no longer kept, or a service the host's executor has not confirmed lately, is said as unknown now, with what was last seen and when; so is who pays: the page names what the running process uses, and a saved change of it as waiting for the restart.
- **Saved is not applied:** the page compares the config.yaml the assistant started with and the file now, and offers
  **Restart to apply** (operators and admins): the host's `restart-assistant` operation, which waits for the assistant
  to write its status again before it counts as done.
- **Who talks to her, and who pays:** the web chat's people, the Slack audience, who may have tickets made, whom she
  calls; and what her replies run on (the factory's Claude or Codex subscription, or an Anthropic or OpenAI
  API key), and whose key it is.
- **Keys, write-only:** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `YOUTRACK_TOKEN`. An admin pastes a
  key and saves it, replaces it or removes it; the page shows only whether each is set, since when and by whom. The
  console never reads `assistant.env` (its container sees it empty). It writes the key into a request readable by the
  run user alone, and only when the host's executor is answering; the executor takes it at once (not as a unit of its
  own), checks it again, writes it into the data folder's own `assistant.env` (600, never where config.yaml points
  elsewhere) and removes the request. The ledger, the console's copy of it, the journal and the log name the key and
  who set it, never the value. A Slack token restarts the assistant so it reconnects; the Anthropic and YouTrack keys
  apply to her next reply.
- **Its settings** are the `assistant:` section, edited through the same Check → Save and History as every section.
  What would run code or read files into what she says (`claude_bin`, `ticket_engine`, `home`, `secrets`,
  `persona_file`, `knowledge_file`, `port`) is shown and set on the server only; `enabled`, `web` and `port` are
  applied by setup.
- **The web chat is on only where nginx routes it.** setup writes `/assistant/` into the console's nginx from
  `assistant.web`; a restart does not. The host's executor reads that nginx.conf and says whether it routes
  `/assistant/` and to which port. The tile says **On** only when she serves the chat and nginx routes to her; with
  the chat off she answers every web chat path with 404 (`assistant.web: false`), whatever nginx still routes. When
  nginx does not route as the saved settings say, the page asks for **Run setup**, not for a restart.

## Operations

**Operations** restarts the runner and the engine and runs setup. The console has no root on the machine, so it
does not do these itself. It writes a request into `~/arceus/data/console/ops/requests`, and the host does the
rest (`runner/src/arceus_runner/console/hostops.py`):

- `arceus-ops.path` wakes `arceus-ops.service` (root) for each request. The service reads the request without
  following links, checks it against its fixed list (restart the runner, restart the engine when idle, the setup
  check, setup), records it in its own ledger (`/var/lib/arceus/ops`, root's) and starts it as a transient unit
  `arceus-op-<id>`. The unit outlives the console, so setup can rebuild the console's container while it runs.
- One operation at a time. A request while one is under way is recorded as refused, with the reason.
- `arceus-ops-tick.timer`, every 20 seconds from boot: an operation whose unit is gone without a result (a reboot,
  a killed process) is marked **interrupted**. It never becomes a success and is never run again. The timer also
  writes the services' state for the page. When nothing has written it for 90 seconds, the page says the
  executor is not answering and starts nothing.
- Root writes the copy the console reads (`console/ops/runs`) without following any link: the data folder is
  writable by the console's user, and a planted link must not make root write elsewhere.

Before an operation, the page names the services it touches and the work under way. Afterwards the operation's
page shows its state and output as they come, its result and the service's health after it. It follows the
host's record, so a reload or a restart of the console loses nothing. Operators restart the runner and the
engine and run the setup check. Only admins run setup.

**Restart the engine when it is idle.** Restarting the engine ends its tasks' sessions, so the operation goes in
steps:

1. It asks the runner to start nothing new in the engine (`drain` in `runner_state`). The runner then takes no
   new tickets, and any call to the engine (a dispatch, a follow-up, a code review, a test run, an architecture
   review) answers busy, which every caller already treats as "try again later". The runner says it has seen the
   request on its next pass.
2. It waits for the engine's own running tasks to end, however long they take, and shows which ones it is waiting
   for. A run that waits longer than its own timeout ends as such.
3. It restarts `bulava-engine` and lifts the request, whatever the outcome.

While it waits, it can be cancelled, which lifts the request as well. The operation's record keeps owing the
lift until the runner's database confirms it is gone; if lifting fails (the database busy, the machine
restarted mid-operation), every tick tries again. A lift only ever removes its own operation's request, never a
newer one.

**Restart the runner** restarts it straight away. A task in work keeps going, since its session belongs to the
engine. The operation counts as done once `runner.service` is active again and the runner has recorded that it
started with `config.yaml` (Settings then shows whether that is the saved version).

## Grafana

**Monitoring** opens Grafana at `/grafana/` on the console's address, in a new tab, as do the dashboards' links on Home. nginx asks the console who the visitor is
before each request; without a session it sends them to the sign-in, and from there back to the dashboard they
opened (its path; its time range starts over). Grafana gets the person's email in
`X-WEBAUTH-USER`, as a Viewer, or an Admin for the console's admins, and makes their Grafana user on the first
visit. nginx writes those headers from the console's answer over anything the browser sent, and Grafana takes
them only from 127.0.0.1. With the console on, Grafana's anonymous viewing is off; the SSH tunnel still reaches
it, and its admin password (`~/arceus/data/grafana-admin-password`) still signs in there.

## People and roles

Anyone can ask for an account on the sign-in page; it waits until an admin approves it on **People** and picks
its roles. An admin can also invite someone with a link that makes an account with roles already set. Roles
combine:

| Role | What it adds |
|---|---|
| (none) | sees the status, the tasks, the reports, the findings and the monitoring; changes nothing |
| QA | starts and stops test runs of the platform, edits their instructions |
| Architect | decides on architecture findings, starts and stops reviews, edits their instructions |
| Operator | sees the settings, restarts the runner and the engine, runs the setup check |
| Admin | everything: edits the settings and the project's notes, runs setup, approves people and gives roles |

The server checks the role on every request; a button that is not shown is not the check. A change of roles
applies on the next page the person opens. Disabling an account signs it out everywhere at once. The last active
admin can be neither disabled nor demoted: to hand the factory over, make the next person an admin first, then
disable yourself or let them do it. **People → What people did** lists sign-ins, approvals, role changes,
decisions, settings saved and operations started, with who did it.

A forgotten password: an admin makes a reset link on the person's page. When nobody can sign in, from the server:

```bash
/opt/arceus-runner/venv/bin/arceus-runner console reset-password you@example.com [--enable]
/opt/arceus-runner/venv/bin/arceus-runner console users
```

## Sessions and forms

Passwords are stored as scrypt hashes. Five wrong passwords for one address from one network address within 15
minutes make the next attempt from there wait; thirty from one network address, whatever the accounts, too. A
stranger's wrong passwords therefore do not keep an account's owner out (only a hundred from everywhere in 15
minutes would). One network address can ask for at most five accounts an hour. Email addresses are printable ASCII
only, and names keep no control characters: both go into the headers nginx passes to Grafana. A session lasts `session_idle` (24 hours) after the last page opened
and `session_max` (14 days) at most. Its cookie is `HttpOnly` and `SameSite=Lax`, and behind `https://` it is a
`__Host-` cookie with `Secure`, and the pages carry `Strict-Transport-Security`. Every form carries a token of its session, and a form the browser says came from
another site is refused.
