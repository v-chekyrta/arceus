"""One turn of the assistant: Claude Code in print mode, resumed per conversation, read-only on a code mirror.

What keeps it in its box:
- `--tools` lists the only tools that exist in the session: Read, Grep, Glob (plus the assistant's own MCP tools,
  when configured). No Bash, no Edit/Write, no web.
- `--permission-mode dontAsk`: anything not pre-approved is refused, never asked. Reading is approved only inside
  the working directory (the code mirror) by the settings file; the factory's data, keys and homes are also denied
  outright in case a path rule is ever widened.
- `--setting-sources project`: the run user's own settings, hooks and memory never reach the assistant.
- `CLAUDE_CODE_DISABLE_CLAUDE_MDS` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY`: no CLAUDE.md is loaded on its own — not the
  factory agents' notes, not one above the mirror — and nothing is remembered between conversations. The repositories'
  CLAUDE.md files are still there to Read as code.
- A conversation's attachments: its own upload folder is added for that turn only (`--add-dir` and read rules for
  that one folder). Other conversations' files and the assistant's database stay outside.

Who pays (`auth`): Claude's environment is built from nothing, never copied from this process.
- `subscription` (the default, as it always was): the run user's own Claude sign-in, the developer's whose factory
  this is: their HOME, and their sign-in token if the service was given one (CLAUDE_CODE_OAUTH_TOKEN). No API key.
- `api_key`: ANTHROPIC_API_KEY from assistant.env, read for every turn, and a Claude home of the assistant's own
  (HOME and CLAUDE_CONFIG_DIR in its data folder) that holds no sign-in: with the key missing or refused the turn
  fails and says so; it never falls back to the subscription.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator


@dataclass
class BrainConfig:
    claude_bin: str
    workdir: Path              # the read-only code mirror: the only place the assistant may read
    prompt_file: Path          # the assistant's system prompt (persona, rules, playbook)
    settings_file: Path        # permissions for the session
    model: str = "sonnet"
    effort: str = "low"
    timeout_s: int = 600
    mcp_config: Path | None = None
    extra_tools: tuple[str, ...] = ()
    auth: str = "api_key"      # api_key | subscription (see the module's docstring)
    secrets: Path | None = None   # assistant.env: ANTHROPIC_API_KEY
    claude_home: Path | None = None   # api_key: Claude's own HOME and config, with no sign-in in it
    provider: str = "claude"   # claude | codex (codex_brain.py): which CLI writes her replies
    codex_bin: str = "codex"
    codex_model: str = "gpt-6.1-terra"
    codex_home: Path | None = None    # api_key on Codex: its own HOME and CODEX_HOME, with no sign-in in it


ISOLATION_ENV = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                 "DISABLE_AUTOUPDATER": "1"}
# What Claude may get of this process's environment: how to find programs and read text. Nothing that signs in.
PASSED = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")
# The subscription's own sign-in, as the developer gave it to the service: what Claude finds the login by (HOME, USER
# for a Mac's Keychain) or the login itself (a `claude setup-token` token).
SIGN_IN = ("HOME", "USER", "CLAUDE_CODE_OAUTH_TOKEN")
NO_KEY = "Асистентові не налаштовано API-ключ. Його додають у налаштуваннях асистента в консолі фабрики."
BAD_KEY = "API-ключ асистента не прийнято. Його треба замінити в налаштуваннях асистента в консолі фабрики."


class NoKey(Exception):
    """api_key mode without a key: the turn is not started at all."""


def claude_env(cfg: BrainConfig, base: dict | None = None) -> dict[str, str]:
    """Claude's whole environment for a turn. Built from nothing: no token, key or sign-in of this process's reaches
    it except the one `auth` names."""
    from assistant import secrets
    base = dict(os.environ if base is None else base)
    env = {k: base[k] for k in PASSED if base.get(k)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    if cfg.auth == "subscription":
        env.update({k: base[k] for k in SIGN_IN if base.get(k)})
        return {**env, **ISOLATION_ENV}
    if cfg.secrets is None or cfg.claude_home is None:
        raise NoKey("no assistant.env or Claude home configured")
    try:
        key = secrets.get(cfg.secrets, "ANTHROPIC_API_KEY")
    except (OSError, secrets.SecretsError) as e:
        raise NoKey(str(e)) from None
    if not key:
        raise NoKey(f"ANTHROPIC_API_KEY not set in {cfg.secrets}")
    cfg.claude_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    return {**env, **ISOLATION_ENV, "HOME": str(cfg.claude_home),
            "CLAUDE_CONFIG_DIR": str(cfg.claude_home / ".claude"), "ANTHROPIC_API_KEY": key}


@dataclass
class Event:
    kind: str                  # session | delta | tool | done | error
    text: str = ""
    data: dict | None = None


def path_rule(tool: str, folder: Path) -> str:
    """A permission rule for everything under an absolute folder (`//` marks an absolute path in Claude rules)."""
    return f"{tool}(/{folder.resolve().as_posix()}/**)"


def command(cfg: BrainConfig, session: str | None, new_session: str | None,
            read_dirs: tuple[Path, ...] = (), mcp: dict | None = None) -> list[str]:
    """`mcp`: the assistant's own tools server for this turn ({command, args, env}); its tools are allowed by name."""
    tools = ["Read", "Grep", "Glob", *cfg.extra_tools]
    cmd = [cfg.claude_bin, "-p", "--output-format", "stream-json", "--include-partial-messages", "--verbose",
           "--model", cfg.model, "--effort", cfg.effort,
           "--tools", ",".join(tools),
           "--permission-mode", "dontAsk",
           "--setting-sources", "project",
           "--settings", str(cfg.settings_file),
           "--system-prompt-file", str(cfg.prompt_file)]
    # Only the MCP servers named here exist: never the account's claude.ai connectors or anything else.
    if mcp:
        cmd += ["--mcp-config", json.dumps({"mcpServers": {"assistant": mcp}}), "--strict-mcp-config",
                "--allowedTools", "mcp__assistant"]
    else:
        cmd += ["--mcp-config", str(cfg.mcp_config) if cfg.mcp_config else '{"mcpServers": {}}',
                "--strict-mcp-config"]
    for d in read_dirs:
        cmd += ["--add-dir", str(d.resolve()),
                "--allowedTools", path_rule("Read", d), path_rule("Grep", d), path_rule("Glob", d)]
    if session:
        cmd += ["--resume", session]
    elif new_session:
        cmd += ["--session-id", new_session]
    return cmd


