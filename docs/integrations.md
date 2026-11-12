# Trackers and code hosts

The runner's flow (`loop`, `delivery`, `review`, `screening`, `cleanup`) never talks to YouTrack or GitLab directly. It talks to two interfaces, defined in [`runner/src/arceus_runner/integrations.py`](../runner/src/arceus_runner/integrations.py):

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

The keys at the top of each section (`bot_user`, `allowed_users`, `tags`, `states`; `git_author`, `exclude_repos`, `branch_prefix`, `mr_label`) mean the same for every provider; the rest belong to the type.

## Adding a tracker or a code host

Jira, GitHub or anything else can be added without touching the runner's flow:

1. Write one module, using [`youtrack.py`](../runner/src/arceus_runner/youtrack.py) or [`gitlab.py`](../runner/src/arceus_runner/gitlab.py) as the example. It has three parts:
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
| Links to a file at a commit (`file_url`) | a test run's report names the suspected file and commit without a link |
