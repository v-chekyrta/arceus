"""The engine and the classifier run the claude and codex setup pinned in /opt/agent-tools (linked from
/usr/local/bin), not a copy in ~/.local/bin that Claude's native installer keeps updating by itself."""

from __future__ import annotations

from arceus_runner.config import Config
from arceus_runner.engine import Engine
from arceus_runner.screening import classifier_env

from .conftest import example_raw


def _pinned_first(env: dict[str, str]) -> bool:
    dirs = env["PATH"].split(":")
    local = [d for d in dirs if d.endswith("/.local/bin")]
    return dirs[0] == "/usr/local/bin" and all(dirs.index(d) > 0 for d in local)


def test_the_engine_finds_the_pinned_tools_first_and_claude_does_not_update_itself(tmp_path):
    raw = example_raw()
    raw["paths"] = {"secrets": str(tmp_path / "secrets.env"), "bulava_env": str(tmp_path / "bulava.env"),
                    "db": str(tmp_path / "runner.db")}
    env = Engine(Config.model_validate(raw)).env()
    assert _pinned_first(env)
    assert env["DISABLE_AUTOUPDATER"] == "1"


def test_so_does_the_classifier():
    env = classifier_env({})
    assert _pinned_first(env)
    assert env["DISABLE_AUTOUPDATER"] == "1"
