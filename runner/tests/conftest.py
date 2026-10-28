from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from arceus_runner.config import Config, Secrets
from arceus_runner.integrations import build_code_host, build_tracker

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"

YT_TOKEN = "perm:yt-secret-token-123"
GL_TOKEN = "glpat-secret-token-456"


def example_raw() -> dict:
    return yaml.safe_load(EXAMPLE.read_text())


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    raw = example_raw()
    raw.pop("slack")
    raw["code_host"]["exclude_repos"] = ["acme-infra"]
    raw["metrics"] = {"port": 0}
    raw["health"].pop("devstack_check", None)
    raw["paths"] = {
        "workspace": str(tmp_path / "workspace"),
        "bulava": str(tmp_path / "bulava"),
        "bulava_state": str(tmp_path / "state"),
        "secrets": str(tmp_path / "secrets.env"),
        "db": str(tmp_path / "runner.db"),
        "events": str(tmp_path / "events.jsonl"),
    }
    return Config.model_validate(raw)


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(tracker=YT_TOKEN, code_host=GL_TOKEN, slack=None)


def providers(cfg: Config, secrets: Secrets, fake_tracker, fake_host, clock=None):
    """The configured tracker and code host, talking to in-memory fakes over HTTP."""
    tracker = build_tracker(cfg, secrets, transport=httpx.MockTransport(fake_tracker))
    host = build_code_host(cfg, secrets, transport=httpx.MockTransport(fake_host))
    if clock is not None:
        tracker.client.clock = host.client.clock = clock
    return tracker, host


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def json_response(data, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(data).encode(), headers={"content-type": "application/json",
                                                                               **(headers or {})})


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def no_engine_tmux_server(monkeypatch):
    """The engine's tmux server of the machine running the tests (a dev Mac, the sandbox's keeper) is not
    the engine these tests talk to. The live scenarios, which do use it, put the real reading back."""
    monkeypatch.setattr("arceus_runner.health.engine_tmux_env", lambda socket="bulava": None)
