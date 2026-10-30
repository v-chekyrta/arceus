# Arceus

Arceus runs AI coding agents on a server instead of a developer's laptop. A person tags a ticket in the issue tracker; an agent (Claude Code, reviewed by Codex) does the work on its own branch in the project's repositories on the code host; a merge request with exactly the commit Codex reviewed comes back for people to merge.

The issue tracker and the code host are interfaces. Arceus ships with YouTrack as the tracker and GitLab as the code host; another tracker or host is a module that implements the same interface (see [Trackers and code hosts](#trackers-and-code-hosts)).

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

- **Runner** (`runner/`, Python, a systemd service). The deterministic bridge. It polls the issue tracker for tickets tagged `agent` by someone on the project's allowlist, prepares the repositories, starts the task in Bulava, follows it with `night-shift task show`, and when Codex has passed the work it makes sure the code host holds exactly the reviewed commit, opens and describes the merge request and moves the ticket to Code Review. It then watches the MR: review comments that mention the bot, red CI and conflicts go back to the agent, which either fixes the code or answers the question in its thread, and each reviewed fix is published to the same MR. Questions the agent cannot answer go to the ticket as comments; the answer goes back to the agent. Between tasks it clears what the agents left behind (stopped containers, test volumes, orphaned dev servers, uncommitted files, old screenshots), but only what it can tie to a task that is over; the rest it reports. All of its state is in SQLite, and every external step can be repeated after a crash without doing anything twice.
- **Bulava** ([Stepanokdev/bulava](https://github.com/Stepanokdev/bulava), a pinned commit of branch `factory`, `engine/SERVER.md` is the contract). The engine that runs the agent: a Claude Code session in tmux, after a green build a code review and a security review by Claude sessions that never saw the ticket (`bulava.claude_checks`, see [setup/README.md](setup/README.md#independent-checks-before-codex)), Codex as reviewer, the task record that outlives sessions, usage limits, the server profile that keeps the agent away from credentials.
- **Setup** (`setup/`). One idempotent script that prepares the VM: packages, Docker, Chrome, pinned `claude`/`codex`/`chrome-devtools-mcp`, the Bulava engine, the runner, the systemd units and the observability stack. `--check` reports what is not done without changing anything.
- **Observability** (`observability/`). Grafana with Prometheus and Loki on the VM, reached through an SSH tunnel: what the agent is doing now, what happened to every ticket, and the VM's resources.

Tickets do not say which repository to change, and no repository is the main one. The session starts in the workspace folder that holds every repository of the project's group on the code host; the agent works out which of them the ticket needs and changes those. Merge requests are opened only where it changed something.

## Trackers and code hosts

The runner's flow (`loop`, `delivery`, `review`, `screening`, `cleanup`) never talks to YouTrack or GitLab directly. It talks to two interfaces, defined in [`runner/src/arceus_runner/integrations.py`](runner/src/arceus_runner/integrations.py):

- **`IssueTracker`**: where tasks come from. The queue of tagged issues, who put the tag on, the issue's text and state, tags, comments, attachments, links and mentions.
- **`CodeHost`**: where the repositories live. The repositories of one group, merge requests, their discussion threads, CI runs and job logs, uploads, and the bot's git credentials. Git itself is not abstracted: the workspace is plain git repositories, cloned and pushed over HTTPS.

`config.yaml` picks the implementation of each with `type`:

```yaml
tracker:
  type: youtrack           # built in; or the path of your own module, e.g. acme_arceus.jira
  bot_user: agent-runner
  allowed_users: [j.doe]
  states: { queued: To Do, in_progress: In Progress, code_review: Code Review }
  url: https://youtrack.example.com     # from here on, keys of the type
  project: ACME
code_host:
  type: gitlab
  bot_user: acme-agent-bot
  git_author: { name: "ACME Agent", email: "acme-agent-bot@example.com" }
  url: https://gitlab.example.com
  group: clients/acme
```

The keys at the top of each section (`bot_user`, `allowed_users`, `tags`, `states`; `git_author`, `exclude_repos`, `branch_prefix`, `mr_label`) mean the same for every provider. The rest belong to the type. An older `config.yaml` with `youtrack:` and `gitlab:` sections still loads as the same settings.

### Adding a tracker or a code host

Only YouTrack and GitLab ship today. Jira, GitHub or anything else can be added without touching the runner's flow:

1. Write one module, using [`youtrack.py`](runner/src/arceus_runner/youtrack.py) or [`gitlab.py`](runner/src/arceus_runner/gitlab.py) as the example. It has three parts:
   - the config section: a subclass of `IssueTrackerConfig` or `CodeHostConfig` with your keys;
   - the adapter: a class with the methods of `IssueTracker` or `CodeHost`;
   - `TRACKER = TrackerType(config=..., build=...)` or `CODE_HOST = CodeHostType(...)`.
2. Register it. In this repository, add a line to `BUILTIN_TRACKERS` or `BUILTIN_CODE_HOSTS` in `integrations.py` and use its short name as `type`. Outside it, install your package next to the runner and put its module path in `type`.
3. Run `arceus-runner check`. An unknown `type`, a missing key or a key the provider does not know is reported before anything starts.

What every implementation has to keep:

- **`trigger()` is the security boundary.** The runner starts work only when the person who put the `queue` tag on is in `allowed_users`. Return the author of that change as the tracker's own history records it, never the reporter, the assignee or the last editor. If the history does not say, return `None`: the issue is then not taken.
- **Requests go through `http.ServiceClient`.** It gives the service its own pause and backoff, and raises the errors the flow handles (`AuthError`, `NotFound`, `BadRequest`, `TransientError`).
- **Writes are safe to repeat.** The runner journals each write and repeats it after a crash. Its comments carry a marker it looks for before posting again, so `comments()` and `notes()` must return the bot's own comments with their text intact.
- **Ids are opaque.** Numbers or strings, the runner stores them and hands them back.

Some capabilities are optional. Without them the flow keeps working, with less:

| Capability | Without it |
|---|---|
| CI runs on the merge request (`MRStatus.ci_*`) | a red pipeline is never sent to the agent; people say it in a comment |
| Logs of failed jobs (`failed_jobs`) | the agent gets the link to the run only |
| Conflict detection (`MRStatus.conflict`) | `None` means "not known" and sends nothing; a conflict reaches the agent through people |
| Threaded replies | `reply()` posts a plain comment on the merge request instead |
| Uploads to the code host (`supports_uploads`) | screenshots go to the ticket only |
| Attachments on the tracker (`supports_attachments`) | screenshots go to the merge requests only |
| Assignee (`assign_to_bot`) | nothing is assigned |
| Approval (`supports_approval`, `approve`, `revoke`; `code_host.approver`) | the review agent says what it found in a note and approves nothing |

## Status

Working and checked on an Ubuntu 26.04 (x86_64) VM against a real YouTrack project and GitLab group:

- setup, sign-in with the usual `/login` of Claude and Codex, `arceus-runner check` and `doctor`;
- reading the queue and the tag's author, the allowlist, state changes, assignee, comments and tags on tickets;
- ticket → branches in the group's repositories → Bulava → Codex verdict → push of the reviewed commit → MR → Code Review, including questions and answers, failures with a reason on the ticket, and recovery after a crash or a reboot (tested against the installed engine with a stub instead of Claude);
- the loop after the MR: review notes that mention the bot (also late replies in old threads, each thread sent whole), the current red pipeline with its failed jobs' logs, confirmed conflicts and ticket comments go back to the agent as follow-ups of the same task; the fix is published to the same MR, each thread gets its own answer (with the commit where code changed, without one where the agent only explained), the task is done when all its MRs are merged; an MR a person closes while others are open goes to the agent, which reverts that repository's changes, and the runner then drops the MR from the task (a revert the agent makes on its own closes the open MR the same way);
- Grafana dashboards fed by the runner's metrics and the runner's and Bulava's events.

Optional, off by default: **code review on request** (`review_agent`). `/review` on a ticket in Code Review starts a separate Bulava task that reviews the task's open MRs against the ticket, builds and tests them without changing them, and writes a verdict; the runner approves an MR (as a second GitLab account, `code_host.approver`) only when the review passed it, CI passed on that commit and nothing moved meanwhile, and otherwise says on the MR what has to change. When an approved MR touches what `attention_paths` lists (auth, permissions, secrets…) or something the reviewer says a person has to decide, its note also names those changes for a person to look at before merge; verified changes the ticket asked for do not. What only whoever deploys it needs to know (an environment variable to set, a requested runtime upgrade) is listed in the note under its own heading and does not call a person. It never asks anyone a question. What has to change goes back to the author, and its new commits are reviewed again without anyone asking, up to `fix_rounds` times per ticket (default 3); then a person takes over. Checked against the pinned engine with a stub instead of Claude; how it decides is in [setup/README.md](setup/README.md#code-review-on-request).

Not there yet:

- **The agent pushing its own branch.** The agent's git is set up (fetch and pull work), but Bulava's server profile refuses `git push` inside a run; a setting for it is requested from Bulava. Until then the runner pushes the reviewed commit itself.
- **Branch protection on the code host** is not set up by `setup.sh`. `setup/checks/gitlab-protection.py` reports repositories where Developers (and so the bot) may merge into `main` or force push; a GitLab admin has to change it.
- An outbound traffic allowlist, alerts, and the metrics that need MR history.

## Repository

| Path | What |
|---|---|
| [`runner/`](runner) | the runner: `arceus-runner check | doctor | probe-bulava | cleanup | rebind | run`, tests in `runner/tests`; the tracker and code host interfaces in `integrations.py`, YouTrack and GitLab in `youtrack.py` and `gitlab.py` |
| [`setup/`](setup) | `setup.sh`, `setup.conf.example` and checks; **[setup/README.md](setup/README.md) is the operator's guide** |
| [`config/`](config) | example `config.yaml`, `secrets.env` and `bulava.env` |
| [`observability/`](observability) | Docker Compose stack and the generator of the Grafana dashboards |
| `sandbox/` | local only, git-ignored: the server in a Docker container on a Mac, installed by `setup.sh`, for the tests that need no model and the review cycles with the real Claude and Codex; how to use it is in `sandbox/README.md` beside it |

Running the tests locally:

```bash
cd runner && uv venv && uv pip install -e '.[test]' && .venv/bin/pytest -q -n auto   # in parallel, under a minute
ARCEUS_REAL_ENGINE=/opt/bulava/engine .venv/bin/pytest -q   # on a machine with Bulava installed
```

`./verify.sh` from the root runs the runner's tests and the setup script's test. No model is called. The sandbox and its live scenarios are not part of it: they are for work on the pipeline itself and are run by hand (`sandbox/sandbox.sh test`, `sandbox/sandbox.sh live`).

Where a local `sandbox/` exists, the same runs in a container before anything reaches the VM; `sandbox/README.md` there says how.
