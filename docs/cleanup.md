# Cleanup

Agents leave things behind: stopped containers and their test databases, dev servers whose session is gone, files in the repositories, screenshots. A dirty tree also stops the next ticket (`START_REFUSED`). The runner cleans up by rules; nothing an agent or a model says decides what is removed. The rules and their checks are described in full in [`cleanup.py`](../runner/src/arceus_runner/cleanup.py).

**What goes.** Only what belongs to a task that is over: done, stopped by hand, or failed (or stopped as people closed every MR) longer ago than `continue_window`, since `/continue` still needs its branches, notes and containers. A task that waits for an answer, a review or a security reviewer keeps everything, unless `release_after` is set (below). Ownership comes from:

- the label `arceus.task=<KEY>` the agent puts on what it makes outside a compose file ([agent-notes.md](../config/agent-notes.md));
- the place: `.arceus/media/<KEY>` (after `keep_media`), `.arceus/notes/<KEY>`, `.arceus/replies/<KEY>`, `.arceus/ticket/<KEY>` (after `keep_notes`), the task's branch;
- the time: one task at a time holds the workspace (`workspace_holds`), so a stopped container that mounts it, a `docker compose run` container of a workspace project or a dev server started in it while task X held it is X's. A compose service's container is never removed by time alone: a dev stack the agent recreated looks the same.

| Kind | Removed | Never |
|---|---|---|
| containers | stopped ones of a finished task, after `stopped_after`, with their anonymous volumes unless something else uses or protects one; running ones and compose services only with the task's label | foreign projects, `arceus-observability`, `arceus-console`, `protect`, `arceus.keep=true`, a running container or compose service tied by time only, a container holding a protected volume |
| volumes | with a finished task's label, when no container uses them; anonymous volumes a task's container left, unused for `orphaned_volumes_after` (below) | named volumes without a label: dependency caches and the dev stack's data |
| networks | empty ones labelled for a finished task or belonging to its compose project | networks made by hand (compose `external:`); empty ones nobody can be tied to (reported) |
| images | built with a finished task's label, when no container uses them; by age, untagged images and, if enabled, tagged ones (below) | images a container uses, stopped ones included; `arceus.keep=true`, `protect.images`, a protected compose project's images |
| repositories | uncommitted files on a finished task's branch go to `git stash` (`arceus-cleanup …`); the task's local branch once the host has its commits or it is exactly what was published to a merged MR | ignored files (`node_modules`, builds); anything on a branch that is no finished task's |
| processes | dev servers started in the workspace while a finished task held it, with no `tmux`, `claude` or `codex` above them | anything below a live session |

**A task left waiting.** A merge request can wait days for review, and its containers and test databases wait with it. With `release_after` (say `3d`), a task that waits that long with no work loses what can be made again. Owning something does not prove that, so for a waiting task the rules ask for a mark:

| Kind | Goes while the task waits | Stays until the task is over |
|---|---|---|
| containers | labelled `arceus.disposable=true`, or started with `--rm` (Docker removes those the moment they stop anyway), with their anonymous volumes | every other container of the task: its writable layer or anonymous volume may be the only copy of a reproduction |
| volumes | labelled `arceus.task=<KEY>` and `arceus.disposable=true` | the task's volumes without the mark |
| networks, images | the task's, as for a finished task: they hold no data | |
| processes | dev servers started while it held the workspace | |
| repositories, branches, `.arceus` folders | never | always |

- The wait is counted from its last work: the end of its last hold of the workspace (every round holds it), its MR comment or last `/continue`, its question, its failure. Not from when its record last changed, which moves on bookkeeping.
- Never for a task with review comments the agent has not been given yet, an accepted `/continue` waiting to be sent, or an engine session that is live or does not answer, in a full pass or an independent one. Each removal asks again right before it is made: a task that was answered meanwhile, or a container whose mark was taken off, keeps it (`kept`).
- `arceus.keep=true` and `cleanup.protect` still win. A compose service tied to the task by time only is never released.
- **The agent is told.** The next message the task is sent (a review round, an answer, a `/continue`, a resume) ends with a runner note naming what was removed since its last attempt began, so it builds, seeds or starts it again. If something cannot be made again, the note asks it to declare `blocked` with what is missing: the ticket gets the question, and an answer resumes it.
- A pass cut short leaves the rest `skipped` and goes on at the next pass; the note lists only what was really removed.
- It is off when the setting is absent. In `mode: report` the pass lists what would go and changes nothing.

