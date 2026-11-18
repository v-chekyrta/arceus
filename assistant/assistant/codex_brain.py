"""One turn of the assistant on Codex (`codex exec`), giving the same events as brain.py's Claude turn.

What keeps it in its box, since Codex reads files through a shell and its read-only sandbox may read the whole disk:
- **No shell at all** (`features.shell_tool=false`, and unified exec off): Codex cannot run a command.
- **It reads through the assistant's own tools** (tools_mcp.py: read_file, list_dir, search_code), which check every
  path on the server side against the roots this turn was given: the code mirror and this conversation's own uploads,
  links resolved. Nothing else of the disk — the data folder, the tokens, ~/.codex — is reachable.
- **Everything else Codex could reach for is off**: the browser, computer use, image generation, apps, plugins,
  skills, sub-agents, hooks, web search. `--ignore-user-config` and `--ignore-rules`: the run user's own Codex
  settings never reach the assistant.

Who pays (`auth`), like brain.py:
- `subscription`: the developer's own Codex sign-in (ChatGPT) in their CODEX_HOME (~/.codex), as the factory runs.
- `api_key`: OPENAI_API_KEY from assistant.env as CODEX_API_KEY, with a Codex home of the assistant's own that holds no
  sign-in; missing, the turn does not start; refused, it says so. Nothing falls back to the subscription.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import AsyncIterator

from assistant.brain import (BAD_KEY, NO_KEY, PASSED, BrainConfig, Event, NoKey, stop)

# Features of Codex 0.167 that would give it a way out of the assistant's tools: a shell, a browser, a screen, image
# generation, apps and plugins, skills that install things, sub-agents, hooks. Off for every turn.
OFF = ("shell_tool", "unified_exec", "shell_snapshot", "apps", "browser_use", "browser_use_external",
       "browser_use_full_cdp_access", "computer_use", "image_generation", "multi_agent", "plugins", "remote_plugin",
       "plugin_discovery_prefetch", "skill_search", "skill_mcp_dependency_install", "tool_suggest", "hooks",
       "worktrees", "goals", "sleep_tool", "code_mode_host", "in_app_browser", "realtime_conversation",
       "multi_agent_v2", "code_mode_only_strict_3p_tools",
       # Codex's own image reader reads wherever its sandbox lets it: images come through her view_image instead.
       "view_image")
EFFORTS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
RESTRICTED = ("Цей ключ OpenAI не можна використати для Codex: організація ключа підключена до щомісячних API-кредитів "
              "за підпискою ChatGPT. Потрібен ключ іншої організації або вимкнена пільга.")
HINT = ("\n\nЯк ти бачиш файли: код проєкту й файли цієї розмови читай інструментами read_file, list_dir і search_code "
        "(шляхи в коді — відносно кореня коду); картинки з розмови відкривай її інструментом view_image за шляхом, "
        "який дає list_dir. Команд ти не запускаєш і в інтернет не ходиш.")


def toml(value) -> str:
    """A value for `-c key=value`, as TOML reads it (JSON strings are valid TOML basic strings)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {toml(v)}" for k, v in value.items()) + " }"
    return json.dumps(str(value), ensure_ascii=False)


