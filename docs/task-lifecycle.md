# How a task goes in `live`

## Taking a ticket

1. Someone in `allowed_users` puts the `agent` tag on a ticket in the queued state. The tag's author is read from the ticket's history; a tag from anyone else gets one comment and is not taken.
2. The runner takes one task at a time. The ticket as it is at that moment (description, people's comments oldest first with author and time, files attached to it and to its comments) first goes through the [security screen](security.md); a held ticket is not claimed and its workspace is not touched.
3. **The workspace.** Tickets do not name a repository: the agent gets every repository of the code host's group (`code_host.group` on GitLab) except `exclude_repos`. The runner clones or fetches each, runs `night-shift trust` and `mcp-decide skip` in it once, creates the task branch from the default branch in every one and hands them all to the agent as writable (`SUPERVISOR_EXTRA_DIRS_FILE`). A repository with uncommitted changes stops the start.
   - The session starts in the workspace folder itself. Bulava needs a git project, so the folder is a small local repository of the runner's: a `.gitignore` that ignores every clone, and an `AGENTS.md` (read through `CLAUDE.md`) that says what the folder is and lists the repositories, followed by the project's notes ([agent-instructions.md](agent-instructions.md)). It has no remote and is never published; the next task starts from the runner's version again.
4. The ticket gets In Progress, the bot as assignee and a comment, and `dispatch.sh` starts the work on branch `agent/<KEY>-<slug>`. Follow-ups get the same repositories.

## Following the task

The task is reconciled with `night-shift task show` every `poll_local`:

- **`waiting_review` with `review: passed`.** MRs are opened only where the agent changed something (`reviewed_sha != base_sha`). Each repository in `receipt.repos` is checked first: branch, clean tree, `HEAD == reviewed_sha`, part of the group, `base_sha` on the default branch (otherwise the commits below it were never reviewed). If any check fails, nothing is pushed. Otherwise `reviewed_sha:refs/heads/<branch>` is pushed without force (unless the host already has exactly that commit) and the remote SHA compared afterwards. A branch that already holds commits on top of the reviewed one is left alone, with no MR and a note on the ticket. Then the MR is created or reused, the ticket gets the links and moves to Code Review.
- **`needs_input` / `blocked`.** The question and its options go to the ticket, mentioning the tag's author, with the `agent-needs-input` tag. A `blocked` run without a question of its own sends its whole summary instead. Nothing the agent wrote is cut: a long text goes as several comments in order ("Part 1 of 3"), and the screenshots it left go with the last one. The same holds for the result of a run with no code changes and for the details of a failure. The first comment after it from someone in `allowed_users` goes to the agent (`worker-send --answers`). Without an answer: a reminder after `needs_input_reminder`, failure after `needs_input_fail_after`.
- **`done`.** The ticket gets a comment with the result; nothing to push.
- **`failed`, `debt`, a dirty tree, or no verdict for longer than `attempt_timeout`.** The ticket gets the reason and the `agent-failed` tag. What the attempt left uncommitted becomes a `WIP [KEY]` commit on the task branch, reviewed with the rest before anything is published. While Bulava reads a usage limit as used up, the attempt's clock stands still.
- **`running` with no live session** (for example after a reboot): continued with `worker-send --relation continue`.

**`/continue`.** A comment from someone in `allowed_users` that starts with `/continue`, within `continue_window` (14 days), resumes a failed task: a new attempt with the same goal, branches and commits, plus the comment's text (`worker-send --relation continue`). It is journaled, sent once, goes before new tickets and survives a restart; the bot puts 👀 on the comment when it is taken. The `agent-failed` tag comes off, the ticket returns to In Progress, and the review and CI budgets start again. It also resumes a task stopped because people closed every MR. If Bulava no longer has the task, it starts again from the ticket on the same branches. Other comments on a failed ticket wake nothing.

**Research tickets** carry `agent-research` (`tracker.tags.research`) next to `agent`; who may start one is still decided by `agent`. The agent changes nothing and writes its answer to `.arceus/notes/<KEY>/answer-<run>.md`, a new file per run. The runner keeps a copy in `data/tasks/<KEY>/` and posts it on the ticket whole, in several comments when long; no push, no MR. A missing or unreadable answer (a symlink, over 512 KB) is asked for once, then the task fails. The ticket then moves to `tracker.states.answered` (Code Review; `null` leaves it In Progress). A `/continue` with a follow-up question gets a new answer; to have the answer implemented, queue a new ticket without the research tag.

## After the MR

On each poll of the code host the runner watches every MR of the task for:

- review notes by people that mention the bot (`@` and `code_host.bot_user`), in every discussion, late replies in old threads included. Anyone with access to the repository may ask the agent this way; the text goes through the security screen. Notes without the mention, the bot's own and the host's system notes wake nothing;
- the head pipeline: a failure of the commit the runner published is sent with the failed jobs and the end of their logs; a running, cancelled or older pipeline is not, and one that turns green or is replaced before it is sent is dropped;
- a conflict the code host has confirmed (GitLab `detailed_merge_status: conflict`; `checking` does not count);
- ticket comments by people in `allowed_users`, except `/review` and `/security-allow` / `/security-reject`, which are the runner's.

Signals are stored once. When `review_debounce` has passed since the newest one, they go to the agent as one follow-up of the same Bulava task (`worker-send --relation continue --source mr|ci|youtrack`), the ticket returns to In Progress, and Codex reviews the fix against the original goal. Fixes for open MRs go before new tickets.

- **Threads.** Each thread goes to the agent whole, the new notes marked. The agent decides per thread whether it asks for a code change or only an answer, and writes one answer per thread to `.arceus/replies/<ticket key>/<round>.json`; for the reasons behind its choices it reads its own `.arceus/notes/<ticket key>/decisions.md`. The runner pushes the new reviewed commit (fast-forward, never force), rewrites the MR description and posts each answer in its thread, with the commit where code changed. Threads are left for people to resolve. A round that changed no code only posts the answers; a thread the agent left unanswered gets its overall explanation.
- **People's commits.** Before each follow-up every repository is put back on the task branch and caught up with what people pushed to the MR; a push while the agent is fixing is merged in on the next round.
- **Closing.** The task is **done** when all its MRs are merged; the ticket then moves to `tracker.states.merged` (`Testing`; `null` leaves it in Code Review; `check` warns when the board has no such state) unless a person already moved it. An MR a person closes while others are open goes to the agent, which takes that repository's changes out, and the MR leaves the task. When people close every MR and merge none, the task stops (`stopped`, no failure tag) and `/continue` can pick it up, opening a new MR. Some merged and the rest closed is done. A ticket moved past Code Review by hand stops the task for good.
- **Budgets**, kept across restarts: `max_review_rounds` (rounds that published code), `max_review_answers` (rounds answered without a code change), `max_ci_fix_attempts` (CI repairs in a row, reset by a green pipeline). When one runs out the task fails with one comment saying why.

## Who opens and describes the MR

The runner. Bulava's server profile refuses `git push` inside a run, so the agent only commits. The runner writes the code host bot's credentials (`CodeHost.git_auth`) to `~/.git-credentials` for that host only, so plain `git fetch` and `pull` work for the agent too and the token is never in a URL, a command line or `.git/config`. If the engine later lets the agent push, the runner still checks after the review that the host holds exactly the reviewed commit.

The MR description has the ticket with a link; **What was done**, the agent's own run summary; **Commits** since the base; **Checks**, which commit passed review (no model names or round counters); and the other MRs of the same task. It is rewritten after every round to match the branch.

## Doing nothing twice

Comments go through a journal in SQLite and carry an `[arceus:…]` marker; dispatches and follow-ups have stable message ids; a push is compared with the remote first and an MR is looked up before it is created. After a crash the next tick finishes what is missing and duplicates nothing.
