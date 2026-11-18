"""Settings → Assistant: its state as the assistant and the host wrote it, saved against applied, who talks to her,
the keys write-only (through the host's executor, with a sentinel that must come back nowhere), its settings through
Settings' own check with the risky ones locked, and who may do what."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

pytest.importorskip("starlette")

from arceus_runner.console import hostops, settings_form as sf, views_assistant  # noqa: E402
from arceus_runner.console.hostops import Host, Ledger  # noqa: E402
from arceus_runner.console.web import Settings  # noqa: E402
from arceus_runner.db import ReviewStore  # noqa: E402

from tests.conftest import example_raw  # noqa: E402
from tests.test_console_auth import csrf_of, make_admin, signed_in  # noqa: E402
from tests.test_console_ops import FakeSystem  # noqa: E402
from tests.test_console_qa import person  # noqa: E402

SENTINEL = "sk-ant-api03-SENTINEL-" + "x7Q2" * 6
ME = pwd.getpwuid(os.getuid()).pw_name
UNIT = f"arceus-assistant@{ME}"


def iso(age: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(seconds=age)).isoformat(timespec="seconds")


class System(FakeSystem):
    """The host's commands, and the assistant: `console env` names its assistant.env; a restart makes it write its
    status again, as the real one does when it starts."""

    def __init__(self, data: Path):
        super().__init__(data)
        self.data = data
        self.units[UNIT] = {"ActiveState": "active", "ActiveEnterTimestamp": "a0"}

    def __call__(self, args, **kw):
        if args[-2:] == ["console", "env"]:
            self.calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, f"ASSISTANT_SECRETS={self.data / 'assistant.env'}\n", "")
        r = super().__call__(args, **kw)
        if args[:2] == ["systemctl", "restart"] and args[2] == UNIT:
            status(self.data, sha=sha_of(self.data / "config.yaml"))
        return r


def nginx(path: Path, port: int | None) -> None:
    """The console's nginx.conf as setup writes it, with or without the assistant's route (setup.sh's own block)."""
    block = "" if port is None else f"""    location /assistant/ {{
      auth_request /_console_auth;
      auth_request_set $arceus_user $upstream_http_x_arceus_user;
      error_page 401 = @assistant_signin;
      proxy_set_header X-Assistant-User $arceus_user;
      proxy_pass http://127.0.0.1:{port};
    }}
    location = /assistant {{ return 301 /assistant/; }}
"""
    path.write_text("http {\n  server {\n    location /grafana/ {\n      proxy_pass http://127.0.0.1:3000;\n    }\n"
                    + block + "    location / {\n      proxy_pass http://127.0.0.1:8180;\n    }\n  }\n}\n")


def sha_of(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def status(data: Path, sha: str, slack: dict | None = None, claude: dict | None = None, age: float = 0,
           beat_age: float = 0) -> None:
    """Her status file: started `age` seconds ago, last kept `beat_age` seconds ago."""
    (data / "assistant-status.json").write_text(json.dumps({
        "schema": 1, "started_at": iso(age), "at": iso(beat_age), "config_sha256": sha, "web": True, "port": 8190,
        "slack": slack or {"state": "connected", "team": "Acme", "bot": "olena", "at": iso()},
        "claude": claude or {"auth": "api_key", "last": "ok", "at": iso()}}))


@pytest.fixture
def w(tmp_path: Path, monkeypatch):
    """A console and the host's executor on one data folder, with the assistant on: Slack, two people, the key."""
    raw = example_raw()
    raw["paths"] = {"db": str(tmp_path / "runner.db"), "secrets": str(tmp_path / "secrets.env")}
    raw["assistant"].update({"enabled": True, "auth": "api_key", "key_owner": "Acme (Anthropic Console: factory)",
                             "allowed_users": ["lead@example.com", "pm@example.com"],
                             "slack": {"read_channels": ["C0AAAAAAA1"], "partner_channels": ["C0BBBBBBB1"]},
                             "secrets": str(tmp_path / "assistant.env"), "expert_name": "Олени"})
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw, allow_unicode=True))
    s = Settings(config_path=config, db_path=tmp_path / "console" / "console.db", ops_dir=tmp_path / "console" / "ops")
    make_admin(s)
    ReviewStore(tmp_path / "runner.db").close()
    (s.ops_dir / "requests").mkdir(parents=True)
    (tmp_path / "assistant.env").write_text("# made by setup\nYOUTRACK_TOKEN=perm:kept-as-it-was\n")
    (tmp_path / "assistant.env").chmod(0o600)
    system = System(tmp_path)
    host = Host(data=tmp_path, setup=tmp_path / "setup.sh", ledger=tmp_path / "ledger", exe="/opt/a/arceus-runner",
                run=system, sleep=lambda _s: None, console_nginx=tmp_path / "nginx.conf")
    nginx(tmp_path / "nginx.conf", 8190)
    host.system = system
    # The host's path unit takes a request the moment it is written: here, when the page looks for the outcome.
    original = views_assistant._outcome
    monkeypatch.setattr(views_assistant, "_outcome", lambda ctx, op_id, wait=6.0: (
        hostops.dispatch(host), original(ctx, op_id, wait=0.5))[1])

    def beat():
        hostops.tick(host)   # the services' state and the keys' for the page, as the timer writes them
    beat()

    class W:
        pass
    x = W()
    x.s, x.host, x.config, x.data, x.beat = s, host, config, tmp_path, beat
    return x