def codex_env(cfg: BrainConfig, base: dict | None = None) -> dict[str, str]:
    from assistant import secrets
    base = dict(os.environ if base is None else base)
    env = {k: base[k] for k in PASSED if base.get(k)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    if cfg.auth == "subscription":
        env.update({k: base[k] for k in ("HOME", "USER", "CODEX_HOME") if base.get(k)})
        return env
    if cfg.secrets is None or cfg.codex_home is None:
        raise NoKey("no assistant.env or Codex home configured")
    try:
        key = secrets.get(cfg.secrets, "OPENAI_API_KEY")
    except (OSError, secrets.SecretsError) as e:
        raise NoKey(str(e)) from None
    if not key:
        raise NoKey(f"OPENAI_API_KEY not set in {cfg.secrets}")
    (cfg.codex_home / ".codex").mkdir(mode=0o700, parents=True, exist_ok=True)
    return {**env, "HOME": str(cfg.codex_home), "CODEX_HOME": str(cfg.codex_home / ".codex"), "CODEX_API_KEY": key}


def command(cfg: BrainConfig, session: str | None, mcp: dict | None, instructions: str) -> list[str]:
    cmd = [cfg.codex_bin, "exec"]
    if session:
        cmd += ["resume", session]
    cmd += ["--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
            "-m", cfg.codex_model, "-c", f"model_reasoning_effort={toml(EFFORTS.get(cfg.effort, 'low'))}",
            "-c", 'approval_policy="never"', "-c", 'sandbox_mode="read-only"', "-c", 'web_search="disabled"',
            "-c", f"developer_instructions={toml(instructions + HINT)}",
            # These models call tools from inside code mode (model metadata wins over features.code_mode): her own
            # tools are offered directly instead, and code mode (and its host) stays off. Sub-agents off too.
            "-c", 'features.code_mode={ enabled = false, direct_only_tool_namespaces = ["mcp__assistant"] }',
            "-c", "agents.enabled=false"]
    for feature in OFF:
        cmd += ["--disable", feature]
    if mcp:
        cmd += ["-c", f"mcp_servers.assistant.command={toml(mcp['command'])}",
                "-c", f"mcp_servers.assistant.args={toml(mcp['args'])}",
                "-c", f"mcp_servers.assistant.env={toml(mcp['env'])}",
                "-c", "mcp_servers.assistant.required=true",
                "-c", 'mcp_servers.assistant.default_tools_approval_mode="approve"']
    return cmd + ["-"]                 # the prompt from stdin


def error_words(message: str) -> str:
    low = message.lower()
    if "codex_byok_restricted" in low:
        return RESTRICTED
    if "401" in low or "invalid_api_key" in low or "incorrect api key" in low or "unauthorized" in low:
        return BAD_KEY
    if "usage limit" in low or "rate limit" in low or "429" in low or "quota" in low:
        return "Ліміт використання Codex на зараз вичерпано. Спробуйте трохи пізніше."
    if "not logged in" in low or "login" in low or "sign in" in low:
        return "Асистент не зміг увійти в Codex. Це треба полагодити адміністратору фабрики."
    return "Асистент не зміг відповісти через помилку."


def parse_line(line: str, state: dict) -> list[Event]:
    """One `codex exec --json` line -> the chat's events. `state` carries the turn's text across lines."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return []
    t = obj.get("type")
    item = obj.get("item") or {}
    if t == "thread.started":
        state["thread"] = obj.get("thread_id")
        return [Event("session", data={"session_id": obj.get("thread_id")})]
    if t == "item.started" and item.get("type") == "mcp_tool_call":
        return [Event("tool", f"mcp__{item.get('server') or 'assistant'}__{item.get('tool', '')}")]
    if t == "item.completed" and item.get("type") == "agent_message":
        text = str(item.get("text") or "")
        piece = ("\n\n" if state.get("text") else "") + text
        state["text"] = state.get("text", "") + piece
        state["said"] = True
        return [Event("delta", piece)]
    if t == "turn.completed":
        return [Event("done", state.get("text", ""), data={"session_id": state.get("thread"),
                                                            "usage": obj.get("usage")})]
    if t in ("turn.failed", "error"):
        message = str((obj.get("error") or {}).get("message") if t == "turn.failed" else obj.get("message") or "")
        lost = state.get("resumed") and not state.get("said")
        return [Event("error", error_words(message), data={"detail": message[:500],
                                                            "num_turns": 0 if lost else None})]
    return []


async def run_turn(cfg: BrainConfig, prompt: str, session: str | None, new_session: str | None = None,
                   read_dirs: tuple[Path, ...] = (), mcp: dict | None = None) -> AsyncIterator[Event]:
    """One turn on Codex. Codex names the session itself (thread.started); `new_session` is not used. Always ends
    with exactly one `done` or `error`."""
    try:
        env = codex_env(cfg)
    except NoKey as e:
        yield Event("error", NO_KEY, data={"detail": str(e), "num_turns": None})
        return
    instructions = cfg.prompt_file.read_text() if cfg.prompt_file.is_file() else ""
    cmd = command(cfg, session, mcp, instructions)
    state: dict = {"resumed": bool(session)}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cfg.workdir), env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024, start_new_session=True)
    except OSError as e:
        yield Event("error", "Асистент зараз не може запустити Codex на сервері. Це треба полагодити адміністратору "
                             "фабрики.", data={"detail": str(e)})
        return
    assert proc.stdin and proc.stdout and proc.stderr
    stderr_task = asyncio.create_task(proc.stderr.read())
    finished = False
    try:
        async with asyncio.timeout(cfg.timeout_s):
            try:
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                for ev in parse_line(raw.decode("utf-8", "replace"), state):
                    if finished:
                        continue
                    if ev.kind in ("done", "error"):
                        finished = True
                    yield ev
            await proc.wait()
    except TimeoutError:
        await stop(proc)
        if not finished:
            yield Event("error", f"Codex не відповів за {cfg.timeout_s // 60} хв, відповідь перервано.")
        return
    finally:
        if proc.returncode is None:
            await stop(proc)
    if not finished:
        err = (await stderr_task).decode("utf-8", "replace").strip()
        tail = err.splitlines()[-1][:300] if err else f"код виходу {proc.returncode}"
        lost = bool(session) and not state.get("said")
        yield Event("error", error_words(err) if err else f"Codex завершився без відповіді ({tail}).",
                    data={"detail": tail, "num_turns": 0 if lost else None})
