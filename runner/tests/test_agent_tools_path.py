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


def test_bulava_env_gives_the_engine_only_credentials_and_proxies(cfg, tmp_path):
    """bulava.env is a file of the data folder: a variable that changes how a program starts (BASH_ENV, LD_PRELOAD,
    PATH, NODE_OPTIONS) would run code in every engine command, so only the names it is for pass."""
    from arceus_runner.engine import Engine
    env_file = tmp_path / "bulava.env"
    env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-real\nHTTPS_PROXY=http://proxy:3128\n"
                        "BASH_ENV=/tmp/payload.sh\nLD_PRELOAD=/tmp/x.so\nPATH=/tmp/bin\nNODE_OPTIONS=--require /tmp/x\n"
                        "SUPERVISOR_CLAUDE_MODEL=opus\n")
    cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"bulava_env": env_file})})
    env = Engine(cfg).token_env()
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-real", "HTTPS_PROXY": "http://proxy:3128"}
    full = Engine(cfg).env()
    assert "BASH_ENV" not in full and "LD_PRELOAD" not in full and full["PATH"] != "/tmp/bin"