def test_the_page_says_how_she_is_who_pays_and_who_talks_to_her(w):
    status(w.data, sha_of(w.config))
    page = signed_in(w.s, "owner@example.com").get("/settings/assistant/").text
    assert "The assistant runs with this config.yaml" in page
    assert "Connected" in page and "as olena in Acme" in page                 # as Slack's auth.test named them
    assert "No key" in page                       # assistant.env has no ANTHROPIC_API_KEY yet
    assert "The Anthropic API key" in page and "Acme (Anthropic Console: factory)" in page
    assert "lead@example.com, pm@example.com" in page and "Every full member of the Slack workspace" in page
    assert "C0BBBBBBB1" in page
    assert "she tells people she has called Олени" in page
    assert 'href="/settings/assistant/edit/"' in page and 'action="/settings/assistant/restart"' in page


def test_a_failed_slack_connection_and_a_refused_key_are_said(w):
    with open(w.data / "assistant.env", "a") as f:
        f.write("ANTHROPIC_API_KEY=sk-ant-revoked-long-ago\n")
    w.beat()
    status(w.data, sha_of(w.config), slack={"state": "failed", "error": "SlackApiError: invalid_auth", "at": iso()},
           claude={"auth": "api_key", "last": "bad_key", "at": iso()})
    page = signed_in(w.s, "owner@example.com").get("/settings/assistant/").text
    assert "Not connected" in page and "invalid_auth" in page and "Key refused" in page


def test_saved_is_not_applied_until_the_restart_which_goes_through_the_host(w):
    status(w.data, sha="the-file-before-the-last-save", age=3600)
    op = person(w.s, "op@example.com", {"operator"})
    page = op.get("/settings/assistant/").text
    assert "It still runs with the earlier settings" in page and "Restart to apply" in page
    assert 'action="/settings/assistant/key"' not in page and "Admins set the keys" in page   # operators: read-only
    r = op.post("/settings/assistant/restart", data={"csrf": csrf_of(page)})
    assert r.status_code == 303
    op_id = r.headers["location"].split("/")[2]
    assert json.loads((w.s.ops_dir / "requests" / f"{op_id}.json").read_text())["op"] == "restart-assistant"
    assert hostops.dispatch(w.host) == [f"{op_id}: restart-assistant started"]
    assert hostops.run(w.host, op_id) == 0
    rec = Ledger(w.host).get(op_id)
    assert rec["state"] == "succeeded" and "it read config.yaml at" in rec["health"]
    assert ["systemctl", "restart", UNIT] in w.host.system.calls
    assert "The assistant runs with this config.yaml" in op.get("/settings/assistant/").text


