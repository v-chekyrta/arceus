# The project's assistant

A colleague for the developer whose factory it is, beside the factory: a web chat behind the console at `/assistant/` and,
when the project wants it, a member of its Slack. It reads the project's code, tickets and channels, advises on client
requests, drafts YouTrack tickets for the factory and follows them. Its code is `assistant/` in this repository; its
settings are the `assistant:` section of the project's `config.yaml`; its tokens are in `assistant.env` beside it.

## How it runs

```
browser ─https─▶ console nginx ─ /assistant/ ─▶ auth_request /_console_auth (console sign-in)
                                    │                └─ X-Arceus-User → X-Assistant-User
                                    └─▶ 127.0.0.1:<assistant.port>  arceus-assistant@<run user> (systemd)
                                              └─▶ claude -p … per turn, in <assistant.home>/mirror (read-only tools)
```

- **Who gets in:** nginx asks the console who is signed in and passes the email; the server also checks it against
  `assistant.allowed_users`. The console may be shared with other projects, so the list is required while the web
  chat is on.
- **What the assistant can touch:** only `Read`, `Grep`, `Glob` inside `~/assistant/mirror` (clones of the project's
  repositories, reset to their default branch) and, for one turn, the upload folder of that one conversation.
  `--permission-mode dontAsk` refuses everything else; no Bash, no writes, no MCP servers but the ones in its own
  config, none of the run user's settings, hooks, memory or CLAUDE.md files (`CLAUDE_CODE_DISABLE_CLAUDE_MDS`).
  `~/assistant/data` (the database) is denied as a whole, which is why uploads live beside it, not in it.
- **Conversations:** SQLite in `~/assistant/data/assistant.db`, shared by everyone allowed in. One Claude session per
  conversation, resumed every turn; a lost session is rebuilt from the stored history. A reply cut off by a restart
  is marked so, with «Спробувати ще раз».
- **Files:** up to 10 per message, 50 MB each, in `~/assistant/uploads/<conversation>/`. Pictures are opened by Claude
  itself; voice and video are transcribed on the VM (faster-whisper `medium`, models in `~/assistant/models`, decoded
  by ffmpeg) and videos also give a few frames; PDF (pdftotext), Word, Excel, PowerPoint, e-mail, HTML and text become
  text; a zip shows its listing; anything else is named with its type and size. Only images, PDF, audio and video are
  shown inline; everything else, HTML and SVG included, is served as a download.
- **Tickets:** the assistant searches and reads the project's YouTrack tickets and proposes a draft; only a person's
  «Створити тікет» makes it, from the bot's account. The token is `YOUTRACK_TOKEN` in `assistant.env` (600),
  read by the server and its tools process, denied to Claude. Every ticket ends with who asked for it and the draft's
  mark (`fa-<conversation>-<draft>`): a retry after a lost answer or a restart looks for that mark first and returns
  the ticket already made. The ticket goes on the project's board (`board`), in Backlog.
