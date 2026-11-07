# Testing the platform

Off by default (`qa.enabled: false`). With it on, the agent tests the whole platform in a headless browser by your instructions and writes a report per run. Nothing goes through the issue tracker: no ticket, no comment; what to do about a finding is yours to decide.

**The instructions.** One file, `qa.instructions` (default `~/arceus/data/qa/instructions.md`, next to the secrets): the platform's URL, its test accounts and what to test, in any shape. The `runner` phase copies [qa-instructions.example.md](../config/qa-instructions.example.md) there once and never overwrites it. The example is all HTML comments, which the agent never gets, so until you write the instructions the file counts as empty: `arceus-runner check` fails on it with `qa` on, and a run reports it had no instructions. The runner adds how to work and how to report. A run keeps the text it started with, so editing the file never changes a run under way.

**Starting a run.** `arceus-runner qa run` queues one and prints its number; `qa status` lists the latest runs; `qa stop [n]` stops a queued or running one (its report keeps what it found). With `qa.schedule` (`{days: [mon, …], at: "02:00"}`, in `schedule.timezone`) the runner queues runs by itself. A scheduled time is queued once, also across restarts; one missed by more than an hour is not made up, and one that comes while a run is queued or under way is skipped.

**How it runs.** A run is the Bulava task `qa-<n>`. It holds the shared workspace from start to end, and a queued run goes before new tickets. Every repository is put on a local branch `arceus-qa/<n>` at its default branch as last fetched, so the agent can name the file and line it suspects; it changes no code. A repository with uncommitted work (a ticket waiting on people) is left as it is and read with `git show`. Afterwards every repository goes back where it was. Past `qa.timeout` (8 h) the run is stopped and reported as unfinished.

**The report.** The agent writes `findings.json` and its screenshots; the runner validates them and writes `report.html` under `qa.reports`: what was checked, and each problem with its steps, the screenshot where it went wrong, expected against actual, how often, and the suspected cause linked to the code at the commit read. The outcome is one of:

- no problems found: the run finished, the report was read whole, and at least one thing was checked with nothing left unchecked;
- no problems in what was checked, with what could not be checked listed;
- could not test (the site down, the login refused);
- did not finish: nothing checked, everything skipped, a problem without a description, or a report the runner could not read; the report and Slack say why.

Reports are served on `127.0.0.1:3300` (`qa.port`): `ssh -L 3300:127.0.0.1:3300 <user>@<vm>`, then `http://localhost:3300/`. The list is read from the database on every visit, so queued and running runs show at once. Only reports and their screenshots are served, never the instructions.

**Slack and Grafana.** The channel hears when a run starts (by whom, or on schedule) and how it ended, with the agent's two or three lines and the report's link. On **Arceus · now** the agent shows as Testing, and the run is listed as `qa-<n>`.
