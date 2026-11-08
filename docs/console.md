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
  read-only.
- **Why Docker and not a service on the host.** Both can work. The console is the one part of Arceus reachable
  from outside; on the host it would run as the run user, who has passwordless sudo, so a hole in it would be root
  on the machine. In the container it is a process without sudo and without the tokens.
- **The security boundary is still the VM.** The agents run as the same user with sudo (`docs/security.md`): they
  can read `console.db` and change anything the console can. The console keeps strangers out; it does not keep
  the agents in.

## Turning it on

In `config.yaml`:

```yaml
console:
  enabled: true
  public_url: https://console.example.com   # the address people open
  port: 80                                  # what nginx listens on; the load balancer forwards public_url here
  trusted_proxies: [10.0.0.0/8]             # who may say who the visitor is (X-Forwarded-For)
```

Then `sudo ./setup.sh` (or `--phase console`). The first admin comes from the server:

```bash
/opt/arceus-runner/venv/bin/arceus-runner console invite --admin you@example.com
```

It prints a one-time link (48 hours). Open it, choose a name and a password, and sign in. While the console is
on, the runner no longer serves the old report pages on `qa.port` and `architect.port`.

## Home

What the factory does now, in one line at the top: working on a ticket (and which), running a platform test or
an architecture review, idle, waiting on people, held by a failing health check, observing, or that the runner
does not answer. Under it: whether the runner answers and when it last finished a pass, the queue, the tickets
waiting on people, those in code review, what runs now, the latest test run and architecture review, the health
checks that fail with their reason, and Claude's and Codex's subscription usage. The part
refreshes itself every 15 seconds.

It comes from `runner.db` (read-only) and the runner's metrics. The runner writes its health checks with their
detail into `runner.db` (`runner_state`) whenever one changes, so a failure that began a week ago still shows
why. Whatever cannot be read is said as such, never shown as fine: no database yet, metrics off or not
answering, a runner that answers but has not finished its first pass ("starting": health and usage unknown), a
pass that ended more than 15 minutes ago ("stuck"), a check without a recorded reason, a `config.yaml` that does
not load.

## Test runs

**Test runs** lists every run of the platform test, newest first, queued and running ones included, and opens a
finished one as its report: the verdict, what was checked, each problem with its steps, its screenshots and videos
at the step they show, expected against actual and the suspected cause with links into the code. The list stays
when `qa.enabled` is false; it says that none start. Only the report's own files are served: a run's media by its
exact name, never the copy of the instructions a run keeps (it holds the test accounts), `result.json` or any other
path.

People with the QA role and admins start a run (one at a time, as `arceus-runner qa run` does; the run records
their email as who asked) and stop one under way. They also edit the **test instructions**, which nobody else can
open, since the file holds the test accounts. The editor saves atomically and keeps the file's mode. A save over a
version that changed meanwhile (another person, an edit over SSH) is refused with the difference shown. Every
saved version is kept with who saved it and can be opened again in the editor. The page also says what the agent
would get: an instructions file that is empty without its comments, or too large, would stop a run at once.

## Architecture

**Architecture** lists the reviews and a count of open, decided and fixed findings; **All findings** shows every
finding kept. A review opens as its report, built from `runner.db` on every visit. Each finding shows Codex's
second check apart from the person's decision.

People with the architect role and admins decide: **Accept the risk** or **False positive**, each with a reason,
or **Take the decision back**. Everyone else sees the decisions without the buttons, and a direct POST from them
is refused. The finding shows who decided, when and why, and **Decisions on it** lists every decision before.
The form carries the version of the finding the page showed. If someone else decided in the meantime, the second
decision is refused and the page says what stands now. The next review gets the reason only after the security
screen cleared it, quoted as data ([architect.md](architect.md)). Architects also start and stop a review and edit
the review instructions, the same way as test runs.

## Settings

**Settings** is `config.yaml` as a form, a page per section (tracker, code host, schedule, models, test runs and
so on). Every setting of the runner's schema is there with its current value: what the file says, as written, or
the default when the file does not say it. A setting the form cannot hold as a field (a map such as
`review_agent.repos` or `slack.people`, or an optional block such as `qa.schedule`) is edited as YAML in place.
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
on top of the file as it is now, to save or drop. Saving restarts
nothing.

**History** lists every version saved from the console, with who saved it and why, and shows which one is in the
file now and which one the runner runs with. Any version, or what was there before it, opens in the YAML editor
and goes back through the same check. The runner records which `config.yaml` it started with (`runner_state` in
`runner.db`), so Settings says when the file has changed since the runner started and needs a restart to apply.

Operators see all of it read-only. Nobody else opens it. Secrets are not in `config.yaml` (only the names of
the variables in `secrets.env`), and the console never reads `secrets.env`.

## Grafana

**Monitoring** opens Grafana at `/grafana/` on the console's address. nginx asks the console who the visitor is
before each request; without a session it sends them to the sign-in. Grafana gets the person's email in
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
| (none) | sees the status, the reports, the findings and the monitoring; changes nothing |
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

Passwords are stored as scrypt hashes. Five wrong passwords for one address within 15 minutes (or thirty from one
network address) make the next attempt wait. A session lasts `session_idle` (24 hours) after the last page opened
and `session_max` (14 days) at most. Its cookie is `HttpOnly` and `SameSite=Lax`, and behind `https://` it is a
`__Host-` cookie with `Secure`. Every form carries a token of its session, and a form the browser says came from
another site is refused.
