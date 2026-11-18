"""Moving her to the team's key gives her Claude a home of its own. The conversations go on: her transcripts from the
run user's ~/.claude are carried over once at start, and a session Claude does not have is rebuilt from the stored
history. Driven through the real run_turn with a stand-in claude that answers as the real one does (2.1.x): an
unknown --resume is a result with num_turns 0 and «No conversation found with session ID»."""

import json
from pathlib import Path

from starlette.testclient import TestClient

from assistant import server
from assistant.brain import BrainConfig
from tests.test_chat import H, keyed, wait_idle

STAND_IN = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.environ["HOME"], ".claude")
proj = os.path.join(cfg, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(os.getcwd())))
log = os.path.join(LOG_DIR, "calls.jsonl")   # written in below: nothing of the test's environment reaches it
sid = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
with open(log, "a") as f:
    f.write(json.dumps({"resume": "--resume" in args, "sid": sid, "prompt": prompt,
                        "key": os.environ.get("ANTHROPIC_API_KEY")}) + "\n")
if "--resume" in args and not os.path.exists(os.path.join(proj, sid + ".jsonl")):
    print("No conversation found with session ID: " + sid, file=sys.stderr)
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
                      "session_id": sid, "errors": ["No conversation found with session ID: " + sid]}))
    sys.exit(1)
os.makedirs(proj, exist_ok=True)
open(os.path.join(proj, sid + ".jsonl"), "a").write(json.dumps({"prompt": prompt}) + "\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}))
print(json.dumps({"type": "result", "subtype": "success", "result": "так, пам'ятаю", "session_id": sid,
                  "num_turns": 1}))
'''


def setup(tmp: Path, monkeypatch) -> tuple[server.Settings, Path]:
    run_user = tmp / "factory"                       # the service's own home, with her old transcripts in it
    mirror = tmp / "mirror"
    mirror.mkdir()
    monkeypatch.setenv("HOME", str(run_user))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    fake = tmp / "claude"
    fake.write_text(STAND_IN.replace("import json, os, re, sys", f"import json, os, re, sys\nLOG_DIR = {str(tmp)!r}"))
    fake.chmod(0o755)
    prompt = tmp / "prompt.md"
    prompt.write_text("Асистент {project}")
    b = BrainConfig(claude_bin=str(fake), workdir=mirror, prompt_file=prompt, settings_file=tmp / "s.json",
                    **{**keyed(tmp), "claude_home": tmp / "data" / "claude"})
    return server.Settings(data_dir=tmp / "data", brain=b, allowed_users=[], project="Northwind"), run_user


def old_transcript(run_user: Path, mirror: Path, sid: str) -> Path:
    d = run_user / ".claude" / "projects" / server.project_dir(mirror)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text('{"before": "the key"}\n')
    (run_user / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": "the owner\'s sign-in"}')
    return d


def calls(tmp: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp / "calls.jsonl").read_text().splitlines()]


def ask(client, cid: str, text: str) -> dict:
    client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": text})
    return wait_idle(client, cid, timeout=15)


def test_a_carried_session_resumes_as_it_was_on_the_teams_key(tmp_path, monkeypatch):
    s, run_user = setup(tmp_path, monkeypatch)
    old_transcript(run_user, s.brain.workdir, "sid-before")
    with TestClient(server.build(s)) as client:
        app = client.app.state.app
        own = s.brain.claude_home / ".claude" / "projects" / server.project_dir(s.brain.workdir)
        assert (own / "sid-before.jsonl").exists()
        assert not list((s.brain.claude_home).rglob(".credentials.json"))   # a transcript, never a sign-in
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        app.store.add_message(cid, "user", "старе питання", author="m@x")
        app.store.set_session(cid, "sid-before")
        conv = ask(client, cid, "а пам'ятаєш?")
    assert conv["messages"][-1]["text"] == "так, пам'ятаю"
    last = calls(tmp_path)[-1]
    assert last["resume"] and last["sid"] == "sid-before" and last["key"] == "sk-ant-team"
    assert "відновлено з історії" not in last["prompt"]


def test_a_session_her_claude_does_not_have_is_rebuilt_from_the_database(tmp_path, monkeypatch):
    s, run_user = setup(tmp_path, monkeypatch)           # nothing to carry: the transcript is gone
    with TestClient(server.build(s)) as client:
        app = client.app.state.app
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        app.store.add_message(cid, "user", "старе питання про сертифікати", author="m@x")
        app.store.add_message(cid, "assistant", "стара відповідь про сертифікати")
        app.store.set_session(cid, "sid-lost")
        conv = ask(client, cid, "а далі?")
        assert conv["messages"][-1]["text"] == "так, пам'ятаю"
        first, rebuilt = calls(tmp_path)[-2:]
        assert first["resume"] and first["sid"] == "sid-lost"           # tried as it was: Claude has no such session
        assert not rebuilt["resume"] and rebuilt["sid"] != "sid-lost"   # a new one, given the conversation so far
        assert "старе питання про сертифікати" in rebuilt["prompt"] and "стара відповідь" in rebuilt["prompt"]
        assert app.store.conversation(cid).claude_session == rebuilt["sid"]
        ask(client, cid, "і ще")
    assert calls(tmp_path)[-1]["resume"] and calls(tmp_path)[-1]["sid"] == rebuilt["sid"]   # and goes on from it


def test_carrying_happens_once_and_never_on_a_subscription(tmp_path, monkeypatch):
    s, run_user = setup(tmp_path, monkeypatch)
    d = old_transcript(run_user, s.brain.workdir, "a")
    (d / "a").mkdir()
    (d / "a" / "sub.jsonl").write_text("{}\n")
    assert server.carry_sessions(s.brain) == 2
    (d / "b.jsonl").write_text("{}\n")
    assert server.carry_sessions(s.brain) == 0           # once: a later transcript there is not hers any more
    s.brain.auth = "subscription"
    s.brain.claude_home = tmp_path / "other"
    assert server.carry_sessions(s.brain) == 0 and not (tmp_path / "other").exists()
