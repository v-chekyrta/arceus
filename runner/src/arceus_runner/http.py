"""HTTP access to one external service with its own pause rules.

Each service (the issue tracker, the code host, Slack) has its own client and its own pause,
so the code host being down does not stop the tracker's polling and vice versa. Providers of
integrations.py send their requests through it.
"""

from __future__ import annotations

import random
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx

from arceus_runner.config import HttpConfig

AUTH_RETRY_SECONDS = 600
ERROR_BODY = 2048  # bytes of a download's error body read for its message


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


class TooLarge(ServiceError):
    """A download went past its limit of bytes or of time; what came of it is dropped."""


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


def _raw(resp: httpx.Response):
    """The body's bytes as they come off the network. A response that holds its body already (one built in memory,
    as a test's transport does) has no stream left: it is that body, in one piece."""
    return [resp.content] if resp.is_stream_consumed else resp.iter_raw()


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
        self._answered(resp, what, started, lambda: resp.text)
        return resp

    def download(self, url: str, *, what: str, limit: int, seconds: float) -> bytes:
        """GET `url` whole, at most `limit` bytes in at most `seconds`: read as it streams, so a body larger than it
        says (or one that never ends) is cut off, not held in memory. `what` names the request in errors and the
        pause, in place of the URL, which may carry a signed token.

        The body is read as it comes off the network and decoded here, not by httpx: the clock is read after every
        piece that arrives, a compressed header that trickles in included, and no more is ever decompressed than
        the limit allows. The server is asked not to compress at all."""
        if not self.available():
            raise ServicePaused(self.name, f"paused for {self.seconds_left():.0f}s more: {self.state.last_error}")
        started = self.clock()
        try:
            # No single wait for the server longer than the whole download may take.
            read = min(self.http.read_timeout.total_seconds(), seconds)
            timeout = httpx.Timeout(read, connect=min(self.http.connect_timeout.total_seconds(), seconds))
            with self._client.stream("GET", url, timeout=timeout, headers={"Accept-Encoding": "identity"}) as resp:
                code = resp.status_code
                if code in (401, 403):
                    # One file the bot may not see (its comment's visibility) says nothing of the token: the
                    # service is not paused for it.
                    raise AuthError(self.name, f"{what}: HTTP {code} (the bot may not read it)", code)
                if 300 <= code < 400:
                    raise BadRequest(self.name, f"{what}: HTTP {code}, a redirect, not followed", code)
                self._answered(resp, what, started, lambda: self._error_body(resp, started, seconds))
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > limit:
                    raise TooLarge(self.name, f"{what}: {int(declared)} bytes, more than {limit}")
                encoding = resp.headers.get("content-encoding", "identity").strip().lower()
                if encoding not in ("", "identity", "gzip", "x-gzip", "deflate"):
                    raise BadRequest(self.name, f"{what}: sent as {encoding!r}, which the runner does not decode")
                # gzip or zlib, told apart by its header; None: the bytes as they are.
                inflate = zlib.decompressobj(zlib.MAX_WBITS | 32) if encoding not in ("", "identity") else None
                data = bytearray()
                try:
                    for raw in _raw(resp):
                        if self.clock() - started > seconds:
                            raise TooLarge(self.name, f"{what}: not done in {seconds:.0f}s")
                        if inflate is None:
                            data += raw
                        else:
                            data += inflate.decompress(raw, limit + 1 - len(data))
                            if inflate.unconsumed_tail:
                                raise TooLarge(self.name, f"{what}: more than {limit} bytes once decompressed")
                        if len(data) > limit:
                            raise TooLarge(self.name, f"{what}: more than {limit} bytes")
                    if inflate is not None:
                        data += inflate.flush(limit + 1 - len(data))
                        if len(data) > limit:
                            raise TooLarge(self.name, f"{what}: more than {limit} bytes once decompressed")
                        if not inflate.eof:
                            raise BadRequest(self.name, f"{what}: the compressed body ends early")
                except zlib.error:
                    raise BadRequest(self.name, f"{what}: the compressed body is broken") from None
                return bytes(data)
        except httpx.TimeoutException:
            self._transient(f"{what}: timeout", None)
            raise TransientError(self.name, f"{what}: timeout") from None
        except httpx.TransportError as e:
            reason = f"{what}: {type(e).__name__}"  # its words may quote the URL
            self._transient(reason, None)
            raise TransientError(self.name, reason) from None

    def _error_body(self, resp: httpx.Response, started: float, seconds: float) -> str:
        """The start of an error's body, for its message: never more than ERROR_BODY bytes, nor past the deadline."""
        body = bytearray()
        for chunk in _raw(resp):  # each piece as it arrives, so the clock is read after every one
            body += chunk
            if len(body) >= ERROR_BODY or self.clock() - started > seconds:
                break
        return bytes(body[:ERROR_BODY]).decode("utf-8", "replace")

    def _answered(self, resp: httpx.Response, what: str, started: float, body: Callable[[], str]) -> None:
        """The service answered: an outage or a refusal raises, anything else resets the pause. `body` gives the
        error's text, read only for a refusal that quotes it."""
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
            raise BadRequest(self.name, f"{what}: HTTP {code}: {self._scrub(body()[:500])}", code)
        if 300 <= code < 400:
            raise BadRequest(self.name, f"{what}: unexpected redirect to {resp.headers.get('location')}", code)

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
