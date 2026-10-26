from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from arceus_runner.config import Config, Secrets

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"

YT_TOKEN = "perm:yt-secret-token-123"
GL_TOKEN = "glpat-secret-token-456"


def example_raw() -> dict:
    return yaml.safe_load(EXAMPLE.read_text())


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    raw = example_raw()
    raw.pop("slack")
    raw["gitlab"]["exclude_repos"] = ["acme-infra"]
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
    return Secrets(youtrack=YT_TOKEN, gitlab=GL_TOKEN, slack=None)


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Recorder:
    """A MockTransport handler that records requests and dispatches to a function."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def json_response(data, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(data).encode(), headers={"content-type": "application/json",
                                                                               **(headers or {})})


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
