"""Settings → Assistant: the project's assistant on one page. Operators see it; admins change it.

- **How it is now**: its service (as the host's executor last saw it), Slack (the workspace and the bot as Slack
  named them, or why not), who pays for its Claude and how its last reply went, the web chat. The assistant writes
  that itself (assistant-status.json beside config.yaml, no secret in it).
- **Saved is not applied**: the page compares the config.yaml the assistant started with and the file now, and
  offers the restart (a host operation, like the runner's).
- **Who talks to her**, from config.yaml, in words.
- **Keys, write-only**: an admin sets, replaces or removes each of assistant.env's keys. The console never reads that
  file (its container sees it empty); the value goes in a request only the host's executor takes, at once, and comes
  back nowhere: the page knows which keys are set, and since when, from the executor's assistant-keys.json.
- **Its settings** are config.yaml's `assistant:` section, edited through the same Check → Save as every section
  (views_settings.section), with the settings that would run code or read files locked.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from starlette.responses import Response
from starlette.routing import Route

from arceus_runner.console import files, views_ops, views_settings
from arceus_runner.console.hostops import ASSISTANT_KEYS, KEY_VALUE
from arceus_runner.console.store import ConsoleError
from arceus_runner.console.web import Ctx, page

SECTIONS: tuple[str, ...] = ()   # under Settings in the bar
STALE = 100                       # seconds: a status older than this is not being kept (it beats every 30 s)
KEY_WORDS = {
    "ANTHROPIC_API_KEY": ("Anthropic API key", "Her replies on Claude, with auth: api_key. Starts with sk-ant-."),
    "OPENAI_API_KEY": ("OpenAI API key", "Her replies on Codex, with auth: api_key. Starts with sk-."),
    "SLACK_APP_TOKEN": ("Slack app-level token", "Socket Mode, scope connections:write. Starts with xapp-."),
    "SLACK_BOT_TOKEN": ("Slack bot token", "Her messages and reading channels. Starts with xoxb-."),
    "YOUTRACK_TOKEN": ("YouTrack token", "The account her tickets are made from. Starts with perm."),
}
assert set(KEY_WORDS) == set(ASSISTANT_KEYS)
# What writes her replies and who pays for it: (assistant.provider, assistant.auth) in words, and the key it needs.
PAYERS = {("claude", "subscription"): "Claude subscription", ("codex", "subscription"): "Codex subscription",
          ("claude", "api_key"): "Anthropic API key", ("codex", "api_key"): "OpenAI API key"}
KEYS = {("claude", "api_key"): "ANTHROPIC_API_KEY", ("codex", "api_key"): "OPENAI_API_KEY"}


def _json(path: Path) -> dict:
    try:
        if path.is_symlink():
            return {}
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _age(iso: str | None) -> float | None:
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(str(iso))).total_seconds()
    except (TypeError, ValueError):
        return None


def view_state(ctx: Ctx, a) -> dict:
    """Everything the page says about the running assistant, from what the assistant and the host wrote, and how
    sure it can be: the host's word on the service is only as fresh as its executor, and her own status only as fresh
    as her last beat (every minute)."""
    current = files.read(ctx.settings.config_path)
    status = _json(ctx.settings.config_path.parent / "assistant-status.json")
    host = views_ops.host_state(ctx)
    service = (host.get("services") or {}).get("assistant")
    keys = _json(views_ops._ops(ctx) / "assistant-keys.json")
    age = _age(status.get("at"))
    # The service: running, stopped, or not known now (the executor is not answering: what it last said, with when).
    if not host["ok"] or service is None:
        running = None
    else:
        running = service.get("active") == "active"
    fresh = age is not None and age < STALE
    kept = fresh and running is not False
    if not status.get("config_sha256"):
        applied, why = "unknown", "she has not written her status yet"
    elif running is False:
        applied, why = "unknown", f"she is not running ({(service or {}).get('active', '?')})"
    elif not fresh:
        written = views_ops.status.ago(datetime.now(UTC).timestamp() - age) if age is not None else "never"
        applied, why = "unknown", f"her status was last written {written}: she is not keeping it now"
    elif status["config_sha256"] == current.sha:
        applied, why = "same", ""
    else:
        applied, why = "differs", ""
    # Who pays now is what the running process says; a saved change of it waits for the restart.
    claude = status.get("claude") or {}
    payer_now = (claude.get("provider") or "claude", claude.get("auth")) if kept and claude.get("auth") else None
    saved = (a.provider, a.auth)
    # Slack and the web chat, likewise: what the running process said; what is saved differently waits for the restart.
    running_slack = (status.get("slack") or {}).get("state") if kept else None
    running_web = bool(status.get("web")) if kept else None
    saved_web = a.enabled and a.web
    # The web chat is reached only through the console's nginx, whose /assistant/ route setup writes: what nginx
    # routes now, as the host's executor read it (unknown while the executor is not answering).
    route = (host.get("routes") or {}).get("assistant") if host["ok"] else None
    route_known = bool(route and route.get("known"))
    route_port = route.get("port") if route_known else None
    reachable = None if running_web is None or not route_known else \
        bool(running_web and route_port is not None and route_port == status.get("port"))
    # Setup is owed when nginx does not route as the saved settings say: a restart does not change nginx.
    needs_setup = route_known and ((route_port != a.port) if saved_web else (route_port is not None))
    if applied == "same" and needs_setup:
        applied = "setup"
    return {"running_slack": running_slack, "running_web": running_web, "route_known": route_known,
            "route_port": route_port, "route_why": (route or {}).get("why", "") if route else
            ("the host's executor is not answering" if not host["ok"] else "the host has not reported it"),
            "reachable": reachable, "needs_setup": needs_setup, "saved_web": saved_web,
            "slack_pending": running_slack is not None and (running_slack != "off") != bool(a.slack),
            "web_pending": running_web is not None and running_web != saved_web,"status": status, "kept": kept, "fresh": fresh, "age": age, "host": host, "service": service,
            "running": running, "applied": applied, "why": why,
            "running_payer": PAYERS.get(payer_now, "") if payer_now else "", "saved_payer": PAYERS[saved],
            "running_key": KEYS.get(payer_now) if payer_now else None, "saved_key": KEYS.get(saved),
            "auth_pending": bool(payer_now and payer_now != saved),
            "key_state": keys.get("keys") or {}, "keys_known": bool(keys), "keys_at": keys.get("at"),
            "sha": current.sha}


def audience(a) -> dict:
    """Who talks to her, in words, from config.yaml."""
    slack = None
    if a.slack:
        s = a.slack
        slack = {"who": "Every full member of the Slack workspace, in a direct message, a mention, or a thread she is "
                        "in. Guests and people of other companies are refused.",
                 "partner": s.partner_channels, "listen": s.listen_channels, "read": s.read_channels}
    creators = list(a.ticket_creators)
    if a.slack and a.slack.creator_channels:
        creators.append(f"members of {', '.join(a.slack.creator_channels)} with an @{a.slack.creator_domain} e-mail")
    return {"web": list(a.allowed_users) if a.web else None, "slack": slack,
            "creators": creators or ["anyone she talks to"], "expert": a.expert,
            "expert_name": a.expert_name}


def overview(ctx: Ctx) -> Response:
    ctx.require("settings.view")
    cfg, problems = ctx.config()
    if cfg is None:
        return ctx.render("message.html", status=503, title="Assistant",
                          message="config.yaml does not load: " + "; ".join(problems[:3]) + ". Fix it in Settings.",
                          back="/settings/")
    a = cfg.assistant
    return ctx.render("assistant.html", title="Assistant", a=a, s=view_state(ctx, a), who=audience(a),
                      key_words=KEY_WORDS, notice=ctx.query("notice"), error=ctx.query("error"),
                      can_edit=ctx.can("settings.edit"), can_restart=ctx.can("ops.restart"),
                      can_setup=ctx.can("ops.setup"))


def _request_secret(ctx: Ctx, body: dict) -> str:
    """The key's request: like every operation's, but readable by the run user alone, and taken by the host at once."""
    ops = views_ops._ops(ctx)
    (ops / "requests").mkdir(parents=True, exist_ok=True)
    op_id = os.urandom(16).hex()
    tmp = ops / f".request-{op_id}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"id": op_id, **body, "requested_at": datetime.now(UTC).isoformat(timespec="seconds")}, f)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, ops / "requests" / f"{op_id}.json")
    return op_id


def _outcome(ctx: Ctx, op_id: str, wait: float = 6.0) -> dict | None:
    """The host's record of the request once it has taken it (it does at once, through its path unit)."""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        rec = _json(views_ops._ops(ctx) / "runs" / f"{op_id}.json")
        if rec.get("state") in ("succeeded", "failed", "refused"):
            return rec
        time.sleep(0.25)
    return None


