"""HTTP access to one external service with its own pause rules.

Each service (the issue tracker, the code host, Slack) has its own client and its own pause,
so the code host being down does not stop the tracker's polling and vice versa. Providers of
integrations.py send their requests through it.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx

from arceus_runner.config import HttpConfig

AUTH_RETRY_SECONDS = 600


class ServiceError(Exception):
    def __init__(self, service: str, message: str, status: int | None = None):
        self.service = service
        self.status = status
        super().__init__(f"{service}: {message}")


class ServicePaused(ServiceError):
    """The service is in a pause; the request was not sent."""


class TransientError(ServiceError):
    """5xx, 429, timeout, broken connection: retry after a pause."""


class AuthError(ServiceError):
    """401/403: the token is wrong or lacks rights; retrying will not help."""


class NotFound(ServiceError):
    """404: the object is gone. Not a reason to pause the service."""


class BadRequest(ServiceError):
    """400/422 and other 4xx: our request is wrong."""


@dataclass
class ServiceState:
    failures: int = 0
    paused_until: float = 0.0
    auth_failed: bool = False
    last_error: str | None = None
    # A slow but successful answer asks the caller to skip one regular poll.
    slow: bool = False
    pause_seconds: float = 0.0


def _retry_after(resp: httpx.Response, now_wall: float) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(raw).timestamp() - now_wall)
    except (TypeError, ValueError):
        return None


@dataclass
class ServiceClient:
    name: str
    base_url: str
    headers: dict[str, str]
    http: HttpConfig
    transport: httpx.BaseTransport | None = None
    clock: Callable[[], float] = time.monotonic
    rng: Callable[[], float] = random.random
    state: ServiceState = field(default_factory=ServiceState)

    def __post_init__(self) -> None:
        timeout = httpx.Timeout(
            self.http.read_timeout.total_seconds(),
            connect=self.http.connect_timeout.total_seconds(),
        )
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=self.headers,
            timeout=timeout,
            transport=self.transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def begin_poll(self) -> None:
        """Start a poll cycle: slowness is judged over all requests of the cycle, not the last one."""
        self.state.slow = False

    def available(self) -> bool:
        return self.clock() >= self.state.paused_until

    def seconds_left(self) -> float:
        return max(0.0, self.state.paused_until - self.clock())

    def _pause_for(self, seconds: float, reason: str) -> None:
        self.state.paused_until = self.clock() + seconds
        self.state.pause_seconds = seconds
        self.state.last_error = reason

    def _backoff(self) -> float:
        b = self.http.backoff
        base = b.start.total_seconds() * (2 ** (self.state.failures - 1))
        base = min(base, b.max.total_seconds())
        return base * (1 + b.jitter * (2 * self.rng() - 1))

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.available():
            raise ServicePaused(self.name, f"paused for {self.seconds_left():.0f}s more: {self.state.last_error}")
        started = self.clock()
        what = f"{method} {path}"
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            self._transient(f"{what}: timeout", None)
            raise TransientError(self.name, f"{what}: timeout") from None
        except httpx.TransportError as e:
            reason = f"{what}: {type(e).__name__}: {self._scrub(str(e))[:200]}"
            self._transient(reason, None)
            raise TransientError(self.name, reason) from None

        code = resp.status_code
        if code == 429 or code >= 500:
            reason = f"{what}: HTTP {code}"
            self._transient(reason, resp if code in (429, 503) else None)
            raise TransientError(self.name, reason, code)
        if code in (401, 403):
            reason = f"{what}: HTTP {code} (token invalid or lacks rights)"
            self.state.auth_failed = True
            self.state.failures = 0
            self._pause_for(AUTH_RETRY_SECONDS, reason)
            raise AuthError(self.name, reason, code)

        # Any answer that is not an outage resets the pause.
        self.state.failures = 0
        self.state.auth_failed = False
        self.state.paused_until = 0.0
        self.state.pause_seconds = 0.0
        self.state.last_error = None
        if (self.clock() - started) > self.http.slow_response.total_seconds():
            self.state.slow = True

        if code == 404:
            raise NotFound(self.name, f"{what}: not found", code)
        if code >= 400:
            raise BadRequest(self.name, f"{what}: HTTP {code}: {self._scrub(resp.text[:500])}", code)
        if 300 <= code < 400:
            raise BadRequest(self.name, f"{what}: unexpected redirect to {resp.headers.get('location')}", code)
        return resp

    def _scrub(self, text: str) -> str:
        """Error bodies go to logs and terminals: never let our own credentials ride along."""
        for value in self.headers.values():
            for secret in {value, value.split(" ", 1)[-1]}:
                if len(secret) >= 8:
                    text = text.replace(secret, "<redacted>")
        return text

    def _transient(self, reason: str, resp: httpx.Response | None) -> None:
        self.state.failures += 1
        delay = self._backoff()
        if resp is not None:
            ra = _retry_after(resp, time.time())
            if ra is not None and ra > delay:
                delay = ra
        self._pause_for(delay, reason)

    def get_json(self, path: str, params: dict | None = None) -> object:
        return self.request("GET", path, params=params).json()
