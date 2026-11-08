"""Links into the tracker and the code host, worked out the way the runner does (integrations.py), with no token and
no network: the providers are built over a transport that refuses every request. Only their URL helpers are used."""

from __future__ import annotations

import httpx

from arceus_runner.config import Config
from arceus_runner.integrations import code_host_type, tracker_type


def _refuse(request: httpx.Request) -> httpx.Response:
    raise RuntimeError(f"the console makes no requests to {request.url.host}")


_cache: dict[int, tuple] = {}


def providers(cfg: Config):
    """(tracker, code host) for building links, one pair per config object."""
    key = id(cfg)
    if key not in _cache:
        transport = httpx.MockTransport(_refuse)
        tracker = tracker_type(cfg.tracker.type).build(cfg.tracker, "-", cfg.http, transport)
        host = code_host_type(cfg.code_host.type).build(cfg.code_host, "-", cfg.http, transport)
        _cache.clear()  # a new config replaces the old pair
        _cache[key] = (cfg, tracker, host)
    _, tracker, host = _cache[key]
    return tracker, host


def issue_url(cfg: Config):
    tracker, _ = providers(cfg)
    return tracker.issue_url
