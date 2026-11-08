"""The home page (what the factory does now), its part that refreshes itself, and the question nginx asks before
it lets anyone into Grafana."""

from __future__ import annotations

from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route

from arceus_runner.console import auth, links, status
from arceus_runner.console.web import Ctx, page

SECTIONS = ("/", "/grafana/")
DASHBOARDS = [("arceus-now", "Now", "the agent's step, the queue, health and usage, the latest events"),
              ("arceus-tasks", "Tasks", "tasks taken, merge requests, outcomes per day, why tasks failed"),
              ("arceus-resources", "Resources", "CPU, memory, disk and network, with task starts and ends")]


def _facts(ctx: Ctx) -> dict:
    cfg, problems = ctx.config()
    if cfg is None:
        return {"problems": problems}
    return {"s": status.snapshot(cfg, links.issue_url(cfg)), "cfg": cfg, "ago": status.ago}


def home(ctx: Ctx) -> Response:
    return ctx.render("home.html", title="Now", dashboards=DASHBOARDS, **_facts(ctx))


def live(ctx: Ctx) -> Response:
    """The status part of the home page alone: what the page swaps in every few seconds."""
    html = ctx.app.templates.get_template("home_status.html").render(ctx=ctx, user=ctx.user, **_facts(ctx))
    return HTMLResponse(html)


def grafana_auth(ctx: Ctx) -> Response:
    """nginx's auth_request for /grafana/: 200 with who it is and Grafana's role for them, or 401. nginx copies these
    headers over whatever the browser sent, so a header from outside never reaches Grafana."""
    user = ctx.user
    if user is None:
        return PlainTextResponse("", status_code=401)
    headers = {"X-Arceus-User": user.email, "X-Arceus-Role": auth.grafana_role(user),
               "Cache-Control": "no-store"}
    if user.name.isascii():
        headers["X-Arceus-Name"] = user.name
    return PlainTextResponse("", headers=headers)


def routes() -> list[Route]:
    return [Route("/", page(home)),
            Route("/live", page(live, api=True)),
            Route("/_auth/grafana", page(grafana_auth, signed_in=False))]
