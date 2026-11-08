# Architecture reviews

Off by default (`architect.enabled: false`). Agents write most of the code, one ticket at a time, and every change is reviewed on its own. Nobody looks at how the changes fit together. With this on, an agent does that on command or once a week. It reads what changed since the last review, and how those changes meet the code around them, and reports the few things worth a person's decision. It does not try to polish every function. Nothing goes through the issue tracker, and the agent changes no code.

**What it looks for.** The runner's own criteria, given to every review (`CRITERIA` in `architect.py`, shown at the bottom of each report):

1. How the parts fit together: two mechanisms for one job, parallel abstractions, a contract one side changed and the other did not, a module reaching into another's insides. Two features can each look fine and still clash. A review of one change cannot catch that, so this criterion matters most.
2. Code written to look busy: dead code, pass-through wrappers, checks for what cannot happen, comments that retell the code, errors caught and dropped, a migration with both the old path and the new one alive.
3. Tests that prove little: a test that mocks what it tests, one that only checks nothing raised, assertions that restate the implementation, a main path no test drives. Coverage numbers do not count.
4. Modules worth deepening, as in [this survey](https://www.aihero.dev/skills-improve-codebase-architecture). A module is named only when moving its complexity behind a smaller interface would help its callers.
5. Drift from the team's rules: the project's notes, a repository's `AGENTS.md`, its docs or decision records say one thing and the code now does another.

Style, naming and anything a linter finds are left out. A finding that breaks a rule written down somewhere is a *rule broken*, and it quotes the rule. Anything else is a *proposal*, however sure the agent is. There are at most `max_findings` (7) per review, and none is a fine result.

**The team's instructions** (`architect.instructions`, by default `~/arceus/data/architect/instructions.md`) are optional. Use them for what to look at harder, what to leave alone, and what the team does or does not count as a problem. The `runner` phase copies [architect-instructions.example.md](../config/architect-instructions.example.md) there once, as comments only. A run with nothing written works by the criteria above and reports in `bulava.report_language`. A run with instructions reports in their language. `arceus-runner check` says which a run will get, and it fails only when the file cannot be read. A run keeps the text it started with.

**What a review reads.** Only the repositories that changed since the last review that read them, each from that commit to its default branch now:

- The first review of a repository looks back `first_look` (30 days). A repository with no commits in that window is left out.
- A repository counts as read only when the agent says it read it. One it skipped or did not mention keeps its unread changes, and the next review reads them from where they start, however much later it runs.
- `repos` limits which repositories of the group are reviewed at all.
- `skip` names what is never reviewed: generated code, vendored copies, dependencies kept in the group.
- `not_ours` names code the team builds on but does not own, such as an upstream platform or a fork kept in step with it. The agent reads that code for context and never proposes changing it. When the team's own change edits it where an extension point, a setting, a plugin or an override would have done, that is a finding, because the next upgrade will undo it.
- Patterns are paths in the group (`platform/*`). A pattern with a path after a colon names paths inside a repository: `core:vendor/**`, or `*:*.min.js` for a file name anywhere.
- When nothing changed, the run ends without the agent, as *nothing changed since run N*. That costs no Claude time.

Unchanged repositories stay in the workspace for context: a finding usually lives where a change meets older code.

**Earlier findings.** Every finding is kept in the runner's database with a number, and every review is told the ones still open. For each one in code it read, the agent says whether it is still there or fixed. A finding is marked *fixed* only when a later review checked and found it gone, never because the agent stopped mentioning it. A finding that continues an earlier one keeps that number. Findings of a run that did not finish or was stopped are shown in its report but not kept. Such a run also does not move the starting point: the next review starts from the last one that finished.

**Codex's second opinion.** Before the report goes out, Codex reads every finding against the code at the commits the agent read and says whether it holds, with a reason. The runner runs it itself, while the workspace is still switched:

- `codex exec` runs read-only, without the web and without the user's Codex config. Its environment has `HOME`, `USER`, `LANG` and `PATH` only, nothing of the runner's.
- The report and Slack call this the *second check* and never name the tool, which may change: each finding is *confirmed*, *doubtful* or *not confirmed on second check*, and a finding without a verdict is counted apart, never as not confirmed. A finding that is not confirmed stays in the report, marked as such. You decide.
- When Codex fails, times out (`second_opinion_timeout`, 20 min) or is out of quota, the report says the second check *did not run* and why, in words that do not name the tool; the tool's own message is in the runner's events, `arch_second_opinion_failed`. A run stopped while its findings were being checked says the check was *skipped*. That is never counted as agreement. A runner crash during the call repeats it once.
- Codex runs as the same user as everything else, so its read-only sandbox does not stop it from reading files. That is the same exposure as Bulava's own Codex reviews. Its prompt treats the code as data, and its answers reach only the report.
- `second_opinion: false` turns it off.

**Starting a run.** `arceus-runner arch run` queues one and prints its number. It works whether the schedule is set or not. `arch status` lists the latest runs, and `arch stop [n]` stops a queued or running one. With `architect.schedule` (`{days: [sun], at: "03:00"}`, in `schedule.timezone`) the runner queues runs by itself, under the same rules as test runs: a scheduled time is queued once, one missed by more than an hour is not made up, and a time that comes while a review is queued or under way is skipped.

**How it runs.** A run is the Bulava task `arch-<n>` and holds the shared workspace exactly like a test run of the platform ([qa.md](qa.md)). Every repository goes on a local branch `arceus-arch/<n>` at its default branch. Repositories with uncommitted work are read with `git show`. Tickets wait while it runs, and everything is put back afterwards. Test runs and reviews take turns, and a queued one goes before new tickets, so schedule them for nights without tickets, on different days. Past `timeout` (4 h) a run is stopped and reported as unfinished.

**The report and decisions.** The agent writes `findings.json`, and the runner reads it defensively. Every file and line a finding names is checked against the commit the agent read. A finding none of whose references exist is left out, and the report says so. Reports are served on `127.0.0.1:3301` (`architect.port`): `ssh -L 3301:127.0.0.1:3301 <user>@<vm>`, then `http://localhost:3301/`.

- The list shows the runs, and a count of open, decided and fixed findings.
- A run's page has its findings with their second check, what it found fixed, what it did not check again, and what it read.
- `/findings/` lists every finding kept.

Each open finding has two buttons, **Accept the risk** and **False positive**, and both need a reason. The next review is told the decision with that reason and does not raise the finding again unless the code changes in a way that makes the reason wrong. **Take the decision back** reopens a finding. Pages are built from the database on every visit. A decision is accepted only from the server's own pages: the form carries a token made when the runner started, and requests from other sites are refused. After a runner restart, reload the page before deciding. `report.html` in a run's folder is the report as the run left it, without the buttons.

**Slack and Grafana.** The channel hears when a review starts and how it ended: the agent's short summary, how many findings are new, still open and fixed, how many the second check confirmed, and the report's link. Who typed `arch run` on the server is not said: it is always the runner's user. File paths, names and commits in the agent's texts show as code, in Slack and on the pages. On **Arceus · now** the agent shows as *Reviewing architecture*, and the run is listed as `arch-<n>`.

**Cost.** One Claude session per review, up to `timeout`, plus one Codex call. Both come from the same subscriptions as tickets.
