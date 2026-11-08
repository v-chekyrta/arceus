# Cleanup

Agents leave things behind: stopped containers and their test databases, dev servers whose session is gone, files in the repositories, screenshots. A dirty tree also stops the next ticket (`START_REFUSED`). The runner cleans up by rules; nothing an agent or a model says decides what is removed. The rules and their checks are described in full in [`cleanup.py`](../runner/src/arceus_runner/cleanup.py).

**What goes.** Only what belongs to a task that is over: done, stopped by hand, or failed (or stopped as people closed every MR) longer ago than `continue_window`, since `/continue` still needs its branches, notes and containers. A task that waits for an answer, a review or a security reviewer keeps everything. Ownership comes from:

- the label `arceus.task=<KEY>` the agent puts on what it makes outside a compose file ([agent-notes.md](../config/agent-notes.md));
- the place: `.arceus/media/<KEY>` (after `keep_media`), `.arceus/notes/<KEY>`, `.arceus/replies/<KEY>`, `.arceus/ticket/<KEY>` (after `keep_notes`), the task's branch;
- the time: one task at a time holds the workspace (`workspace_holds`), so a stopped container that mounts it, a `docker compose run` container of a workspace project or a dev server started in it while task X held it is X's. A compose service's container is never removed by time alone: a dev stack the agent recreated looks the same.

| Kind | Removed | Never |
|---|---|---|
| containers | stopped ones of a finished task, after `stopped_after`, with their anonymous volumes unless something else uses or protects one; running ones and compose services only with the task's label | foreign projects, `arceus-observability`, `arceus-console`, `protect`, `arceus.keep=true`, a running container or compose service tied by time only, a container holding a protected volume |
| volumes | with a finished task's label, when no container uses them | named volumes without a label: dependency caches and the dev stack's data |
| networks | empty ones labelled for a finished task or belonging to its compose project | networks made by hand (compose `external:`); empty ones nobody can be tied to (reported) |
| images | built with a finished task's label, when no container uses them | everything else; untagged images are only counted |
| repositories | uncommitted files on a finished task's branch go to `git stash` (`arceus-cleanup …`); the task's local branch once the host has its commits or it is exactly what was published to a merged MR | ignored files (`node_modules`, builds); anything on a branch that is no finished task's |
| processes | dev servers started in the workspace while a finished task held it, with no `tmux`, `claude` or `codex` above them | anything below a live session |

Everything else is **reported**, never removed, for a person to clean by hand (for now, once a week): unlabelled containers and volumes, empty networks, unused images by size, untagged images and anonymous volumes (counts), the build cache, and files beside the repositories in the workspace. A name that spells a task (`shop7173-mysql`) is shown as a hint, never as proof. `arceus-runner cleanup --last` shows the full report; Grafana counts it. With `llm: advise` Claude (no tools) adds a one-line opinion to each reported item.

**When.** Inside the runner's tick, so nothing switches the workspace meanwhile:

- the workspace has been free with no work waiting for `idle_after`, and the last full pass is `min_interval` old;
- `force_every` (24 h) without a pass: a full pass at the first free moment, before the next ticket; while a task works all day, an independent pass that touches only labelled non-compose objects and `.arceus` folders of finished tasks;
- low disk (`disk_free` warns or fails): a pass every `retry_after`;
- never while Bulava still has a live session for a task the runner sees as waiting, or does not answer.

A pass lasts at most `max_duration` (10 min). A pass with errors or cut short is `partial` or `interrupted` and is tried again after `retry_after`, doubling up to `force_every`; every removal is checked again right before it is made.

**Starting it.** `cleanup.mode: report` first: it logs what it would remove and changes nothing (`mode: observe` is always report). Read a few reports:

```bash
arceus-runner cleanup            # what a full pass would do now, and why; changes nothing
arceus-runner cleanup --last     # the runner's last pass
```

Add the dev stack's compose project to `cleanup.protect.compose_projects` if it lives in the workspace, then set `mode: act` and restart the runner. `arceus-runner cleanup --act` makes a full pass by hand while the runner is stopped. Test data in the dev stack's own database is never touched: nothing tells which rows a task wrote.

`ARCEUS_REAL_DOCKER=1 pytest tests/test_cleanup.py` checks the rules against the VM's Docker with objects of its own.
