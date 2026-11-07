# What the agent is told

Bulava writes the agent's own rules. Arceus adds three things.

**Server notes** ([config/agent-notes.md](../config/agent-notes.md)): how to write the run summary, leave visual proof, register checks, resolve conflicts, keep notes on decisions and leave nothing behind. The `agent_notes` phase of `setup.sh` puts the file between `arceus:agent-notes` markers in the run user's `~/.claude/CLAUDE.md`, which every Claude Code session reads, the engine's workers included; the rest of that file is left alone. `--check` reports `todo` while the block is missing or differs from the file, so an edit reaches the server with `sudo ./setup.sh --phase agent_notes`. Every session of every task reads these notes whole: keep them short, one rule with one reason each.

**Reading screenshots** (phase `agent_reports`): `Read(//<home>/.claude/supervisor/reports/**)` in `~/.claude/settings.json`. The engine's tools save screenshots there without opening the folder to the agent, and on a server nobody can approve the read.

**The project's notes** (`paths.project_notes`, by default `~/arceus/data/project-notes.md`) belong to the project, not to Arceus: what the product is, the team's words, how the repositories fit together, rules the code does not show, decisions already made. The `runner` phase copies [project-notes.example.md](../config/project-notes.example.md) there once, as comments only; people on the server keep it up to date. The agent never changes it: a lesson worth keeping is a person's edit.

- At the start of every task the runner appends the file, HTML comments left out, to the workspace's `AGENTS.md` under `## Project notes`, below a line saying the server's notes win where the two disagree. Claude reads it through `CLAUDE.md` (`@AGENTS.md`), Codex directly, so nothing is read twice.
- The file is read once per task and kept as `~/arceus/data/tasks/<KEY>/project-notes.md` (event `project_notes_pinned`). Follow-ups, review rounds, a restart after `/continue` and a code review of the task all use that copy, so an edit reaches the next task and leaves running ones alone.
- Without `paths.project_notes`, or with comments only, `AGENTS.md` has no notes. An older `config.yaml` lacks the line: add `project_notes: ~/arceus/data/project-notes.md` under `paths:`.
- A file that is set but missing, unreadable or over 16 KB without comments is never skipped or cut: a task without a copy yet fails with the reason, and `arceus-runner check` shows `FAIL config.project_notes`. Fix the file and `/continue` the ticket.
- Rules of one repository belong in that repository's own `AGENTS.md`; secrets never go in the notes.

**File modes.** The runner runs with umask 077 (its tokens and database); the git and engine commands it starts use 022, so clones are 644/755 and the dev stack's containers can read them. Each task start restores the modes git records for tracked files.