**Anonymous volumes left behind.** A database image (`mongo`, `mysql`, `postgres`) makes anonymous volumes for its data, and `docker rm -f` without `-v` leaves them with no container and no label: on one server a week of test databases left 136 of them, 22.6 GB. Unused is not the same as garbage: such a volume can still be mounted by its ID, and may hold the only copy of someone's data. So the runner ties them to a task while the container still exists:

- every couple of minutes, and in every pass, it notes which anonymous volumes each container labelled `arceus.task=<KEY>` uses (one `docker ps`);
- a pass notes when it first sees such a volume unused by any container, running or stopped. Once it has stayed so for `orphaned_volumes_after` (24 h; `null` only counts them), it goes by its task's rules: when the task is over, or while it waits past `release_after` if the container was marked `arceus.disposable=true` or ran with `--rm`. The pass also checks again right before removing it that no container uses it;
- what keeps it: a container mounting it again or a new volume of the same name (the timer starts over), a pass that could not list the volumes (changes nothing), a volume two tasks' containers used (nobody's), `arceus.keep=true` and `cleanup.protect.volumes`.

A volume no task's container was seen using (a person's, another project's, a container that lived less than a couple of minutes) is only counted. `prune_unowned_volumes: true` removes those too after `orphaned_volumes_after`, host-wide, as `docker volume prune` would: only on a server where nobody keeps data that way.

**Old images and build cache.** Nothing says whose an untagged image or a build cache record is, so these go by age alone, host-wide, and only in a full pass (never while a task works):

| Rule | Goes | Stays |
|---|---|---|
| `build_cache_after` (7 days) | build cache no build has used that long: `docker builder prune --all --filter until=168h`. BuildKit counts from the last use, so cache made long ago and used yesterday stays | everything used more recently |
| `dangling_images_after` (7 days) | untagged images made that long ago that no container uses, each removed by its ID | images a container uses, running or stopped; younger ones; one labelled for a task goes by its task's rules instead |
| `unused_images_after` (off) | tagged images without a task label made that long ago that no container uses, each by its ID | base images, another project's, the dev stack's, as long as it is off |

- `arceus.keep=true`, `protect.images` (shell patterns of repositories or tags: `postgres`, `node:*`) and the images of `protect.compose_projects` stay under every rule, a task's own images included.
- An image's age is when it was made, not when it was last used. A base image pulled a month ago and used every day still counts as a month old, and with `unused_images_after` it goes whenever no container uses it at the moment of the pass. That is why the rule is off by default.
- Each image is inspected again right before it goes: tagged, labelled for a task or protected since the plan, or with other tags than planned, it stays (`kept`). Docker itself refuses to remove an image a container uses.
- The cost: what goes is downloaded and built again, so the first task after a pass takes longer. The periods are in days for that reason, and anything under a day is refused. `null` turns a rule off: the build cache is then only reported, untagged images only counted.
- `docker builder prune` cleans the builder `docker builder` points to: the daemon's own, unless someone selected another one with `docker buildx use`. `docker system df` counts the same one.
- The report says what each rule freed as Docker counts it (`freed_by_rule`; Grafana: `runner_cleanup_rule_freed_bytes{rule}`): for the build cache, what the prune said it reclaimed; for images, Docker's image total before the rule's first removal minus after its last, with a shared layer counted once. A rule that removed nothing says 0; a prune that failed or was cut short says nothing, since part of it may have gone. `runner_cleanup_freed_bytes` stays the change of free disk space over the whole pass.

Everything else is **reported**, never removed, for a person to clean by hand (for now, once a week): unlabelled containers and volumes, empty networks, unused tagged images by size, untagged images younger than `dangling_images_after` and anonymous volumes nobody can be tied to or not yet unused for `orphaned_volumes_after` (counts), and files beside the repositories in the workspace. A name that spells a task (`shop7173-mysql`) is shown as a hint, never as proof. `arceus-runner cleanup --last` shows the full report; Grafana counts it. With `llm: advise` Claude (no tools) adds a one-line opinion to each reported item.

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

`ARCEUS_REAL_DOCKER=1 pytest tests/test_cleanup.py` checks the rules against the VM's Docker with objects of its own; the age rules prune host-wide, so their check starts a throwaway Docker (`docker:29-dind`, privileged) and leaves the VM's images and build cache alone.