def test_a_key_is_written_for_the_run_user_alone_and_comes_back_nowhere(w, capsys):
    admin = signed_in(w.s, "owner@example.com")
    page = admin.get("/settings/assistant/").text
    assert 'type="password"' in page and "Not set" in page
    r = admin.post("/settings/assistant/key", data={"csrf": csrf_of(page), "name": "ANTHROPIC_API_KEY",
                                                    "value": SENTINEL, "action": "set"})
    assert r.status_code == 303 and "ANTHROPIC_API_KEY set" in r.headers["location"].replace("+", " ")
    env = w.data / "assistant.env"
    assert f"ANTHROPIC_API_KEY={SENTINEL}" in env.read_text() and "YOUTRACK_TOKEN=perm:kept-as-it-was" in env.read_text()
    assert env.stat().st_mode & 0o777 == 0o600
    after = admin.get("/settings/assistant/").text
    assert "Not set" in after and ">Set<" in after and "owner@example.com" in after
    # Nowhere else: the page, config.yaml, History and the journal (console.db), the host's ledger, the console's copy
    # of it, the requests folder, what dispatch printed.
    w.beat()
    journal = admin.get("/users/journal/").text
    assert "ANTHROPIC_API_KEY" in journal                  # who set which key is recorded; the key itself is not
    everywhere = [after, admin.get("/operations/").text, journal]
    for path in [w.config, w.s.db_path, *w.s.db_path.parent.glob("console.db*"), *(w.data / "ledger").rglob("*"),
                 *w.s.ops_dir.rglob("*")]:
        if path.is_file():
            everywhere.append(path.read_bytes().decode("utf-8", "replace"))
    everywhere.append(capsys.readouterr().out)
    assert not [t for t in everywhere if SENTINEL in t]
    assert list((w.s.ops_dir / "requests").glob("*.json")) == []
    keys = json.loads((w.s.ops_dir / "assistant-keys.json").read_text())["keys"]
    assert keys["ANTHROPIC_API_KEY"]["set"] and keys["YOUTRACK_TOKEN"]["set"] and not keys["SLACK_BOT_TOKEN"]["set"]


def test_a_replaced_slack_token_reconnects_her_and_a_removed_key_is_gone(w):
    admin = signed_in(w.s, "owner@example.com")
    csrf = csrf_of(admin.get("/settings/assistant/").text)
    admin.post("/settings/assistant/key", data={"csrf": csrf, "name": "SLACK_BOT_TOKEN", "value": "xoxb-1111-2222-abcdef",
                                                "action": "set"})
    assert ["systemctl", "restart", UNIT] in w.host.system.calls          # Slack reads its tokens when it connects
    r = admin.post("/settings/assistant/key", data={"csrf": csrf, "name": "YOUTRACK_TOKEN", "value": "", "action": "remove"})
    assert "YOUTRACK_TOKEN removed" in r.headers["location"].replace("+", " ")
    text = (w.data / "assistant.env").read_text()
    assert "YOUTRACK_TOKEN" not in text and "SLACK_BOT_TOKEN=xoxb-1111-2222-abcdef" in text and "# made by setup" in text