def key(ctx: Ctx) -> Response:
    user = ctx.require("settings.edit")
    name = ctx.field("name")
    if name not in ASSISTANT_KEYS:
        return ctx.redirect("/settings/assistant/", error="That is not one of the assistant's keys.")
    remove = ctx.field("action") == "remove"
    value = "" if remove else ctx.field("value").strip()
    label = KEY_WORDS[name][0]
    if not remove and (not KEY_VALUE.match(value) or not value.startswith(ASSISTANT_KEYS[name])):
        return ctx.redirect("/settings/assistant/", error=f"Not saved: that does not look like a {label}. It starts "
                                                          f"with {ASSISTANT_KEYS[name]}, on one line, without spaces.")
    host = views_ops.host_state(ctx)
    if not host["ok"]:
        # Not written at all: a request with a key in it must not wait in the data folder for an executor that is away.
        return ctx.redirect("/settings/assistant/", error=f"Not saved: {host['why']}.")
    try:
        op_id = _request_secret(ctx, {"op": "assistant-key", "name": name, "value": value,
                                      "requested_by": user.label})
    except OSError as e:
        raise ConsoleError(f"The request could not be written: {e.strerror or e}") from None
    ctx.store.log(user, user.label, "assistant_key", f"key:{name}", {"removed": remove, "op": op_id})
    rec = _outcome(ctx, op_id)
    if rec is None:
        return ctx.redirect("/settings/assistant/", f"Sent to the host: {label} is {'removed' if remove else 'set'} "
                                                    "as soon as it takes the request. Reload in a moment.")
    if rec.get("state") != "succeeded":
        return ctx.redirect("/settings/assistant/", error=f"Not saved: {rec.get('result')}")
    return ctx.redirect("/settings/assistant/", rec.get("result") or "Done.")