ERROR_WORDS = {
    "error_max_turns": "Асистент зробив забагато кроків і зупинився, не дописавши відповідь.",
    "error_during_execution": "Асистент не зміг завершити відповідь: під час роботи сталася помилка.",
    "error_max_budget_usd": "Асистент вичерпав ліміт на одну відповідь.",
}


def error_words(obj: dict) -> str:
    """What the chat shows when a turn fails: plain words, never Claude's internal codes."""
    sub = str(obj.get("subtype") or "")
    text = str(obj.get("result") or "")
    low = text.lower()
    if "rate limit" in low or "usage limit" in low or "limit reached" in low:
        return "Ліміт використання Claude на зараз вичерпано. Спробуйте трохи пізніше."
    if "overloaded" in low:
        return "Сервери Claude зараз перевантажені. Спробуйте ще раз за хвилину."
    if obj.get("api_error_status") == 401 or "invalid api key" in low:
        return BAD_KEY
    if "login" in low or "authentication" in low or "oauth" in low:
        return "Асистент не зміг увійти в Claude. Це треба полагодити адміністратору фабрики."
    return ERROR_WORDS.get(sub, "Асистент не зміг відповісти через помилку.")


def parse_line(line: str) -> list[Event]:
    """One stream-json line -> the events the chat cares about."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return []
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "init":
        return [Event("session", data={"session_id": obj.get("session_id")})]
    if t == "stream_event":
        ev = obj.get("event") or {}
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta":
                return [Event("delta", delta.get("text", ""))]
        if ev.get("type") == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "tool_use":
                return [Event("tool", block.get("name", ""))]
        return []
    if t == "result":
        if obj.get("is_error") or obj.get("subtype") != "success":
            return [Event("error", error_words(obj), data={"detail": str(obj.get("result") or obj.get("subtype")),
                                                           "num_turns": obj.get("num_turns")})]
        return [Event("done", obj.get("result") or "", data={"session_id": obj.get("session_id"),
                                                               "cost_usd": obj.get("total_cost_usd")})]
    return []


async def stop(proc: asyncio.subprocess.Process) -> None:
    """Kill Claude and everything it started (its own process group): a child left behind would keep the pipes open."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if proc.returncode is None:
            proc.kill()
    await proc.wait()


async def run_turn(cfg: BrainConfig, prompt: str, session: str | None,
                   new_session: str | None = None, env: dict | None = None,
                   read_dirs: tuple[Path, ...] = (), mcp: dict | None = None) -> AsyncIterator[Event]:
    """Run one turn and yield events. Always ends with exactly one `done` or `error`."""
    if cfg.provider == "codex":
        from assistant import codex_brain
        async for ev in codex_brain.run_turn(cfg, prompt, session, new_session, read_dirs=read_dirs, mcp=mcp):
            yield ev
        return
    cmd = command(cfg, session, new_session, read_dirs, mcp)
    try:
        full_env = {**claude_env(cfg), **(env or {})}
    except NoKey as e:
        yield Event("error", NO_KEY, data={"detail": str(e), "num_turns": None})
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cfg.workdir), env=full_env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024, start_new_session=True)
    except OSError as e:
        yield Event("error", "Асистент зараз не може запустити Claude на сервері. Це треба полагодити "
                             "адміністратору фабрики.", data={"detail": str(e)})
        return
    assert proc.stdin and proc.stdout and proc.stderr
    stderr_task = asyncio.create_task(proc.stderr.read())

    finished = False
    try:
        async with asyncio.timeout(cfg.timeout_s):
            # Inside the timeout: a Claude that never reads its stdin must not hold the conversation forever.
            try:
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass   # it exited before reading: what it printed (or the exit code) says why
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                for ev in parse_line(raw.decode("utf-8", "replace")):
                    if finished:
                        continue
                    if ev.kind in ("done", "error"):
                        finished = True
                    yield ev
            await proc.wait()
    except TimeoutError:
        await stop(proc)
        if not finished:
            yield Event("error", f"Claude не відповів за {cfg.timeout_s // 60} хв, відповідь перервано.")
        return
    finally:
        if proc.returncode is None:   # also on cancellation: no Claude is left running behind the chat
            await stop(proc)
    if not finished:
        err = (await stderr_task).decode("utf-8", "replace").strip()
        tail = err.splitlines()[-1][:300] if err else f"код виходу {proc.returncode}"
        yield Event("error", f"Claude завершився без відповіді ({tail}).")
