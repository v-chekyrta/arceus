"""Operations: restarting the runner or the engine, and setup, from the console. The console has no root on the
machine: it writes a request, and the host's executor (hostops.py) does it from its fixed list and writes what
happens where the console can read it. The pages follow that record; nothing here assumes an operation went well.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from arceus_runner.console import status
from arceus_runner.console.hostops import ACTIVE, FINAL, OPS
from arceus_runner.console.store import ConsoleError
from arceus_runner.console.web import Ctx, Gone, page

SECTIONS = ("/operations/",)
FRESH = 90  # seconds: host.json older than this means the executor is not answering
_ID = re.compile(r"^[0-9a-f]{32}$")
STATE_WORDS = {"pending": "Waiting for the host", "accepted": "Starting", "running": "Running",
               "waiting": "Waiting", "succeeded": "Done", "failed": "Failed", "cancelled": "Cancelled",
               "interrupted": "Interrupted", "refused": "Refused"}
WHAT = {
    "restart-runner": "Restarts runner.service. A task in work keeps going: its session belongs to the engine, and "
                      "the runner picks it up again when it is back. config.yaml is read again.",
    "restart-engine": "First the runner stops starting anything new in the engine (new tickets, follow-ups, code "
                      "reviews, test runs, architecture reviews). Then this waits for the engine's running tasks to "
                      "end, as long as they take, restarts bulava-engine, and lets work start again. While it "
                      "waits it can be cancelled, which also lets work start again.",
    "setup-check": "Runs setup.sh --check: says which phases are not done. Changes nothing.",
    "setup": "Runs setup.sh: installs what the copy of the repository on the server holds, phase by phase. It may "
             "rebuild this console (the page comes back by itself) and restart the engine when no task runs; the "
             "runner then runs the new code once restarted.",
}


def _ops(ctx: Ctx) -> Path:
    assert ctx.settings.ops_dir is not None
    return ctx.settings.ops_dir


def host_state(ctx: Ctx) -> dict:
    """The services as the host's executor last wrote them, and whether it is answering."""
    path = _ops(ctx) / "host.json"
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {"ok": False, "why": "the host's executor has not written anything yet: setup.sh's console phase "
                                    "installs it (arceus-ops units)"}
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(data["at"])).total_seconds()
    except (KeyError, ValueError):
        age = 1e9
    if age > FRESH:
        return {"ok": False, "why": f"the host's executor last answered {status.ago(datetime.now(UTC).timestamp() - age)}"
                                    " (arceus-ops-tick.timer): operations wait until it runs again",
                "services": data.get("services") or {}}
    return {"ok": True, "services": data.get("services") or {}, "at": data["at"]}


def records(ctx: Ctx) -> list[dict]:
    out = []
    runs = _ops(ctx) / "runs"
    for p in runs.glob("*.json") if runs.is_dir() else []:
        if p.is_symlink() or not _ID.match(p.stem):
            continue
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            continue
    requests = _ops(ctx) / "requests"
    known = {r.get("id") for r in out}
    for p in requests.glob("*.json") if requests.is_dir() else []:
        if p.is_symlink() or not _ID.match(p.stem) or p.stem in known:
            continue
        try:
            req = json.loads(p.read_text())
        except ValueError:
            continue
        if req.get("op") in OPS:
            out.append({**req, "title": OPS[req["op"]][0], "state": "pending"})
    return sorted(out, key=lambda r: r.get("requested_at") or "", reverse=True)


def record(ctx: Ctx, op_id: str) -> dict | None:
    return next((r for r in records(ctx) if r.get("id") == op_id), None)


def _request(ctx: Ctx, body: dict) -> str:
    """The request, written where the host's path unit sees it: whole, by a rename in the same folder tree."""
    ops = _ops(ctx)
    (ops / "requests").mkdir(parents=True, exist_ok=True)
    op_id = uuid.uuid4().hex
    tmp = ops / f".request-{op_id}.tmp"
    with open(tmp, "x", encoding="utf-8") as f:
        json.dump({"id": op_id, **body, "requested_at": datetime.now(UTC).isoformat(timespec="seconds")}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, ops / "requests" / f"{op_id}.json")
    return op_id


