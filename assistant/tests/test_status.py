"""What the console's Assistant page reads of her: assistant-status.json beside config.yaml, written by herself."""

import hashlib
import json

from assistant.brain import BAD_KEY, NO_KEY
from assistant.status import Status, outcome


def test_the_status_names_the_config_she_read_and_says_slack_and_claude_without_a_secret(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("project: shop\n")
    s = Status(config, "api_key", web=True, slack_on=True)
    path = tmp_path / "assistant-status.json"
    data = json.loads(path.read_text())
    assert data["config_sha256"] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert data["slack"]["state"] == "connecting" and data["claude"] == {"provider": "claude", "auth": "api_key", "last": None, "at": None}
    s.slack("failed", error="SlackApiError: invalid_auth for xoxb-1234-5678-abcdefghijkl")
    s.slack("connected", team="Acme", bot="olena")
    s.claude(outcome("error", BAD_KEY))
    data = json.loads(path.read_text())
    assert data["slack"]["team"] == "Acme" and data["claude"]["last"] == "bad_key"
    s.slack("failed", error="token xapp-1-A0-123456789-abcdef refused")
    assert "xapp-…" in path.read_text() and "123456789" not in path.read_text()
    assert outcome("error", NO_KEY) == "no_key" and outcome("done", "так") == "ok" and outcome("error", "x") == "error"
    assert not list(tmp_path.glob(".assistant-status*"))       # written whole, by a rename


def test_without_a_config_file_nothing_is_written(tmp_path):
    Status(None, "subscription", web=False, slack_on=False).beat()
    assert list(tmp_path.iterdir()) == []


def test_a_dropped_slack_connection_is_said_until_slacks_client_is_back(tmp_path):
    """The bridge as it runs: connected once, then the socket drops and comes back. The console reads each state."""
    import asyncio
    from types import SimpleNamespace

    from assistant.slack import SlackBridge, SlackConfig

    class Socket:
        up = True

        async def is_connected(self):
            return self.up

    config = tmp_path / "config.yaml"
    config.write_text("x: 1\n")
    app = SimpleNamespace(status=Status(config, "api_key", web=False, slack_on=True), after_turn=[])
    bridge = SlackBridge(app, SlackConfig(tmp_path / "assistant.env"), web=object(), socket=Socket())
    bridge.team, bridge._names = "T1", ("Acme", "olena")
    app.status.slack("connected", team="Acme", bot="olena")
    read = lambda: json.loads((tmp_path / "assistant-status.json").read_text())["slack"]  # noqa: E731

    async def go():
        await bridge.watch()
        assert read()["state"] == "connected"
        bridge.socket.up = False
        await bridge.watch()
        lost = read()
        assert lost["state"] == "reconnecting" and lost["team"] == "Acme" and "dropped" in lost["error"]
        await bridge.watch()
        assert read()["at"] == lost["at"]                      # still the moment it dropped
        bridge.socket.up = True
        await bridge.watch()
        assert read()["state"] == "connected" and read()["error"] == ""
    asyncio.run(go())


def test_the_beat_checks_slack_and_keeps_the_status_fresh(tmp_path, monkeypatch):
    import asyncio
    from assistant import server
    from tests.test_chat import cfg

    s = cfg(tmp_path)
    s.config_path = tmp_path / "config.yaml"
    s.config_path.write_text("x: 1\n")
    app = server.App(s)
    watched = []

    class Bridge:
        async def watch(self):
            watched.append(1)
    app.slack = Bridge()
    before = json.loads((tmp_path / "assistant-status.json").read_text())["at"]

    async def go():
        task = asyncio.create_task(app._beat_loop(every=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
    import time
    time.sleep(1.1)                                            # so the next write is a later second
    asyncio.run(go())
    assert watched and json.loads((tmp_path / "assistant-status.json").read_text())["at"] > before


def test_with_the_web_chat_off_nothing_of_it_answers_over_http_whatever_nginx_still_routes(tmp_path):
    from starlette.testclient import TestClient

    from assistant import server
    from tests.test_chat import H, cfg

    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    on = cfg(tmp_path / "on")
    with TestClient(server.build(on)) as client:
        assert client.get("/assistant/", headers=H).status_code == 200
        assert client.get("/assistant/api/conversations", headers=H).status_code == 200
    off = cfg(tmp_path / "off")
    off.web = False
    with TestClient(server.build(off)) as client:
        for path in ("/assistant/", "/assistant/api/me", "/assistant/api/conversations", "/assistant/static/app.js"):
            r = client.get(path, headers=H)
            assert r.status_code == 404 and "вимкнено" in r.text, path
        assert client.post("/assistant/api/conversations", headers=H, json={}).status_code == 404
