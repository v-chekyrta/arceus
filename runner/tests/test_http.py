from __future__ import annotations

import httpx
import pytest

from arceus_runner.config import HttpConfig
from arceus_runner.http import (
    AUTH_RETRY_SECONDS,
    AuthError,
    BadRequest,
    NotFound,
    ServiceClient,
    ServicePaused,
    TransientError,
)

from .conftest import FakeClock, json_response


def client(handler, clock: FakeClock, rng=lambda: 0.5) -> ServiceClient:
    return ServiceClient("svc", "https://svc.example", {}, HttpConfig(), transport=httpx.MockTransport(handler),
                         clock=clock, rng=rng)


def test_backoff_doubles_up_to_max_and_success_resets():
    clock = FakeClock()
    status = {"code": 502}
    c = client(lambda r: json_response({}, status["code"]), clock)
    pauses = []
    for _ in range(6):
        with pytest.raises(TransientError):
            c.request("GET", "/x")
        pauses.append(round(c.seconds_left()))
        with pytest.raises(ServicePaused):
            c.request("GET", "/x")  # nothing is sent during a pause
        clock.advance(c.seconds_left())
    assert pauses == [60, 120, 240, 480, 900, 900]

    status["code"] = 200
    c.request("GET", "/x")
    assert c.state.failures == 0 and c.available()
    status["code"] = 500
    with pytest.raises(TransientError):
        c.request("GET", "/x")
    assert round(c.seconds_left()) == 60


def test_jitter_stays_within_twenty_percent():
    clock = FakeClock()
    lo = client(lambda r: json_response({}, 500), clock, rng=lambda: 0.0)
    hi = client(lambda r: json_response({}, 500), clock, rng=lambda: 0.999999)
    for c in (lo, hi):
        with pytest.raises(TransientError):
            c.request("GET", "/x")
    assert round(lo.seconds_left()) == 48 and round(hi.seconds_left()) == 72


def test_retry_after_wins_when_longer():
    clock = FakeClock()
    c = client(lambda r: json_response({}, 503, {"Retry-After": "300"}), clock)
    with pytest.raises(TransientError):
        c.request("GET", "/x")
    assert round(c.seconds_left()) == 300

    c2 = client(lambda r: json_response({}, 429, {"Retry-After": "5"}), clock)
    with pytest.raises(TransientError):
        c2.request("GET", "/x")
    assert round(c2.seconds_left()) == 60


def test_timeout_is_transient():
    def boom(request):
        raise httpx.ConnectTimeout("slow", request=request)

    c = client(boom, FakeClock())
    with pytest.raises(TransientError, match="timeout"):
        c.request("GET", "/x")
    assert not c.available()


def test_auth_failure_waits_ten_minutes():
    clock = FakeClock()
    sent = []

    def handler(r):
        sent.append(r)
        return json_response({}, 401)

    c = client(handler, clock)
    with pytest.raises(AuthError):
        c.request("GET", "/x")
    assert c.state.auth_failed
    clock.advance(AUTH_RETRY_SECONDS - 1)
    with pytest.raises(ServicePaused):
        c.request("GET", "/x")
    assert len(sent) == 1
    clock.advance(1)
    with pytest.raises(AuthError):
        c.request("GET", "/x")
    assert len(sent) == 2


def test_not_found_and_bad_request_do_not_pause():
    c = client(lambda r: json_response({"error": "x"}, 404 if r.url.path == "/gone" else 422), FakeClock())
    with pytest.raises(NotFound):
        c.request("GET", "/gone")
    with pytest.raises(BadRequest):
        c.request("GET", "/bad")
    assert c.available()


def test_slowness_is_kept_for_the_whole_poll():
    clock = FakeClock()

    def handler(r):
        clock.advance(12 if r.url.path == "/slow" else 0.1)
        return json_response([])

    c = client(handler, clock)
    c.begin_poll()
    c.request("GET", "/slow")
    c.request("GET", "/fast")  # a fast follow-up must not erase it
    assert c.state.slow
    c.begin_poll()
    c.request("GET", "/fast")
    assert not c.state.slow