@pytest.mark.parametrize("value", ["not-a-key-at-all-123", "sk-ant-with spaces in it", "sk-ant-\nYOUTRACK_TOKEN=x"])
def test_what_is_not_the_key_is_refused_and_nothing_is_written(w, value):
    admin = signed_in(w.s, "owner@example.com")
    before = (w.data / "assistant.env").read_text()
    r = admin.post("/settings/assistant/key", data={"csrf": csrf_of(admin.get("/settings/assistant/").text),
                                                    "name": "ANTHROPIC_API_KEY", "value": value, "action": "set"})
    assert "error=Not+saved" in r.headers["location"]
    assert (w.data / "assistant.env").read_text() == before and list((w.s.ops_dir / "requests").glob("*")) == []
    # The host checks again, whatever reaches it.
    (w.s.ops_dir / "requests" / f"{'a' * 32}.json").write_text(json.dumps(
        {"id": "a" * 32, "op": "assistant-key", "name": "ANTHROPIC_API_KEY", "value": value, "requested_by": "x"}))
    hostops.dispatch(w.host)
    assert Ledger(w.host).get("a" * 32)["state"] == "refused" and (w.data / "assistant.env").read_text() == before


def test_only_admins_set_keys_and_never_from_another_page(w):
    op = person(w.s, "op@example.com", {"operator"})
    page = op.get("/settings/assistant/").text
    r = op.post("/settings/assistant/key", data={"csrf": csrf_of(page), "name": "ANTHROPIC_API_KEY", "value": SENTINEL})
    assert r.status_code == 403
    admin = signed_in(w.s, "owner@example.com")
    r = admin.post("/settings/assistant/key", data={"name": "ANTHROPIC_API_KEY", "value": SENTINEL})   # no CSRF
    assert r.status_code == 403
    viewer = person(w.s, "viewer@example.com", set())
    assert viewer.get("/settings/assistant/").status_code == 403
    assert SENTINEL not in (w.data / "assistant.env").read_text() and list((w.s.ops_dir / "requests").glob("*")) == []


def test_without_the_hosts_executor_no_key_is_written_anywhere(w):
    (w.s.ops_dir / "host.json").write_text(json.dumps({"at": iso(600), "services": {}}))
    admin = signed_in(w.s, "owner@example.com")
    r = admin.post("/settings/assistant/key", data={"csrf": csrf_of(admin.get("/settings/assistant/").text),
                                                    "name": "ANTHROPIC_API_KEY", "value": SENTINEL, "action": "set"})
    assert "error=Not+saved" in r.headers["location"] and list((w.s.ops_dir / "requests").glob("*")) == []


def test_root_writes_only_the_data_folders_own_assistant_env(w):
    w.host.system.__class__ = type("Elsewhere", (System,), {"__call__": lambda self, args, **kw: (
        subprocess.CompletedProcess(args, 0, "ASSISTANT_SECRETS=/etc/elsewhere.env\n", "")
        if args[-2:] == ["console", "env"] else System.__call__(self, args, **kw))})
    (w.s.ops_dir / "requests" / f"{'b' * 32}.json").write_text(json.dumps(
        {"id": "b" * 32, "op": "assistant-key", "name": "ANTHROPIC_API_KEY", "value": SENTINEL, "requested_by": "x"}))
    hostops.dispatch(w.host)
    rec = Ledger(w.host).get("b" * 32)
    assert rec["state"] == "refused" and "set the keys on the server" in rec["result"]
    assert SENTINEL not in (w.data / "assistant.env").read_text()


