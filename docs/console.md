# The web console

One address with a sign-in for everything people do with the factory, instead of SSH tunnels. It runs on the VM
in Docker beside the observability stack and is set up by `setup.sh` (the `console` phase).

## How it is put together

```
browser ──https──▶ load balancer (ends TLS) ──http──▶ nginx :console.port ──▶ console 127.0.0.1:8180
                                                          │
                                                          └─ /grafana/ ─▶ Grafana 127.0.0.1:3000, once the console says who asks
console ──reads/writes──▶ ~/arceus/data: runner.db, config.yaml, console/console.db
console ──reads──▶ the runner's metrics (127.0.0.1:9464) and events.jsonl
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
checks that fail with their reason (from `events.jsonl`), and Claude's and Codex's subscription usage. The part
refreshes itself every 15 seconds.

It comes from `runner.db` (read-only), the runner's metrics and the tail of `events.jsonl`. Whatever of these
cannot be read is said as such, never shown as fine: no database yet, metrics off or not answering, a pass that
ended more than 15 minutes ago ("stuck"), a `config.yaml` that does not load.

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
