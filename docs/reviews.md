# Reviews

Two kinds of review besides Codex: Claude checks that Bulava runs before every Codex review, and a code review a person asks for on the ticket.

## Checks before Codex

After the build and tests pass and before Codex reviews, Bulava runs two Claude sessions side by side, a code review and a security review (engine/SERVER.md, "Independent checks before Codex"). They see only the diff of every repository with work in it, never the ticket or the agent's conversation, and change nothing. Findings at `min_severity` or above go back to the agent, which fixes each one or says why not; Codex then decides with the findings and the answers in front of it. The checks only advise: an empty list is not a pass, and the number of returns per task is fixed. A red build skips them.

```yaml
bulava:
  claude_checks:
    enabled: true
    kinds: [code, security]
    min_severity: high     # critical | high | medium | low
    rounds: 1              # returns to the agent per task, 1–5; a follow-up does not reset it
    timeout: 15m           # per check, 1m–30m
    required: true         # a check that did not run stops the task for a person; false: Codex alone
```

The review gate reads these from the environment of the engine's tmux server, not from a dispatch. So `setup.sh` writes them into `bulava-engine.service` (`arceus-runner engine-env` prints them from `config.yaml`) and restarts the engine when they changed and no task runs; otherwise restart it between tasks. `check` and `doctor` show `FAIL` on `config.claude_checks` while the running engine differs from `config.yaml`, or has no checks at all, so no task starts on other terms. The runner drops these names from `bulava.env`. A `config.yaml` written from an earlier example keeps its `required: false` until you change it.

- **Spent rounds** stop the checks for that task: fixes after MR comments and red CI then go to Codex alone, with the last reading.
- **A check that did not run** (time, a Claude limit, the network) shows as `gate.check failed` in the engine steps. With `required: true` the task fails before Codex, nothing is published, and `/continue` runs it again; with `false` the work goes on to Codex alone.
- **Cost.** Two more Claude sessions per review on the same subscription, up to `timeout` each, inside the review gate's hour. A diff over 400 000 characters is cut; the checks read the rest from the files.
- **Model and depth** are the task's `bulava.claude_model` and `claude_effort`; there is no setting of their own.
- **Not the slash commands.** The checks run Bulava's own prompts on a diff the engine builds across all repositories, not `/code-review` and `/security-review`.
- **Start handshake.** The engine rolls a start back when the new session's hooks do not confirm it in time (5 s in the engine), which fails the ticket. A cold Claude on a busy machine is slower, so the runner passes `bulava.start_handshake_wait` (30 s by default, 5 s–2 m).

## Code review on request

Off by default (`review_agent.enabled: false`); then `/review` is an ordinary comment to the agent. With it on, someone in `allowed_users` writes on a ticket in Code Review a comment starting with `/review` (`review_agent.command`), or one that mentions the tracker bot and then says `review` (`@agent-runner review, mostly the migration`). What follows is passed on as what to look at.

**The request.** Recognised before any other handler reads the comment, so it never reaches the agent that wrote the code. Written on an MR it starts nothing: reviews are asked for on the ticket, where the allowlist applies. Someone not on the list, a ticket outside Code Review, a review already under way, or `max_per_ticket` reached get a one-line answer and no review; refused requests do not count towards the limit. A taken request gets 👀 from the bot at once (journaled, so a tracker outage delays it but never doubles it). With Slack set up, the channel hears when a review starts and its result per MR.

**The run.** The review is a Bulava task of its own, `<KEY>.review-<n>`, never a follow-up of the author's session. Its text is the ticket whole (description and every comment, through the security screen; a text too long for the screen is held for a person, never shortened) plus the open MRs. Each MR's repository is checked out at the host's commit on a local branch `arceus-review/<KEY>-<n>`, readable but not writable: the reviewer builds and runs the tests, it changes nothing and asks nobody. Its verdict goes to `.arceus/reviews/<KEY>/<id>.json`: per MR, whether it meets the ticket, build and tests as seen, blockers, suggestions (at most `max_suggestions`) and changes for a person. A run that asks a question, passes `review_agent.timeout` or leaves no readable verdict ends as "could not be confirmed". It holds the shared workspace like a task; afterwards the runner stashes what it left, puts every repository back and deletes the review branch.

**What the runner approves.** The model advises; the runner decides. An MR is approved only when all of this holds:

