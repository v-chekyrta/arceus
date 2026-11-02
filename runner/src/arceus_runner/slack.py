"""Slack: the Web API for `check` (token, alerts channel) and the incoming webhook the runner reports through.

The webhook's URL is the credential: whoever has it can post to the channel, and Slack revokes one it finds leaked.
Nothing here ever puts it into an error, a log line or an event; httpx's own words about a failed request may quote
it, so they are reduced to their type.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from arceus_runner.config import HttpConfig
from arceus_runner.http import BadRequest, ServiceClient, _retry_after


class SlackApiError(BadRequest):
    pass


class Slack:
    def __init__(self, client: ServiceClient):
        self.c = client

    def call(self, method: str, params: dict | None = None) -> dict:
        data = self.c.get_json(f"/api/{method}", params)
        if not data.get("ok"):
            raise SlackApiError("slack", f"{method}: {data.get('error', 'unknown error')}")
        return data

    def auth_test(self) -> dict:
        return self.call("auth.test")

    def channel_exists(self, name: str) -> bool:
        name = name.lstrip("#")
        cursor = ""
        while True:
            params = {"types": "public_channel,private_channel", "limit": 200, "exclude_archived": "true"}
            if cursor:
                params["cursor"] = cursor
            data = self.call("conversations.list", params)
            if any(c.get("name") == name for c in data.get("channels") or []):
                return True
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return False


# httpx logs every request's URL at INFO: whoever turns logging on must not get the webhook with it.
logging.getLogger("httpx").setLevel(logging.WARNING)

# The webhook itself is gone or may not post: no message gets through until a person fixes it.
# https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/ (Handling errors)
HOOK_BROKEN = {"invalid_token", "action_prohibited", "no_service", "no_service_id", "no_team", "team_disabled",
               "channel_not_found", "channel_is_archived", "no_active_hooks", "posting_to_general_channel_denied"}
_CODE = re.compile(r"^[a-z_]{1,64}$")

Verdict = Literal["sent", "retry", "rejected", "broken"]


@dataclass(frozen=True)
class Posted:
    """What became of one post. `retry`: try again later (after `retry_after` when Slack said so), and when
    `uncertain` the message may already be in the channel. `rejected`: this message will never go. `broken`:
    the webhook takes nothing until a person fixes it."""
    verdict: Verdict
    detail: str = ""
    retry_after: float | None = None
    uncertain: bool = False


class Webhook:
    def __init__(self, url: str, http: HttpConfig, transport: httpx.BaseTransport | None = None):
        self._url = url
        self._client = httpx.Client(
            timeout=httpx.Timeout(http.read_timeout.total_seconds(), connect=http.connect_timeout.total_seconds()),
            transport=transport, follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def post(self, text: str) -> Posted:
        payload = {"text": text, "unfurl_links": False, "unfurl_media": False}
        try:
            resp = self._client.post(self._url, json=payload)
        except (httpx.ConnectTimeout, httpx.ConnectError) as e:
            return Posted("retry", f"cannot reach Slack ({type(e).__name__})")
        except httpx.TimeoutException as e:
            # Slack may have posted it and not answered in time: the next try may post it twice.
            return Posted("retry", f"no answer from Slack in time ({type(e).__name__})", uncertain=True)
        except httpx.TransportError as e:
            return Posted("retry", f"connection to Slack broke ({type(e).__name__})", uncertain=True)
        code = resp.status_code
        body = resp.text.strip()[:200]
        word = body if _CODE.match(body) else ""
        said = f"HTTP {code}" + (f" {word}" if word else "")
        if 200 <= code < 300:
            return Posted("sent", said)
        if code == 429 or code >= 500:
            return Posted("retry", said, retry_after=_retry_after(resp, time.time()))
        if 300 <= code < 400:
            return Posted("broken", f"{said}: a redirect, not followed (is the URL an incoming webhook?)")
        if word in HOOK_BROKEN or code in (403, 404, 410):
            return Posted("broken", said)
        return Posted("rejected", said)  # invalid_payload and the like: this message, not the webhook
