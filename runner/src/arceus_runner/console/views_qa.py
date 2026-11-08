"""Test runs of the platform (qa.py) in the console: the list, a run's report with its screenshots, starting and
stopping a run, and the test instructions.

Only the report's own files are served, as the runner's old page did (qa_report.resolve): a run's media by its
exact name. Not the copy of the instructions kept with a run (it holds the test accounts), not result.json, not any
other path. The report is rendered from the run's result.json, the same view report.html was written from."""

from __future__ import annotations

import json

from starlette.responses import FileResponse, Response
from starlette.routing import Route

from arceus_runner import qa as qa_mod
from arceus_runner import qa_report
from arceus_runner.console import runs
from arceus_runner.console.store import ConsoleError
from arceus_runner.console.web import Ctx, Gone, Markup, page
from arceus_runner.db import ReviewStore

SECTIONS = ("/qa/",)


def _cfg(ctx: Ctx):
    cfg, problems = ctx.config()
    if cfg is None:
        raise ConsoleError("config.yaml has errors: " + "; ".join(problems[:3]))
    return cfg


def _n(ctx: Ctx) -> int:
    value = ctx.param("n")
    if not value.isdigit() or len(value) > 9:
        raise Gone()
    return int(value)


def index(ctx: Ctx) -> Response:
    cfg, problems = ctx.config()
    if cfg is None:
        return ctx.render("message.html", status=503, title="Test runs",
                          message="config.yaml has errors, so the runs cannot be found: " + "; ".join(problems[:3]))
    error = ""
    if ctx.request.method == "POST":
        user = ctx.require("qa.run")
        try:
            n = runs.start(cfg, "qa", user)
            ctx.store.log(user, user.label, "qa_run_queued", f"qa-{n}")
            return ctx.redirect("/qa/", f"Run {n} queued. The runner starts it once the workspace is free.")
        except ConsoleError as e:
            error = str(e)
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        rows, db_error = [], str(e)
    else:
        try:
            rows, db_error = qa_mod.index_rows(store, cfg.qa.reports), ""
        finally:
            store.close()
    return ctx.render("runs.html", status=400 if error else 200, title="Test runs", kind="qa",
                      rows=runs.dress(rows, qa_report.head_of),
                      enabled=cfg.qa.enabled, error=error or db_error, notice=ctx.query("notice"),
                      mode=cfg.mode, can_run=ctx.can("qa.run"), can_edit=ctx.can("qa.instructions"),
                      schedule=cfg.qa.schedule)


def run(ctx: Ctx) -> Response:
    n = _n(ctx)
    cfg = _cfg(ctx)
    folder = cfg.qa.reports / str(n)
    result = folder / "result.json"
    try:
        if result.is_symlink():
            raise Gone()
        view = json.loads(result.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError):
        raise Gone() from None
    except (OSError, ValueError):
        return ctx.render("message.html", status=500, title=f"Run {n}",
                          message=f"The run's report cannot be read ({result}).")
    title, body = qa_report.run_body(view, cfg.project, cfg.schedule.tz)
    return ctx.render("report.html", title=title, body=Markup(body), kind="qa", n=n)


def stop(ctx: Ctx) -> Response:
    user = ctx.require("qa.run")
    n = _n(ctx)
    try:
        runs.stop(_cfg(ctx), "qa", n)
    except ConsoleError as e:
        return ctx.redirect("/qa/", str(e))
    ctx.store.log(user, user.label, "qa_run_stop_asked", f"qa-{n}")
    return ctx.redirect("/qa/", f"Run {n}: asked to stop. The runner stops it on its next pass and writes its report.")


def media(ctx: Ctx) -> Response:
    n = _n(ctx)
    cfg = _cfg(ctx)
    root = cfg.qa.reports.resolve()
    found = qa_report.resolve(root, f"/{n}/media/{ctx.param('file')}")
    if found is None:
        raise Gone()
    path, kind = found
    if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
        raise Gone()
    return FileResponse(path, media_type=kind, headers={"Cache-Control": "private, max-age=86400"})


def instructions(ctx: Ctx) -> Response:
    cfg = _cfg(ctx)
    return runs.file_page(
        ctx, kind="qa.instructions", action="qa.instructions", path=cfg.qa.instructions,
        title="Test instructions",
        lede="What a test run is told: the platform's address, its test accounts and what to test. Each run reads "
             "it when it starts and keeps a copy, so a change never alters a run already made.",
        back=("/qa/", "Test runs"), required=True, sensitive=True)


def routes() -> list[Route]:
    return [Route("/qa/", page(index), methods=["GET", "POST"]),
            Route("/qa/instructions/", page(instructions), methods=["GET", "POST"]),
            Route("/qa/{n}/", page(run)),
            Route("/qa/{n}/stop", page(stop), methods=["POST"]),
            Route("/qa/{n}/media/{file}", page(media))]