- the reviewer found it ready: requirements met, build passed, tests passed (or the repository has none), no blocker;
- the build and tests satisfy the repository's rule in `review_agent.repos`, matched by path in the group (`services/api`, or a pattern such as `services/*`; the exact path wins, then the first pattern):
  - `build: none` / `tests: none`: not needed for this repository; what the reviewer did not run stops nothing, but a failure it saw still does;
  - `ci_build_jobs` / `ci_test_jobs` (`*` and `?` allowed): what the reviewer could not run itself counts as passed when, in the CI run of the reviewed commit, at least one job matches and every match succeeded;
  - no rule: both as the reviewer ran them;
- tests that also fail on the target branch without the change are reported as `failed_before` and ask for nothing;
- the host's newest CI run is for the reviewed commit and passed (`require_green_ci`; a running one is waited for);
- the MR still points at that commit and target branch, and the ticket is still in Code Review with the text the review read.

Then the approver approves exactly that commit, with a note "ready to merge". The note also names, for a person to look at before merge:

- changed files that match `attention_paths` (by default auth, permissions, crypto, `.env`; ordinary tests and docs are left out);
- what the reviewer says a person has to decide: a security boundary changed in substance, a CI check removed or weakened, wider privileges or exposure, a migration that drops or rewrites existing data, behaviour other systems rely on changed beyond the ticket, a hard-to-reverse product decision the ticket left open.

What only the people who release it need to know (an environment variable, a requested runtime upgrade, a new dependency, an additive schema change) goes under "For whoever deploys it" and calls nobody. Files under `review_hint_paths` (CI, Docker, Makefile, dependencies, settings, migrations) are pointed out to the reviewer to read with care. Otherwise the note says what has to change or why the review could not confirm it, and an earlier approval is taken back. An approval is also taken back, once and with a reason on the MR, when the MR gets new commits, changes target, or the ticket's description or files change (a comment does not). A final review stands for the same commits only under the same `review_agent.repos` rules. The ticket gets one summary with a line per MR.

**A merge the review did not let through.** When an MR a review judged is merged, the runner compares the merge with the newest finished review of it. If that review asked for changes, or the MR was merged at commits the review never read, the ticket gets one comment that says which, with the blockers the review found, and Slack is told (`review_merged_unchecked` in the event log). A merge at the reviewed commit, or at a rebase or squash GitLab records with the same patch id, is left alone; on a GitLab that keeps no patch ids any other head counts as unreviewed. A review that could not confirm the MR, then merged as it was, is the people's decision and is not reported. MRs no review judged are not looked at, and a merge while a review runs is left to that review's own result. The comment is posted once per MR, also after a tracker outage or a restart. A judged MR is followed until it is merged or closed even after the runner lets go of its ticket (moved back to In Progress for the fixes, or the task ended), so a merge weeks later is still checked.

**The approver.** GitLab does not let the bot approve MRs it opened, so approvals come from a second account, `code_host.approver`, with its own token in `secrets.env` (`api` scope, at least Developer in the group); `check` verifies the token is its. It also signs the review's notes. Without an approver the review still runs and its notes say it could not approve. A refusal by GitLab is reported as it is, never shown as an approval.

**The fix loop.** When changes are needed, the runner sends the blockers, a failed build or tests and unmet requirements (not the suggestions) to the agent that wrote the code as a round of its own, counted against `limits.max_review_rounds`; the note says "Sent to the agent to fix, round 1 of 3". Once the agent has pushed new commits and the ticket is back in Code Review, the runner asks for the same review again in the first requester's name. This repeats until approval or until `review_agent.fix_rounds` (3) is used up; a round with no code change also ends it. `fix_rounds: 0` turns the loop off. Asking again for commits that already have a final review returns that review.

**Merge requests people wrote.** On any ticket in Code Review the agent never worked on, a request reviews the merge requests of the group that the ticket links to, in its description or comments, each at the head GitLab has (forks included). The summary lists links it left out and why (a GitHub PR, another group, a second open MR of the same repository). The reviewer is told people wrote the code; there is no fix loop, and the note asks the author to change it and write `/review` again. Such a ticket is a task of kind `review` that nothing else touches; the runner lets go of it when it leaves Code Review or has no open MR, and requests older than `limits.continue_window` are ignored. Queued for the agent later, it becomes an ordinary task.

**Switched off** while a review runs: the run finishes, nothing is posted or approved, and the ticket says so.
