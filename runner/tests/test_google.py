"""Google for the agent: the read-only Google MCP server setup puts into bulava.mcp_config with a service account's
key. An unattended run may call it, and the agent's own tools never read the key."""

from __future__ import annotations

import json
from pathlib import Path

from arceus_runner.config import Config
from arceus_runner.engine import Engine, google_key

from .conftest import example_raw


def _mcp(tmp_path: Path, with_google: bool = True) -> Path:
    servers = {"browser": {"command": "/opt/agent-tools/bin/chrome-devtools-mcp", "args": ["--headless"]}}
    if with_google:
        servers["google"] = {"command": "/opt/agent-tools/workspace-mcp/bin/workspace-mcp", "args": ["--read-only"],
                             "env": {"GOOGLE_SERVICE_ACCOUNT_KEY_FILE": str(tmp_path / "google" / "service-account.json"),
                                     "USER_GOOGLE_EMAIL": "agent@proj.iam.gserviceaccount.com"}}
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


def _engine(tmp_path: Path, mcp: Path) -> Engine:
    raw = example_raw()
    raw["bulava"]["mcp_config"] = str(mcp)
    raw["paths"] = {"secrets": str(tmp_path / "secrets.env"), "bulava_env": str(tmp_path / "bulava.env"),
                    "db": str(tmp_path / "runner.db")}
    return Engine(Config.model_validate(raw))


def test_with_the_google_server_an_unattended_run_may_use_it_and_its_tools_never_read_its_key(tmp_path):
    engine = _engine(tmp_path, _mcp(tmp_path))
    assert engine.env()["SUPERVISOR_ALLOW_TOOLS"] == "google"
    assert tmp_path / "google" in engine.secret_paths()
    assert str(tmp_path / "google") in engine.env()["SUPERVISOR_SERVER_EXTRA_SECRET_PATHS"].split()


def test_without_it_nothing_changes(tmp_path):
    engine = _engine(tmp_path, _mcp(tmp_path, with_google=False))
    assert "SUPERVISOR_ALLOW_TOOLS" not in engine.env()
    assert len(engine.secret_paths()) == 4
    assert google_key(tmp_path / "missing.json") is None and google_key(None) is None
