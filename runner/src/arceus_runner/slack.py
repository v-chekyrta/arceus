"""Slack Web API: token check and alerts channel lookup."""

from __future__ import annotations

from arceus_runner.http import BadRequest, ServiceClient


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