- **Into work is the manager's:** the runner takes a ticket only when it is in To Do and its `agent` tag was put on by
  someone in its `allowed_users` (by the ticket's history), never by its own bot (`Agent01`, so `queue_tag` stays `""`).
  The assistant does not queue tickets. The card under a made ticket reminds the manager of both steps and checks
  them against the ticket itself, reading the runner's own config (`factory.config`): which step is done, whose tag
  the factory accepts, and, once the runner moves the ticket on from To Do, that the factory took it.
- **Requirements first:** the agent that does the ticket cannot ask anyone and cannot fetch files, texts or decisions
  that are not in it. The prompt has the assistant find out what changes, where, an example, the materials (or the
  decision to do without them), what counts as done and who decided, before it proposes a ticket; `propose_ticket`
  refuses a draft while its `open_questions` is not empty.

- **Tickets through Night Shift:** the chat stays fast (`claude -p`); the ticket is written by the assistant's own
  Night Shift engine. `propose_ticket` stores her draft as a brief in state `preparing`; a background loop sends it,
  with the conversation, to the engine as a task (one at a time; the task id is the draft's mark, so a restart never
  makes a second one). A worker writes `artifacts/<task>.md`, checking every claim in the code; Codex reviews it as a
  research result. The ticket then replaces the brief: `checked: passed`, or `notes` with Codex's remaining objections
  under it. When the engine cannot (no sign-in, Codex away, nothing written, session lost, over `timeout`) her brief
  stays, `checked: unchecked`, with the reason. Measured: about 2 minutes; Codex caught decisions she had attributed to
  the manager. The engine runs as its own Linux user (`ENGINE_USER`) that cannot read the assistant's database, tokens
  or the factory's secrets; it has a copy of the code mirror that root refreshes before every ticket. The assistant
  reaches it only through `/usr/local/bin/fa-ticket-engine` (`sudo -n`; `assistant.ticket_engine.bridge`). Setup:
  `ENGINE_USER=… PROJECT=… ASSISTANT_NAME=… ASSISTANT_USER=… MIRROR=… deploy/ticket-engine/setup.sh` (the engine user,
  its `engine.conf` and its unit `bulava-engine-<ENGINE_USER>` are made from the templates there), then sign the engine
  user in once: Claude — `claude setup-token` on a machine with a browser, the token into `~<ENGINE_USER>/engine.env`
  as `CLAUDE_CODE_OAUTH_TOKEN=…`; Codex — `sudo -u <ENGINE_USER> -H codex login --device-auth`; then
  `sudo systemctl restart bulava-engine-<ENGINE_USER>`. `fa-ticket-engine status` shows what is missing. A second assistant gets a desk of its own on the same engine user: `DESK=<name>` as well makes
  `/usr/local/bin/fa-ticket-engine-<name>` with `/etc/fa-ticket-engine-<name>.conf`, a workspace of its own and a
  sudoers rule for that bridge alone. `deploy/ticket-engine/probe-links.sh [<bridge>]` proves the bridge never follows
  the engine user's links with root's rights. The bridge reads the caller's task file and brief as `ASSISTANT_USER`, never as root.

- **Slack:** the same assistant answers in Slack (`assistant/slack.py`, `slack:` in the config; without it there is
  no Slack). Socket Mode: the server opens the connection, nothing listens on the internet. A direct message is one
  ongoing conversation that only its person sees in the web chat; an @mention in a channel opens a conversation for
  the thread under it, and she keeps answering in that thread without being mentioned again. She answers any full member
  of the workspace (not guests, not people from other companies; `allowed_users` is for the web chat), never in channels shared with
  another company, nor in a channel she cannot vouch for. Before anything of hers goes into a channel she asks Slack afresh who is in
  it (`conversations.info`, which needs `channels:read`/`groups:read`); not known means held, not posted. Anyone of
  the workspace may talk to her; tickets are proposed and made only for `ticket_creators`, judged by the messages the
  turn answers (the server hands their ids to her tools), and `call_expert` sends the `expert` a Slack message. Everyone else gets a note only they see. Every message is written down before Slack is told it
  arrived, so a resend or a restart never loses or repeats it; messages sent while she is answering are answered
  together. 👀 goes on the message at once and comes off with her answer. Ticket drafts come as cards with
  «Створити тікет» / «Відкласти»; the button checks who pressed it and makes the ticket once. Conversations from Slack
  are read-only in the web chat. Tokens: `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN` in `assistant.env` (600), read by
  the server only, never put in its environment, denied to Claude.

- **Memory:** every DM, thread and web conversation keeps its own Claude session (people, privacy and replies stay
  apart), and the project is shared through her notes: `remember`, `memory_search`, `forget` (SQLite, server side).
  A note written in someone's DM is readable only in that DM; one written in a project channel's thread or the web
  chat, in every conversation of the project; one from any other channel, only there. A new session starts with her
  newest notes (bounded). Which notes were in front of her in which turn is recorded (`note_exposures`): when a note
  is taken back (by her, a person, an edit or deletion of its Slack source, or the channel becoming shared), every
  conversation that saw it starts a new session rebuilt from its stored history without the replies she wrote with it;
  a lost session is rebuilt from the latest part of the conversation only. Notes that depend on a channel are usable
  only while Slack last said the channel is ours alone (`channel_status`); search folds case for any alphabet.
  Notes grant nothing: rights are checked in code.
  She also reads the project's channels (`slack.read_channels`) into `channel_log` (`channel_read`): every message,
  edit and deletion, written before Slack hears it arrived; history page by page in a fixed window with its cursor kept (`channel_backfill`), so a run cut short goes on from
  the page it stopped at and live messages never move it (rate limits waited out); a deleted
  message stays deleted and notes made from it go too; a channel shared with another company is not read and what was
  read is purged. Whether a channel is ours comes from Slack (the event, or `conversations.info` with `channels:read`/`groups:read`);
  when Slack will not say, a channel the owner listed in `slack.internal_channels` counts as ours, and once Slack says a
  channel is shared she stays out of it whatever the list says.

- **Channels named by the owner:** `slack.listen_channels` are read like project channels and never spoken in, by
  anything she does (no answer to a mention, no reaction, no note), whoever Slack says is there: a channel with the
  client. `slack.partner_channels` are shared with another company on purpose: she reads and answers there, and
  their people may talk to her (tickets stay for `ticket_creators`). Neither is purged when Slack says it is shared;
  every other channel keeps the rules above. `slack.history_days` (120) is how far back channels are read and
  searched (`channel_read` pages back with `before`); raising it makes the next start read the older part. A reply
  written inside the window under a thread started before it is not found by the history read (Slack lists threads by
  their first message); live events still bring such replies in.

- **Asking the expert for what she lacks:** `request_capability` (a Drive document she cannot open, a missing access or
  tool) sends the `expert` one Slack DM per need, with the project, what is needed, what for, the link and the error
  with tokens masked; the same need again only counts (`capability_requests`), and again after a week. YouTrack
  refusing the bot (401/403) files it by itself. Unlike `call_expert` it opens no conversation to him.

- **Persona and the factory in the prompt:** `assistant/assistant/prompt.md` is shared; who she or he is comes from
  `assistant.persona_file` (the project's own, in `assistant.home`) and `assistant.gender` picks the prompt's
  `{сама|сам}` forms. Its «Фабрика» section is the runner's own config in words (`FactoryPolicy.guide`, read from the
  same `config.yaml`): how to queue a ticket and who may, research, questions, MR review and the GitLab bot to mention,
  `/continue`, platform tags. `assistant.knowledge_file`, when set, is appended as what is known about the project.

## Settings

The `assistant:` section of `config.yaml` (the example is in `config/config.example.yaml`). It is checked by the
runner's own code, like every other section: an unknown key is refused, and so is a token put there.

| Key | What it is |
|---|---|
| `enabled` | the assistant runs (setup starts `arceus-assistant@<run user>`; off: setup stops it) |
| `project`, `name`, `gender`, `intro` | the project as people say it, who she or he is, the empty chat's first line |
| `persona_file`, `knowledge_file` | character and legend; what is known about the project (appended to the prompt) |
| `home` | her folder: `data/` (the conversations), `uploads/`, `mirror/` (the code she reads), `models/` (voice) |
| `secrets` | her tokens' file, `~/arceus/data/assistant.env` (never the runner's `secrets.env`) |
| `port`, `web` | her port on 127.0.0.1 (another project's assistant on the machine needs another); the web chat at `/assistant/` for the console's project |
| `allowed_users` | console accounts (e-mail) let into the web chat |
| `ticket_creators`, `expert`, `expert_name` | who may have tickets made; whom she calls for what is past her (e-mail), and how she names that person to others («покликала …»: the accusative) |
| `provider`, `auth` | what writes her replies (`claude` or `codex`) and who pays (`subscription`: the factory's own sign-in, the default; `api_key`: the key in `assistant.env`); see «Who pays» |
| `model` (Claude), `codex_model` (Codex), `effort`, `reply_timeout`, `voice_model`, `claude_bin`, `codex_bin` | how she runs |
| `youtrack` | where her tickets are made: `project`, `board`, `queue_tag`, states, types (the tracker's own address) |
| `ticket_engine` | `bridge` and `timeout` of her Night Shift desk (see «Tickets through Night Shift») |
| `slack` | channels she reads, listens to, answers in with a partner; `call_names`; who may have tickets from Slack |

**Tokens** are `assistant.env` (600, the run user's), one per line, read afresh at every use:

```
SLACK_APP_TOKEN=xapp-…      Socket Mode (connections:write)
SLACK_BOT_TOKEN=xoxb-…      the bot's Web API calls
YOUTRACK_TOKEN=perm:…       the account tickets are made from
ANTHROPIC_API_KEY=sk-ant-…  provider claude, auth api_key: what her replies are billed to
OPENAI_API_KEY=sk-…         provider codex, auth api_key: the same, on OpenAI
```

**The Slack app**: `/opt/arceus-assistant/venv/bin/arceus-assistant slack-manifest` (as the run user) prints its
manifest with this project's assistant in it; paste it at api.slack.com → Create New App → From a manifest, install
it to the workspace, then put its app-level token (`connections:write`) and bot token into `assistant.env`.

## Who pays

One developer, one factory, one assistant: she talks with that developer. What writes her replies and who pays for
it is two settings, four ways:

| `provider` | `auth` | Her replies run on | Billed to |
|---|---|---|---|
| `claude` (default) | `subscription` (default) | Claude Code, the factory's own Claude sign-in | the factory owner's Claude subscription, as before |
| `codex` | `subscription` | Codex, the factory's own Codex sign-in (ChatGPT) | the factory owner's ChatGPT subscription |
| `claude` | `api_key` | Claude Code, `ANTHROPIC_API_KEY` from `assistant.env` | the key's owner (Anthropic API) |
| `codex` | `api_key` | Codex, `OPENAI_API_KEY` from `assistant.env` | the key's owner (OpenAI API) |

Each reply runs with an environment built from nothing: with a subscription, the run user's home (and a
`claude setup-token` token the service was given); with a key, that key alone and a home of its own with no sign-in
in it (`<home>/data/claude` or `<home>/data/codex`). A missing key starts nothing: she says the key is not set up; a
refused one is said as such. Nothing falls back to the subscription. Moving between them keeps the conversations:
a session the new one does not have is rebuilt from the stored history (Claude's transcripts are carried over once).

Tickets and «розберусь» always go to her Night Shift desk (`ticket_engine`), on the factory owner's subscription,
whichever way her replies are paid.

**On Codex** (`assistant/assistant/codex_brain.py`) she has no shell: Codex reads files only through a shell, and its
read-only sandbox could read the whole disk, so `shell_tool` and everything else that reaches out (the browser,
computer use, image generation, apps, plugins, skills, sub-agents, hooks, web search) is off for every turn, and the
run user's own Codex settings are not loaded (`--ignore-user-config`, `--ignore-rules`). She reads through her own
tools instead (`read_file`, `list_dir`, `search_code`, `assistant/assistant/file_tools.py`), which resolve every path,
links included, and refuse anything outside the code mirror and this conversation's own uploads. `codex_model`
(`gpt-6.1-terra`) and `effort` pick the model. An OpenAI key of an organization enrolled in ChatGPT's monthly API
credits cannot be used for Codex: she says so.

## Install

`sudo ./setup.sh` (its `assistant` phase), once `assistant.enabled` is on:

- installs the code into `/opt/arceus-assistant` (its own venv, the runner's package beside it, faster-whisper);
- writes `arceus-assistant@.service` and, per project, a drop-in that names its `config.yaml`; enables and starts
  `arceus-assistant@<run user>`, and restarts it when its code changes;
- refreshes the code mirror every hour (`arceus-assistant-mirror@<run user>.timer`: `deploy/mirror.sh` from the
  factory's workspace clones into `<home>/mirror`);
- for the console's project with `web: true`, adds `/assistant/` to the console's nginx;
- refuses to start it while the same user's bot from before Arceus still runs: two connections to one Slack app
  split its events between them.

The machine also needs ffmpeg and poppler-utils (setup's packages phase has them). The ticket desk is set up apart,
as root: `assistant/deploy/ticket-engine/setup.sh`.

## Carrying over an assistant set up before Arceus

The assistant used to live in `~/assistant` with its own config, `secrets/` and unit (`factory-assistant`). Its
conversations stay where they are; only its settings and tokens move. As the run user:

```bash
/opt/arceus-runner/venv/bin/arceus-runner assistant import ~/assistant/config.yaml            # says what it would write
/opt/arceus-runner/venv/bin/arceus-runner assistant import ~/assistant/config.yaml --apply    # --port 8191 --no-web for a Slack-only one
```

It appends the section to `config.yaml` (every other line stays byte for byte; a file that already has one is left
alone), adds the tokens to `assistant.env` (a name it already has is kept), and copies a persona that was inside the
old code folder into `home`. Then: `sudo systemctl disable --now factory-assistant`, take the old `/assistant/`
block out of `CONSOLE_EXTRA_LOCATIONS` (setup refuses the route twice), remove the old mirror line from the run
user's crontab, and `sudo ./setup.sh`.

## Tests

`assistant/.venv/bin/python -m pytest -q` from `assistant/` (part of `./verify.sh`). `deploy/live-check.sh` checks
the deployed assistant on the VM end to end: the real Claude command's isolation (its own conversation's files only;
not other conversations, the database or any CLAUDE.md) and a screenshot, a voice note, a Word file, a sheet and an
e-mail asked about through the running service. It removes everything it creates.