def test_her_settings_go_through_settings_check_and_the_risky_ones_are_locked(w):
    admin = signed_in(w.s, "owner@example.com")
    page = admin.get("/settings/assistant/edit/").text
    assert 'name="assistant.key_owner"' in page and 'name="assistant.allowed_users"' in page
    for locked in ("claude_bin", "home", "secrets", "persona_file", "knowledge_file", "port"):
        tag = page.split(f'name="assistant.{locked}"', 1)[1].split(">", 1)[0]
        assert "readonly" in tag, locked
    text = w.config.read_text()
    for key, value in (("claude_bin", "/tmp/evil"), ("persona_file", "~/arceus/data/secrets.env")):
        raw = yaml.safe_load(text)
        raw["assistant"][key] = value
        assert "set on the server" in " ".join(sf.check(yaml.safe_dump(raw), text))
    raw = yaml.safe_load(text)
    raw["assistant"]["ticket_engine"] = {"bridge": "sudo -n /bin/sh"}
    assert "set on the server" in " ".join(sf.check(yaml.safe_dump(raw), text))
    # A field that is hers to change: the same Check → Save, back to her page, saying a restart applies it.
    form = page.split('class="card settings-form"', 1)[1]
    sha = form.split('name="sha" value="', 1)[1].split('"', 1)[0]
    from tests.test_console_settings import form_values
    values = dict(form_values(page))
    values["assistant.key_owner"] = "Acme, Anthropic org Factory"
    preview = admin.post("/settings/assistant/edit/", data={**values, "csrf": csrf_of(page)})
    assert preview.status_code == 200 and "key_owner" in preview.text
    new_text = preview.text.split('name="text"', 1)[1].split(">", 1)[1].split("</textarea>", 1)[0]
    import html as h
    r = admin.post("/settings/save", data={"csrf": csrf_of(preview.text), "text": h.unescape(new_text), "sha": sha,
                                           "origin": "/settings/assistant/", "note": "who pays"})
    assert r.status_code == 303 and r.headers["location"].startswith("/settings/assistant/?notice=Saved")
    assert "Anthropic org Factory" in w.config.read_text()
    assert admin.get("/settings/history/").text.count("who pays") >= 1


def test_who_pays_is_what_the_running_process_uses_and_a_saved_change_waits_for_the_restart(w):
    # Saved: the team's key (the fixture). Running: still on the subscription, from before the save.
    status(w.data, sha="before-the-save", claude={"auth": "subscription", "last": "ok", "at": iso()})
    page = signed_in(w.s, "owner@example.com").get("/settings/assistant/").text
    assert "She still runs on the Claude subscription" in page
    assert "Saved:</strong> Anthropic API key" in page
    tile = page.split("<h2>Chat</h2>", 1)[1].split("</section>", 1)[0]
    assert "Claude subscription" in tile and "API key" not in tile.split("Saved:")[0]
    # The other way: one person's own assistant saved back to a subscription, still running on the key.
    raw = yaml.safe_load(w.config.read_text())
    raw["assistant"].update(auth="subscription", allowed_users=["lead@example.com"])
    raw["assistant"].pop("slack")
    w.config.write_text(yaml.safe_dump(raw, allow_unicode=True))
    status(w.data, sha="before-the-save", claude={"auth": "api_key", "last": "ok", "at": iso()})
    page = signed_in(w.s, "owner@example.com").get("/settings/assistant/").text
    assert "She still runs on the Anthropic API key" in page and "Claude subscription: that applies from her next start" in page
    tile = page.split("<h2>Chat</h2>", 1)[1].split("</section>", 1)[0]
    assert "Anthropic API key" in tile.split("Saved:")[0] and "subscription" not in tile.split("Saved:")[0]
    # Restarted with it: no longer pending.
    status(w.data, sha=sha_of(w.config), claude={"auth": "subscription", "last": None, "at": None},
           slack={"state": "off", "at": iso()})                     # she was restarted with these settings: no Slack
    page = signed_in(w.s, "owner@example.com").get("/settings/assistant/").text
    assert "She still runs on" not in page and "Saved:</strong>" not in page


def test_an_hour_old_word_is_said_as_such_never_as_now(w):
    status(w.data, sha_of(w.config), age=3600, beat_age=3600)       # her status: an hour old
    (w.s.ops_dir / "host.json").write_text(json.dumps({"at": iso(3600), "services": {
        "assistant": {"active": "active", "since": "an hour ago", "installed": True}}}))
    page = signed_in(w.s, "owner@example.com").get("/settings/assistant/").text
    service = page.split("<h2>Service</h2>", 1)[1].split("</section>", 1)[0]
    assert "Unknown now" in service and "last seen active" in service and "Running" not in service
    assert "The assistant runs with this config.yaml" not in page and "last written 1 h 00 min ago: she is not keeping it now" in page
    slack = page.split("<h2>Slack</h2>", 1)[1].split("</section>", 1)[0]
    assert "Unknown now" in slack and "Connected" not in slack and "last said connected" in slack
    assert "Unknown now" in page.split("<h2>Chat</h2>", 1)[1].split("</section>", 1)[0]


