"""Her replies on Codex: no shell, nothing of Codex's own reach for the disk or the web, her own read-only tools
bounded to the code and this conversation's files, the factory's own Codex sign-in or an OpenAI key, and the same
events the server already handles. Driven by a stand-in codex that prints codex 0.167's own `exec --json` lines."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from assistant import codex_brain, server
from assistant.brain import BAD_KEY, NO_KEY, BrainConfig
from assistant.file_tools import FileTools, Refused
from tests.test_chat import H, wait_idle

STAND_IN = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
open("@LOG@", "a").write(json.dumps({"args": args, "prompt": prompt, "env": dict(os.environ)}) + "\n")
mode = open("@MODE@").read().strip() if os.path.exists("@MODE@") else "ok"
resumed = "resume" in args
tid = args[args.index("resume") + 1] if resumed else "thread-new-1"
def out(o): print(json.dumps(o), flush=True)
if mode == "lost" and resumed and tid == "thread-gone":
    out({"type": "error", "message": "no rollout found for thread id " + tid}); sys.exit(1)
if not resumed and os.path.exists("@MODE@.next"):
    tid = open("@MODE@.next").read().strip()
out({"type": "thread.started", "thread_id": tid})
out({"type": "turn.started"})
if mode == "restricted":
    out({"type": "turn.failed", "error": {"message": json.dumps({"error": {"code": "codex_byok_restricted_by_api_credit_promo"}})}}); sys.exit(1)
if mode == "401":
    out({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized: invalid_api_key"}}); sys.exit(1)
out({"type": "item.started", "item": {"id": "i1", "type": "mcp_tool_call", "server": "assistant", "tool": "search_code",
     "arguments": {"pattern": "def"}, "status": "in_progress"}})
out({"type": "item.completed", "item": {"id": "i1", "type": "mcp_tool_call", "server": "assistant", "tool": "search_code", "status": "completed"}})
out({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "Подивилась код: поріг 60 балів."}})
out({"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 0, "output_tokens": 40}})
'''


def stand_in(tmp: Path) -> Path:
    fake = tmp / "codex"
    fake.write_text(STAND_IN.replace("@LOG@", str(tmp / "codex.jsonl")).replace("@MODE@.next", str(tmp / "next"))
                    .replace("@MODE@", str(tmp / "mode")))
    fake.chmod(0o755)
    return fake


def calls(tmp: Path) -> list[dict]:
    p = tmp / "codex.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def cfg(tmp: Path, auth="subscription", **kw) -> BrainConfig:
    (tmp / "mirror").mkdir(exist_ok=True)
    (tmp / "prompt.md").write_text("Ти Олена.")
    env = tmp / "assistant.env"
    env.write_text("OPENAI_API_KEY=sk-proj-openai-1\nANTHROPIC_API_KEY=sk-ant-x\n")
    env.chmod(0o600)
    return BrainConfig(claude_bin="claude", workdir=tmp / "mirror", prompt_file=tmp / "prompt.md",
                       settings_file=tmp / "s.json", provider="codex", codex_bin=str(stand_in(tmp)),
                       codex_model="gpt-6.1-terra", auth=auth, secrets=env, codex_home=tmp / "codex-home", **kw)


def turn(c: BrainConfig, session=None, mcp=None) -> list:
    async def go():
        return [e async for e in codex_brain.run_turn(c, "Чому немає сертифіката?", session, mcp=mcp)]
    return asyncio.run(go())


def flag_values(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def test_no_shell_no_browser_no_web_and_none_of_the_users_own_codex_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    evs = turn(cfg(tmp_path), mcp={"command": "/py", "args": ["-m", "assistant.tools_mcp"], "env": {"A": "1"}})
    assert [e.kind for e in evs] == ["session", "tool", "delta", "done"]
    a = calls(tmp_path)[0]["args"]
    disabled = set(flag_values(a, "--disable"))
    assert {"shell_tool", "unified_exec", "browser_use", "computer_use", "image_generation", "apps", "plugins",
            "multi_agent", "hooks"} <= disabled
    configs = flag_values(a, "-c")
    assert 'web_search="disabled"' in configs and 'approval_policy="never"' in configs
    assert "--ignore-user-config" in a and "--ignore-rules" in a and a[a.index("-m") + 1] == "gpt-6.1-terra"
    instructions = next(c for c in configs if c.startswith("developer_instructions="))
    assert "Ти Олена." in instructions and "read_file" in instructions
    assert 'mcp_servers.assistant.default_tools_approval_mode="approve"' in configs
    # Her tools directly, not from inside code mode (whose host stays off); no sub-agents; no image reader of Codex's.
    assert 'features.code_mode={ enabled = false, direct_only_tool_namespaces = ["mcp__assistant"] }' in configs
    assert "agents.enabled=false" in configs and {"multi_agent_v2", "view_image", "code_mode_host"} <= disabled
    assert a[-1] == "-" and calls(tmp_path)[0]["prompt"] == "Чому немає сертифіката?"


def test_events_are_the_chats_own_and_a_session_resumes_by_its_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    evs = turn(cfg(tmp_path))
    assert evs[0].data["session_id"] == "thread-new-1"
    assert evs[1].text == "mcp__assistant__search_code"            # the chat's own words for a tool (tool_words)
    assert evs[-1].kind == "done" and evs[-1].text == "Подивилась код: поріг 60 балів."
    turn(cfg(tmp_path), session="thread-old-7")
    a = calls(tmp_path)[-1]["args"]
    assert a[1:3] == ["resume", "thread-old-7"] and "--ephemeral" not in a


def test_a_thread_codex_no_longer_has_is_rebuilt_like_a_lost_claude_session(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    (tmp_path / "mode").write_text("lost")
    evs = turn(cfg(tmp_path), session="thread-gone")
    assert evs[-1].kind == "error" and evs[-1].data["num_turns"] == 0       # server.py then rebuilds from history


def test_the_factorys_own_codex_sign_in_and_never_a_key(tmp_path, monkeypatch):
    for k, v in {"PATH": "/usr/bin:/bin", "HOME": "/home/factory", "USER": "factory", "OPENAI_API_KEY": "sk-parent",
                 "CODEX_API_KEY": "sk-parent-2", "SLACK_BOT_TOKEN": "xoxb-1"}.items():
        monkeypatch.setenv(k, v)
    turn(cfg(tmp_path, auth="subscription"))
    env = calls(tmp_path)[0]["env"]
    assert env["HOME"] == "/home/factory"
    for gone in ("OPENAI_API_KEY", "CODEX_API_KEY", "SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY"):
        assert gone not in env


def test_an_openai_key_from_assistant_env_in_a_codex_home_of_its_own(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/factory")
    turn(cfg(tmp_path, auth="api_key"))
    env = calls(tmp_path)[0]["env"]
    assert env["CODEX_API_KEY"] == "sk-proj-openai-1" and env["CODEX_HOME"] == str(tmp_path / "codex-home/.codex")
    assert env["HOME"] == str(tmp_path / "codex-home") and "ANTHROPIC_API_KEY" not in env


def test_without_the_key_no_codex_starts_and_refusals_are_said_plainly(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    c = cfg(tmp_path, auth="api_key")
    c.secrets.write_text("ANTHROPIC_API_KEY=sk-ant-x\n")
    evs = turn(c)
    assert [e.text for e in evs] == [NO_KEY] and calls(tmp_path) == []
    c.secrets.write_text("OPENAI_API_KEY=sk-proj-1\n")
    (tmp_path / "mode").write_text("401")
    assert turn(c)[-1].text == BAD_KEY
    (tmp_path / "mode").write_text("restricted")
    assert turn(c)[-1].text == codex_brain.RESTRICTED


# ----------------------------------------------------------------------------------------------- her file tools
@pytest.fixture
def tree(tmp_path):
    code = tmp_path / "mirror"
    (code / "shop").mkdir(parents=True)
    (code / "shop" / "certs.py").write_text("THRESHOLD = 60\n\ndef send(score):\n    return score >= THRESHOLD\n")
    (code / "logo.png").write_bytes(b"\x89PNG\0\0binary")
    mine, other = tmp_path / "uploads" / "conv-a", tmp_path / "uploads" / "conv-b"
    mine.mkdir(parents=True)
    other.mkdir(parents=True)
    (mine / "shot.txt").write_text("скріншот: 58 балів")
    (other / "secret.txt").write_text("чужа розмова")
    data = tmp_path / "data"
    data.mkdir()
    (data / "assistant.env").write_text("SLACK_BOT_TOKEN=xoxb-secret")
    (code / "escape").symlink_to(data)
    (code / "env-link.txt").symlink_to(data / "assistant.env")
    return FileTools([code, mine]), tmp_path


def test_she_reads_the_code_and_this_conversations_files(tree):
    t, tmp = tree
    assert "2\t\n3\tdef send(score):" in t.read("shop/certs.py", 1, 0)
    assert t.list("") .startswith(".:") and "shop/" in t.list("")
    assert "shop/certs.py:1: THRESHOLD = 60" in t.search("THRESHOLD", "", "*.py")
    assert "58 балів" in t.read(str(tmp / "uploads/conv-a/shot.txt"), 1, 0)


@pytest.mark.parametrize("path", ["../data/assistant.env", "escape/assistant.env", "env-link.txt",
                                  "/etc/passwd", "UPLOADS_OTHER", "~/.codex/auth.json"])
def test_nothing_outside_the_code_and_this_conversation_is_read(tree, path):
    t, tmp = tree
    path = path.replace("UPLOADS_OTHER", str(tmp / "uploads/conv-b/secret.txt"))
    with pytest.raises(Refused):
        t.read(path, 1, 0)


def test_a_search_never_follows_a_link_out_and_skips_binaries(tree):
    t, _ = tree
    found = t.search("xoxb|SLACK|PNG", "", "")
    assert "xoxb" not in found and "secret" not in found and "logo.png" not in found


def test_her_view_image_shows_pictures_in_bounds_only(tree):
    t, tmp = tree
    (tmp / "mirror" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
    assert t.image("shot.png")["mime"] == "image/png"
    for bad in ("shop/certs.py", "escape/assistant.env", str(tmp / "uploads/conv-b/secret.txt")):
        with pytest.raises(Refused):
            t.image(bad)


# ------------------------------------------------------------------------------------------- through the server
def test_a_reply_on_codex_through_the_server_with_her_tools_bounded_to_this_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    c = cfg(tmp_path)
    c.prompt_file = tmp_path / "prompt.md"
    s = server.Settings(data_dir=tmp_path / "data", brain=c, allowed_users=[], project="Northwind",
                        config_path=tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text("x: 1\n")
    with TestClient(server.build(s)) as client:
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        (s.data_dir.parent / "uploads" / cid).mkdir(parents=True, exist_ok=True)
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "Чому немає сертифіката?"})
        conv = wait_idle(client, cid, timeout=10)
    assert conv["messages"][-1]["text"] == "Подивилась код: поріг 60 балів."
    a = calls(tmp_path)[0]["args"]
    env_cfg = next(x for x in flag_values(a, "-c") if x.startswith("mcp_servers.assistant.env="))
    roots = json.loads(env_cfg.split("=", 1)[1].replace(" = ", ": ").replace("{ ", "{").replace(" }", "}")) \
        ["ASSISTANT_READ_ROOTS"].split(os.pathsep)
    assert roots == [str(tmp_path / "mirror"), str((s.data_dir.parent / "uploads" / cid).resolve())] or \
        roots == [str(tmp_path / "mirror"), str(s.data_dir.parent / "uploads" / cid)]
    st = json.loads((tmp_path / "assistant-status.json").read_text())["claude"]
    assert st["provider"] == "codex" and st["last"] == "ok"


def server_settings(tmp: Path) -> server.Settings:
    c = cfg(tmp)
    (tmp / "config.yaml").write_text("x: 1\n")
    return server.Settings(data_dir=tmp / "data", brain=c, allowed_users=[], project="Northwind",
                           config_path=tmp / "config.yaml")


def ask(client, cid: str, text: str) -> dict:
    client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": text})
    return wait_idle(client, cid, timeout=10)


def test_a_lost_codex_thread_is_rebuilt_and_the_next_turn_resumes_codexs_own_new_thread(tmp_path, monkeypatch):
    """Through the server: the stored thread is gone, the conversation is rebuilt into a thread Codex names itself,
    that name is kept, and the next message resumes it."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    (tmp_path / "mode").write_text("lost")
    (tmp_path / "next").write_text("thread-rebuilt-9")
    with TestClient(server.build(server_settings(tmp_path))) as client:
        app = client.app.state.app
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        app.store.add_message(cid, "user", "старе питання про сертифікати", author="m@x")
        app.store.set_session(cid, "thread-gone")
        conv = ask(client, cid, "а далі?")
        assert conv["messages"][-1]["text"] == "Подивилась код: поріг 60 балів."
        assert app.store.conversation(cid).claude_session == "thread-rebuilt-9"     # Codex's own, not a made-up id
        ask(client, cid, "і ще")
    tried = [(c["args"][1:3] if c["args"][1] == "resume" else ["new"]) for c in calls(tmp_path)]
    assert tried == [["resume", "thread-gone"], ["new"], ["resume", "thread-rebuilt-9"]]
    assert "старе питання про сертифікати" in calls(tmp_path)[1]["prompt"]


def test_a_note_taken_back_rebuilds_into_codexs_own_thread_too(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    (tmp_path / "next").write_text("thread-after-forget")
    with TestClient(server.build(server_settings(tmp_path))) as client:
        app = client.app.state.app
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        app.store.set_session(cid, "thread-before")
        note = app.store.add_note("Поріг сертифіката 70 балів", "сертифікати", "project", "m@x", None)
        app.store.expose([note], cid, "1")                         # it was in front of her in this conversation
        app.store.forget_note(note, "m@x", "хибна нотатка")       # and is taken back: the thread is rebuilt
        assert app.store.conversation(cid).rebuild
        ask(client, cid, "що там?")
        assert app.store.conversation(cid).claude_session == "thread-after-forget"
        ask(client, cid, "далі")
    assert [c["args"][1] for c in calls(tmp_path)] == ["--json", "resume"]
    assert calls(tmp_path)[1]["args"][2] == "thread-after-forget"
