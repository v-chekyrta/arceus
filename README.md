# Arceus

Arceus runs AI coding agents on a server instead of a developer's laptop. A person tags a YouTrack ticket; an agent (Claude Code, reviewed by Codex) does the work on its own branch in the project's GitLab repositories; a merge request with exactly the commit Codex reviewed comes back for people to merge.

## How it fits together

```
YouTrack ──tag "agent"──▶ runner ──dispatch / worker-send──▶ Bulava ──▶ Claude Code (writes, commits)
   ▲                        │  ▲                                  └──▶ Codex (reviews)
   │ comments, state        │  └── task show: state, verdict, reviewed commits
   └────────────────────────┤
                            └──push reviewed commit, open MR──▶ GitLab ──▶ CI, people merge
runner + Bulava events ──▶ Loki ─┐
runner metrics, VM ──▶ Prometheus ┴──▶ Grafana
```

- **Runner** (`runner/`, Python, a systemd service). The deterministic bridge. It polls YouTrack for tickets tagged `agent` by someone on the project's allowlist, prepares the repositories, starts the task in Bulava, follows it with `night-shift task show`, and when Codex has passed the work it makes sure GitLab holds exactly the reviewed commit, opens and describes the merge request and moves the ticket to Code Review. It then watches the MR: review comments that mention the bot, red CI and conflicts go back to the agent, which either fixes the code or answers the question in its thread, and each reviewed fix is published to the same MR. Questions the agent cannot answer go to the ticket as comments; the answer goes back to the agent. All of its state is in SQLite, and every external step can be repeated after a crash without doing anything twice.
- **Bulava** ([Stepanokdev/bulava](https://github.com/Stepanokdev/bulava), branch `factory`, `engine/SERVER.md` is the contract). The engine that runs the agent: a Claude Code session in tmux, Codex as reviewer, the task record that outlives sessions, usage limits, the server profile that keeps the agent away from credentials.
- **Setup** (`setup/`). One idempotent script that prepares the VM: packages, Docker, Chrome, pinned `claude`/`codex`/`chrome-devtools-mcp`, the Bulava engine, the runner, the systemd units and the observability stack. `--check` reports what is not done without changing anything.
- **Observability** (`observability/`). Grafana with Prometheus and Loki on the VM, reached through an SSH tunnel: what the agent is doing now, what happened to every ticket, and the VM's resources.

Tickets do not say which repository to change, and no repository is the main one. The session starts in the workspace folder that holds every repository of the project's GitLab group; the agent works out which of them the ticket needs and changes those. Merge requests are opened only where it changed something.

## Status

Working and checked on an Ubuntu 26.04 (x86_64) VM against a real YouTrack project and GitLab group:

- setup, sign-in with the usual `/login` of Claude and Codex, `arceus-runner check` and `doctor`;
- reading the queue and the tag's author, the allowlist, state changes, assignee, comments and tags on tickets;
- ticket → branches in the group's repositories → Bulava → Codex verdict → push of the reviewed commit → MR → Code Review, including questions and answers, failures with a reason on the ticket, and recovery after a crash or a reboot (tested against the installed engine with a stub instead of Claude);
- the loop after the MR: review notes that mention the bot (also late replies in old threads, each thread sent whole), the current red pipeline with its failed jobs' logs, confirmed conflicts and ticket comments go back to the agent as follow-ups of the same task; the fix is published to the same MR, each thread gets its own answer (with the commit where code changed, without one where the agent only explained), the task is done when all its MRs are merged; an MR a person closes while others are open goes to the agent, which reverts that repository's changes, and the runner then drops the MR from the task (a revert the agent makes on its own closes the open MR the same way);
- Grafana dashboards fed by the runner's metrics and the runner's and Bulava's events.

Not there yet:

- **The agent pushing its own branch.** The agent's git is set up (fetch and pull work), but Bulava's server profile refuses `git push` inside a run; a setting for it is requested from Bulava. Until then the runner pushes the reviewed commit itself.
- **Branch protection in GitLab** is not set up by `setup.sh`. `setup/checks/gitlab-protection.py` reports repositories where Developers (and so the bot) may merge into `main` or force push; a GitLab admin has to change it.
- An outbound traffic allowlist, alerts, and the metrics that need MR history.

## Repository

| Path | What |
|---|---|
| [`runner/`](runner) | the runner: `arceus-runner check | doctor | probe-bulava | run`, tests in `runner/tests` |
| [`setup/`](setup) | `setup.sh`, `setup.conf.example` and checks; **[setup/README.md](setup/README.md) is the operator's guide** |
| [`config/`](config) | example `config.yaml`, `secrets.env` and `bulava.env` |
| [`observability/`](observability) | Docker Compose stack and the generator of the Grafana dashboards |

Running the tests locally:

```bash
cd runner && uv venv && uv pip install -e '.[test]' && .venv/bin/pytest -q
ARCEUS_REAL_ENGINE=/opt/bulava/engine .venv/bin/pytest -q   # on a machine with Bulava installed
```