def test_a_stopped_service_is_said_and_nothing_claims_it_runs_with_the_file(w):
    status(w.data, sha_of(w.config), age=30)
    (w.s.ops_dir / "host.json").write_text(json.dumps({"at": iso(), "services": {
        "assistant": {"active": "failed", "sub": "failed", "status": "1", "installed": True}}}))
    page = signed_in(w.s, "owner@example.com").get("/settings/assistant/").text
    service = page.split("<h2>Service</h2>", 1)[1].split("</section>", 1)[0]
    assert "Failed" in service and "exit status 1" in service
    assert "The assistant runs with this config.yaml" not in page and "she is not running (failed)" in page
    assert "Connected" not in page.split("<h2>Slack</h2>", 1)[1].split("</section>", 1)[0]


def test_a_dropped_slack_connection_shows_as_lost(w):
    status(w.data, sha_of(w.config), slack={"state": "reconnecting", "team": "Acme", "bot": "olena",
                                            "error": "the connection to Slack dropped; reconnecting", "at": iso()})
    slack = signed_in(w.s, "owner@example.com").get("/settings/assistant/").text.split("<h2>Slack</h2>", 1)[1]
    assert "Connection lost" in slack.split("</section>", 1)[0] and "reconnecting since" in slack


def tile(page: str, name: str) -> str:
    return page.split(f"<h2>{name}</h2>", 1)[1].split("</section>", 1)[0]


def save(w, **change) -> None:
    raw = yaml.safe_load(w.config.read_text())
    for k, v in change.items():
        if v is None:
            raw["assistant"].pop(k, None)
        else:
            raw["assistant"][k] = v
    w.config.write_text(yaml.safe_dump(raw, allow_unicode=True))


def running(w, web: bool, slack: str) -> None:
    """Her status as she writes it after a start with the settings now in config.yaml (sha) or older ones."""
    sha = sha_of(w.config)
    status(w.data, sha, slack={"state": slack, "team": "Acme", "bot": "olena", "at": iso()})
    st = json.loads((w.data / "assistant-status.json").read_text())
    st["web"] = web
    (w.data / "assistant-status.json").write_text(json.dumps(st))


def test_slack_and_the_web_chat_are_what_runs_and_a_saved_switch_waits_for_the_restart(w):
    admin = signed_in(w.s, "owner@example.com")
    running(w, web=True, slack="connected")
    save(w, web=False, slack=None)                                   # saved: off and out of Slack, not applied yet
    page = admin.get("/settings/assistant/").text
    assert "Connected" in tile(page, "Slack") and "Saved:</strong> out of Slack" in tile(page, "Slack")
    assert "✓</span> On" in tile(page, "Web chat") and "Saved:</strong> off" in tile(page, "Web chat")
    running(w, web=False, slack="off")                               # restarted with them
    page = admin.get("/settings/assistant/").text
    assert "Off" in tile(page, "Slack") and "Saved:" not in tile(page, "Slack")
    assert "Off" in tile(page, "Web chat") and "Saved:" not in tile(page, "Web chat")
    save(w, web=True, slack={"read_channels": ["C0AAAAAAA1"]})       # and back on, saved
    page = admin.get("/settings/assistant/").text
    assert "Off" in tile(page, "Slack") and "Saved:</strong> in Slack" in tile(page, "Slack")
    assert "Off" in tile(page, "Web chat") and "Saved:</strong> on" in tile(page, "Web chat")
    running(w, web=True, slack="connecting")                         # applied
    page = admin.get("/settings/assistant/").text
    assert "Connecting" in tile(page, "Slack") and "✓</span> On" in tile(page, "Web chat")
    assert "Saved:" not in tile(page, "Slack") + tile(page, "Web chat")


