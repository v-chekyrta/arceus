# Arceus

Arceus runs AI coding agents on a server instead of a developer's laptop. A person tags a ticket in the issue tracker; an agent (Claude Code, reviewed by Codex) does the work on its own branch in the project's repositories on the code host; a merge request with exactly the commit Codex reviewed comes back for people to merge.

The issue tracker and the code host are interfaces. YouTrack and GitLab ship; another tracker or host is a module of its own ([docs/integrations.md](docs/integrations.md)).

## How it fits together

```
issue tracker ──tag "agent"──▶ runner ──dispatch / worker-send──▶ Bulava ──▶ Claude Code (writes, commits)
 (YouTrack)                     │  ▲                                  ├──▶ Claude code + security checks (advise)
   ▲                            │  │                                  └──▶ Codex (reviews)
   │ comments, state            │  └── task show: state, verdict, reviewed commits
   └────────────────────────────┤
                                └──push reviewed commit, open MR──▶ code host ──▶ CI, people merge
                                                                    (GitLab)
runner + Bulava events ──▶ Loki ─┐
runner metrics, VM ──▶ Prometheus ┴──▶ Grafana
```

- **Runner** (`runner/`, Python, a systemd service). The deterministic bridge: it takes tickets tagged `agent` by someone on the project's allowlist, prepares the repositories, starts the task in Bulava, and once Codex has passed the work pushes exactly the reviewed commit, opens the merge request and moves the ticket to Code Review. Review comments that mention the bot, red CI and conflicts go back to the agent until the MRs are merged. It also screens every text before the agent sees it and cleans up what finished tasks left behind. State is in SQLite; every external step can be repeated after a crash without doing anything twice. How a ticket travels: [docs/task-lifecycle.md](docs/task-lifecycle.md).
- **Bulava** ([Stepanokdev/bulava](https://github.com/Stepanokdev/bulava) at a pinned commit, `BULAVA_REF` in [setup/setup.conf.example](setup/setup.conf.example); `engine/SERVER.md` is the contract). The engine that runs the agent: a Claude Code session in tmux, Codex as the only reviewer ([docs/reviews.md](docs/reviews.md)), the task record that outlives sessions, usage limits, the server profile that keeps the agent away from credentials.
- **Setup** (`setup/`). One idempotent script that prepares the VM; `--check` reports what is not done without changing anything. [setup/README.md](setup/README.md) is the operator's guide.
- **Observability** (`observability/`). Grafana with Prometheus and Loki on the VM.
- **Console** (`console/`, `runner/src/arceus_runner/console`). One web address with a sign-in and roles, instead of SSH tunnels: [docs/console.md](docs/console.md).

Tickets do not say which repository to change. The session starts in a workspace folder with every repository of the project's group; the agent works out which ones the ticket needs, and merge requests are opened only where it changed something.

## Status

Working and checked on an Ubuntu 26.04 (x86_64) VM against a real YouTrack project and GitLab group:

- setup, sign-in with the usual `/login` of Claude and Codex, `arceus-runner check` and `doctor`;
- ticket → branches → Bulava → Codex verdict → push of the reviewed commit → MR → Code Review, with questions and answers, failures explained on the ticket, research tickets answered on the ticket, and recovery after a crash or a reboot;
- the loop after the MR: review threads, red pipelines, conflicts and ticket comments go back to the agent; fixes go to the same MR, each thread gets its own answer, the task is done when all its MRs are merged;
- the Slack channel of the project, Grafana dashboards, cleanup between tasks.

Optional, off by default:

- **Code review on request** (`review_agent`): `/review` on a ticket in Code Review reviews its MRs against the ticket, approves through a second GitLab account when the review, CI and the commit all agree, and sends what has to change back to the agent. Also reviews MRs people wrote. [docs/reviews.md](docs/reviews.md#code-review-on-request).
- **Testing the platform** (`qa`): on command or a schedule, the agent tests the whole platform in a browser by an instructions file kept on the server and writes a report with steps, screenshots and the suspected cause in the code. [docs/qa.md](docs/qa.md).
- **Architecture reviews** (`architect`): on command or a weekly schedule, the agent reads what changed since the last review and how it fits with the rest: features that clash, code written to look busy, tests that prove little, modules worth deepening, drift from the team's rules. Codex checks each finding before the report goes out; a person accepts the risk or marks a false positive on the report's page, and the next review is told. [docs/architect.md](docs/architect.md).

Not there yet: the agent pushing its own branch (Bulava's server profile refuses `git push` inside a run, so the runner pushes the reviewed commit), an outbound traffic allowlist, an alert when the runner itself is down, and the metrics that need MR history.

## Repository

| Path | What |
|---|---|
| [`runner/`](runner) | the runner: `arceus-runner check | doctor | probe-bulava | cleanup | rebind | qa | arch | run`, tests in `runner/tests` |
| [`setup/`](setup) | `setup.sh`, `setup.conf.example` and checks; **[setup/README.md](setup/README.md) is the operator's guide** |
| [`docs/`](docs) | how the parts work: the [task lifecycle](docs/task-lifecycle.md), [reviews](docs/reviews.md), [testing the platform](docs/qa.md), [architecture reviews](docs/architect.md), [cleanup](docs/cleanup.md), the [security screen](docs/security.md), [what the agent is told](docs/agent-instructions.md), [trackers and code hosts](docs/integrations.md) |
| [`config/`](config) | example `config.yaml`, `secrets.env`, `bulava.env`, project notes and QA instructions; `agent-notes.md`, the agent's server rules |
| [`observability/`](observability) | Docker Compose stack and the generator of the Grafana dashboards |
| [`console/`](console) | the web console's image and Docker Compose project (the code is `runner/src/arceus_runner/console`) |
| `sandbox/` | local only, git-ignored: the server in a Docker container on a Mac, for the tests that need no model and the review cycles with the real Claude and Codex; `sandbox/README.md` beside it says how |

Running the tests locally:

```bash
cd runner && uv venv && uv pip install -e '.[test,console]' && .venv/bin/pytest -q -n auto   # in parallel: about 35 s on Linux, 75 s on a Mac
ARCEUS_REAL_ENGINE=/opt/bulava/engine .venv/bin/pytest -q   # on a machine with Bulava installed
```

`./verify.sh` from the root runs the runner's tests and the setup script's tests; no model is called. `./verify.sh --offline` leaves out the one step that fetches the engine's upstream from GitHub; GitHub Actions runs it on every pull request and every push to `main`, on Python 3.12 and 3.13 ([.github/workflows/tests.yml](.github/workflows/tests.yml)). Tests that need the installed engine, Docker or a real model skip there and say why. The sandbox's scenarios are run by hand (`sandbox/sandbox.sh test`, `sandbox/sandbox.sh live`).

## Licence

**FSL-1.1-ALv2**: the Functional Source License with an Apache 2.0 future grant. See
[LICENSE](LICENSE).

In plain words: read it, change it, run it, use it inside your company, teach and research with
it. What you may not do is sell a competing product or service built on it. Two years after each
version is published, that version becomes available under Apache 2.0 as well, and the restriction
no longer applies to it.

The licence covers Arceus's own code. Bulava is not part of this repository: `setup.sh` fetches it
from `BULAVA_SOURCE`, and it comes under its own licence, FSL-1.1-ALv2 as well.