def restart(ctx: Ctx) -> Response:
    user = ctx.require("ops.restart")
    host = views_ops.host_state(ctx)
    busy = [r for r in views_ops.records(ctx) if r.get("state") in (*views_ops.ACTIVE, "pending")]
    if not host["ok"]:
        return ctx.redirect("/settings/assistant/", error=f"Not restarted: {host['why']}.")
    if busy:
        return ctx.redirect("/settings/assistant/", error=f"Not restarted: {busy[0].get('title')} is under way. "
                                                          "One operation at a time.")
    try:
        op_id = views_ops._request(ctx, {"op": "restart-assistant", "requested_by": user.label})
    except OSError as e:
        raise ConsoleError(f"The request could not be written: {e.strerror or e}") from None
    ctx.store.log(user, user.label, "operation_requested", f"op:{op_id}", {"op": "restart-assistant"})
    return ctx.redirect(f"/operations/{op_id}/")


def edit(ctx: Ctx) -> Response:
    """The section's fields, through Settings' own form, check and save."""
    ctx.request.scope.setdefault("path_params", {})["name"] = "assistant"
    return views_settings.section(ctx)


def routes() -> list[Route]:
    return [Route("/settings/assistant/", page(overview)),
            Route("/settings/assistant/edit/", page(edit), methods=["GET", "POST"]),
            Route("/settings/assistant/key", page(key), methods=["POST"]),
            Route("/settings/assistant/restart", page(restart), methods=["POST"])]