def _work(ctx: Ctx) -> dict:
    cfg, _ = ctx.config()
    return status.read_store(cfg) if cfg else {"error": "config.yaml does not load"}


def overview(ctx: Ctx) -> Response:
    ctx.require("ops.view")
    recs = records(ctx)
    return ctx.render("operations.html", title="Operations", host=host_state(ctx), work=_work(ctx), recs=recs[:30],
                      ops=OPS, words=STATE_WORDS, busy=[r for r in recs if r.get("state") in (*ACTIVE, "pending")],
                      notice=ctx.query("notice"))


def new(ctx: Ctx) -> Response:
    op = ctx.param("op")
    if op not in OPS:
        raise Gone()
    user = ctx.require(OPS[op][2])
    host = host_state(ctx)
    busy = [r for r in records(ctx) if r.get("state") in (*ACTIVE, "pending")]
    error = ""
    if ctx.request.method == "POST":
        if not host["ok"]:
            error = f"Not started: {host['why']}."
        elif busy:
            error = f"Not started: {busy[0].get('title')} is under way. One at a time."
        else:
            try:
                op_id = _request(ctx, {"op": op, "requested_by": user.label})
            except OSError as e:
                raise ConsoleError(f"The request could not be written: {e.strerror or e}") from None
            ctx.store.log(user, user.label, "operation_requested", f"op:{op_id}", {"op": op})
            return ctx.redirect(f"/operations/{op_id}/")
    return ctx.render("operation_new.html", status=409 if error else 200, title=OPS[op][0], op=op, what=WHAT[op],
                      services=OPS[op][1], host=host, work=_work(ctx), busy=busy, error=error, words=STATE_WORDS)


def _log(ctx: Ctx, op_id: str, limit: int = 64 * 1024) -> str:
    path = _ops(ctx) / "runs" / f"{op_id}.log"
    try:
        if path.is_symlink():
            return ""
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read().decode("utf-8", "replace")
        return data if size <= limit else "…\n" + data.split("\n", 1)[-1]
    except FileNotFoundError:
        return ""


def _op_or_404(ctx: Ctx) -> dict:
    ctx.require("ops.view")
    op_id = ctx.param("id")
    if not _ID.match(op_id):
        raise Gone()
    rec = record(ctx, op_id)
    if rec is None:
        raise Gone()
    return rec


def one(ctx: Ctx) -> Response:
    rec = _op_or_404(ctx)
    return ctx.render("operation.html", title=rec.get("title") or "Operation", rec=rec, log=_log(ctx, rec["id"]),
                      words=STATE_WORDS, final=rec.get("state") in FINAL, notice=ctx.query("notice"),
                      can_cancel=rec.get("op") == "restart-engine" and rec.get("state") in ACTIVE
                      and ctx.can("ops.restart") and not rec.get("cancel"))


def live(ctx: Ctx) -> Response:
    rec = _op_or_404(ctx)
    html = ctx.app.templates.get_template("operation_live.html").render(
        ctx=ctx, rec=rec, log=_log(ctx, rec["id"]), words=STATE_WORDS, final=rec.get("state") in FINAL, csrf=ctx.csrf,
        can_cancel=rec.get("op") == "restart-engine" and rec.get("state") in ACTIVE and ctx.can("ops.restart")
        and not rec.get("cancel"))
    return HTMLResponse(html)


def cancel(ctx: Ctx) -> Response:
    user = ctx.require("ops.restart")
    rec = _op_or_404(ctx)
    if rec.get("op") != "restart-engine" or rec.get("state") not in ACTIVE:
        return ctx.redirect(f"/operations/{rec['id']}/", "Only an engine restart that is still waiting can be cancelled.")
    _request(ctx, {"op": "cancel", "target": rec["id"], "requested_by": user.label})
    ctx.store.log(user, user.label, "operation_cancel_asked", f"op:{rec['id']}")
    return ctx.redirect(f"/operations/{rec['id']}/", "Cancel asked: the operation stops at its next check.")


def routes() -> list[Route]:
    return [Route("/operations/", page(overview)),
            Route("/operations/new/{op}", page(new), methods=["GET", "POST"]),
            Route("/operations/{id}/", page(one)),
            Route("/operations/{id}/live", page(live, api=True)),
            Route("/operations/{id}/cancel", page(cancel), methods=["POST"])]