def test_an_old_word_on_slack_and_the_web_chat_is_unknown_now(w):
    status(w.data, sha_of(w.config), age=3600, beat_age=3600)
    page = signed_in(w.s, "owner@example.com").get("/settings/assistant/").text
    assert "Unknown now" in tile(page, "Slack") and "Unknown now" in tile(page, "Web chat")
    assert "On" not in tile(page, "Web chat")


def web_tile(w) -> tuple[str, str]:
    page = signed_in(w.s, "owner@example.com").get("/settings/assistant/").text
    return page, tile(page, "Web chat")


def test_turning_the_web_chat_off_is_applied_by_setup_not_by_a_restart_alone(w):
    running(w, web=True, slack="connected")
    page, t = web_tile(w)
    assert "✓</span> On" in t and "The assistant runs with this config.yaml" in page
    save(w, web=False)                                               # saved
    page, t = web_tile(w)
    assert "✓</span> On" in t and "Saved:</strong> off, from her next start and setup" in t
    assert "It still runs with the earlier settings" in page and "run setup as well" in page
    running(w, web=False, slack="connected")                         # restarted: she serves no chat, nginx still routes
    w.beat()
    page, t = web_tile(w)
    assert "Off" in t and "nginx still routes /assistant/ here" in t
    assert "The assistant runs with this config.yaml" not in page and "nginx still routes /assistant/ to port 8190" in page
    assert 'href="/operations/new/setup"' in page                    # admins: the way to apply it
    nginx(w.data / "nginx.conf", None)                               # setup ran: the route is gone
    w.beat()
    page, t = web_tile(w)
    assert "Off" in t and "nginx" not in t and "Saved:" not in t and "The assistant runs with this config.yaml" in page


def test_turning_it_on_needs_setup_for_the_route_and_says_so_until_then(w):
    save(w, web=False)
    nginx(w.data / "nginx.conf", None)
    running(w, web=False, slack="connected")
    w.beat()
    save(w, web=True)                                                # saved on
    page, t = web_tile(w)
    assert "Off" in t and "Saved:</strong> on, from her next start and setup" in t
    running(w, web=True, slack="connected")                          # restarted: she serves it, nginx has no route
    page, t = web_tile(w)
    assert "Not reachable" in t and "nginx has no route to her" in t and "On<" not in t
    assert "nginx has no route to her" in page and "The assistant runs with this config.yaml" not in page
    op = person(w.s, "op@example.com", {"operator"})
    assert "An admin runs setup" in op.get("/settings/assistant/").text   # operators cannot run setup
    nginx(w.data / "nginx.conf", 8190)                               # setup ran
    w.beat()
    page, t = web_tile(w)
    assert "✓</span> On" in t and "The assistant runs with this config.yaml" in page and "Saved:" not in t


def test_while_the_host_cannot_say_what_nginx_routes_the_chat_is_not_claimed_reachable(w):
    running(w, web=True, slack="connected")
    (w.s.ops_dir / "host.json").write_text(json.dumps({"at": iso(3600), "services": {}, "routes": {
        "assistant": {"known": True, "port": 8190}}}))
    page, t = web_tile(w)
    assert "On in her" in t and "not known" in t and "✓</span> On" not in t


def test_the_host_reads_the_route_setup_wrote(tmp_path):
    host = Host(data=tmp_path, setup=tmp_path / "s", ledger=tmp_path / "l", console_nginx=tmp_path / "nginx.conf")
    assert hostops.assistant_route(host)["known"] is False
    nginx(tmp_path / "nginx.conf", 8191)
    assert hostops.assistant_route(host) == {"known": True, "port": 8191}
    nginx(tmp_path / "nginx.conf", None)
    assert hostops.assistant_route(host) == {"known": True, "port": None}
