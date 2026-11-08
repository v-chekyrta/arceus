"""The console's web server: the app, a request's context, and the rules every page goes through.

Every page is a plain function of a `Ctx` that returns a response; `page()` wraps it. The wrapper finds the session
(signed out: to /login, or 401 for the API), checks that a POST comes from the console's own page (the Origin the
browser sends, and the form's CSRF token), runs the handler in a thread (SQLite, password hashing), and adds the
headers every response carries. Pages are rendered from templates/ with autoescaping; nothing is loaded from
another site.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from arceus_runner import __version__
from arceus_runner.config import Config, ConfigError, load_config, parse_duration
from arceus_runner.console import auth
from arceus_runner.console.store import ConsoleError, ConsoleStore, Session, User

HERE = Path(__file__).resolve().parent
MAX_FORM = 2 * 1024 * 1024  # a settings form or an instructions file; nginx refuses bigger bodies first


@dataclass(frozen=True)
class Settings:
    """How this console runs: from the environment its container is started with (setup writes it)."""
    config_path: Path
    db_path: Path
    public_url: str = ""          # https://console.example.com; empty: whatever Host the request names (development)
    ops_dir: Path | None = None
    session_idle: timedelta = timedelta(hours=24)
    session_max: timedelta = timedelta(days=14)
    grafana_path: str = "/grafana/"

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def origin(self) -> str:
        p = urllib.parse.urlsplit(self.public_url)
        return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        config = Path(env.get("ARCEUS_RUNNER_CONFIG") or "~/arceus/data/config.yaml").expanduser()
        data = config.parent
        return cls(config_path=config,
                   db_path=Path(env.get("ARCEUS_CONSOLE_DB") or data / "console" / "console.db").expanduser(),
                   public_url=(env.get("ARCEUS_CONSOLE_PUBLIC_URL") or "").rstrip("/"),
                   ops_dir=Path(env.get("ARCEUS_CONSOLE_OPS") or data / "console" / "ops").expanduser(),
                   session_idle=parse_duration(env.get("ARCEUS_CONSOLE_SESSION_IDLE") or "24h"),
                   session_max=parse_duration(env.get("ARCEUS_CONSOLE_SESSION_MAX") or "14d"))


class ConfigCache:
    """config.yaml as the runner would read it, read again when the file changes. A broken file is not an error of
    the console: the pages say what is wrong, and an admin can fix it in Settings."""

    def __init__(self, path: Path):
        self.path = path
        self._key: tuple | None = None
        self._value: tuple[Config | None, list[str]] = (None, [])

    def get(self) -> tuple[Config | None, list[str]]:
        try:
            st = self.path.stat()
            key = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError as e:
            return None, [f"{self.path}: {e.strerror or e}"]
        if key != self._key:
            try:
                self._value = (load_config(self.path), [])
            except ConfigError as e:
                self._value = (None, e.problems)
            self._key = key
        return self._value


class Denied(Exception):
    """The signed-in person may not do this."""


class Gone(Exception):
    """No such page."""


@dataclass
class Ctx:
    """One request as a page sees it."""
    request: Request
    app: ConsoleApp
    store: ConsoleStore
    session: Session | None
    form: FormData | None = None
    token: str = ""
    cookies_out: dict[str, tuple[str, int | None]] = field(default_factory=dict)

    @property
    def user(self) -> User | None:
        return self.session.user if self.session else None

    @property
    def settings(self) -> Settings:
        return self.app.settings

    @property
    def path(self) -> str:
        return self.request.url.path

    @property
    def ip(self) -> str:
        return client_ip(self.request)

    def param(self, name: str) -> str:
        return str(self.request.path_params.get(name, ""))

    def query(self, name: str, default: str = "") -> str:
        return self.request.query_params.get(name, default)

    def field(self, name: str, default: str = "") -> str:
        value = self.form.get(name) if self.form is not None else None
        return value if isinstance(value, str) else default

    def fields(self, name: str) -> list[str]:
        return [v for v in (self.form.getlist(name) if self.form is not None else []) if isinstance(v, str)]

    def can(self, action: str) -> bool:
        return auth.can(self.user, action)

    def require(self, action: str) -> User:
        if not self.can(action):
            raise Denied(action)
        assert self.user is not None
        return self.user

    def config(self) -> tuple[Config | None, list[str]]:
        return self.app.config.get()

    @property
    def csrf(self) -> str:
        return self.session.csrf if self.session else self.request.cookies.get(self.app.csrf_cookie, "") or \
            self.cookies_out.setdefault(self.app.csrf_cookie, (secrets.token_urlsafe(24), None))[0]

    def render(self, template: str, status: int = 200, **values: Any) -> HTMLResponse:
        user = self.user
        nav = []
        if user:
            for href, label, action in SECTIONS:
                if href in self.app.sections and self.can(action):
                    here = self.path == href if href == "/" else self.path.startswith(href)
                    nav.append((href, label, here))
        cfg, _ = self.config()
        body = self.app.templates.get_template(template).render(
            ctx=self, user=user, nav=nav, csrf=self.csrf, project=cfg.project if cfg else "",
            pending=self.store.pending_count() if self.can("users.manage") else 0, version=__version__,
            css=self.app.asset_url("console.css"), js=self.app.asset_url("console.js"), **values)
        return HTMLResponse(body, status_code=status)

    def redirect(self, to: str, notice: str = "") -> RedirectResponse:
        if notice:
            to += ("&" if "?" in to else "?") + urllib.parse.urlencode({"notice": notice})
        return RedirectResponse(to, status_code=303)


def client_ip(request: Request) -> str:
    """The person's address: nginx (on this machine) passes the one it trusts as X-Real-IP; anything else is the
    peer itself."""
    peer = request.client.host if request.client else ""
    if peer in ("127.0.0.1", "::1"):
        return request.headers.get("x-real-ip", peer)[:64]
    return peer


def safe_next(value: str) -> str:
    """Where to go after signing in: a path of this console only."""
    if not value.startswith("/") or value.startswith("//") or "\\" in value or any(c in value for c in "\r\n"):
        return "/"
    return value


def same_origin(request: Request, settings: Settings) -> bool:
    """A POST from the console's own page: the browser says so (Sec-Fetch-Site, Origin)."""
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True  # an old browser or a script: the CSRF token still has to match
    expected = settings.origin or f"{request.url.scheme}://{request.headers.get('host', '')}"
    return origin == expected


# The bar at the top: address, words, who sees it. Only the sections this console serves are shown.
SECTIONS = [("/", "Home", "view"), ("/qa/", "Test runs", "view"), ("/architecture/", "Architecture", "view"),
            ("/grafana/", "Monitoring", "view"), ("/settings/", "Settings", "settings.view"),
            ("/operations/", "Operations", "ops.view"), ("/users/", "People", "users.manage")]

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class ConsoleApp:
    """The pieces every request shares: settings, templates, the config cache."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.config = ConfigCache(settings.config_path)
        self.templates = Environment(loader=FileSystemLoader(HERE / "templates"),
                                     autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
        self.templates.globals["can"] = auth.can
        self.templates.globals["role_words"] = auth.ROLE_WORDS
        self.templates.filters["when"] = self.when
        self.session_cookie = "__Host-arceus" if settings.secure else "arceus"
        self.csrf_cookie = "__Host-arceus-csrf" if settings.secure else "arceus-csrf"
        self.sections: set[str] = set()
        self.assets: dict[str, tuple[bytes, str, str]] = {}
        from arceus_runner.console import assets
        for name, (data, kind) in assets.build().items():
            self.assets[name] = (data, kind, hashlib.sha256(data).hexdigest()[:12])
        # The database exists before the first request: migrations run once, here, not in the middle of a page.
        ConsoleStore(settings.db_path).close()

    def tz(self):
        cfg, _ = self.config.get()
        return cfg.schedule.tz if cfg else UTC

    def when(self, iso_text: str | None) -> str:
        """A stored time in the project's timezone (schedule.timezone), as people there read it."""
        from arceus_runner.console.store import parse_iso
        if not iso_text:
            return ""
        try:
            at = parse_iso(iso_text)
        except ValueError:
            return iso_text
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        return f"{at.astimezone(self.tz()):%d %b %Y, %H:%M}"

    def now_text(self) -> str:
        return f"{datetime.now(self.tz()):%H:%M:%S}"

    def asset_url(self, name: str) -> str:
        return f"/static/{name}?v={self.assets[name][2]}"

    def store(self) -> ConsoleStore:
        return ConsoleStore(self.settings.db_path)



def page(handler: Callable[[Ctx], Response], *, signed_in: bool = True, api: bool = False):
    """A route's endpoint around a page function."""
    async def endpoint(request: Request) -> Response:
        app: ConsoleApp = request.app.state.console
        form = None
        if request.method == "POST":
            if int(request.headers.get("content-length") or 0) > MAX_FORM:
                return _finish(app, PlainTextResponse("The form is too large.", status_code=413), None)
            form = await request.form(max_files=0, max_fields=2000, max_part_size=MAX_FORM)

        def run() -> Response:
            store = app.store()
            try:
                token = request.cookies.get(app.session_cookie, "")
                session = store.session(token, app.settings.session_idle, app.settings.session_max) if token else None
                ctx = Ctx(request=request, app=app, store=store, session=session, form=form, token=token)
                if signed_in and session is None:
                    if api:
                        return PlainTextResponse("Sign in first.", status_code=401)
                    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                    return RedirectResponse("/login?" + urllib.parse.urlencode({"next": nxt}), status_code=303)
                if request.method == "POST":
                    sent = ctx.field("csrf")
                    if not same_origin(request, app.settings) or not sent or \
                            not hmac.compare_digest(sent.encode(), ctx.csrf.encode()):
                        return ctx.render("message.html", status=403, title="The page was out of date",
                                          message="This form was sent from an old or another page. Go back, "
                                                  "reload the page and try again.")
                try:
                    response = handler(ctx)
                except Denied:
                    response = ctx.render("message.html", status=403, title="Not allowed",
                                          message="Your roles do not allow this. An admin can change them.")
                except Gone:
                    response = ctx.render("message.html", status=404, title="Not found",
                                          message="There is nothing at this address.")
                except ConsoleError as e:
                    response = ctx.render("message.html", status=503, title="Not available now", message=str(e))
                for name, (value, age) in ctx.cookies_out.items():
                    _cookie(response, name, value, age, app.settings.secure)
                return _finish(app, response, session)
            finally:
                store.close()
        return await run_in_threadpool(run)
    return endpoint


def _cookie(response: Response, name: str, value: str, age: int | None, secure: bool) -> None:
    if value:
        response.set_cookie(name, value, max_age=age, path="/", httponly=True, samesite="lax", secure=secure)
    else:
        response.delete_cookie(name, path="/", httponly=True, samesite="lax", secure=secure)


def _finish(app: ConsoleApp, response: Response, session: Session | None) -> Response:
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if response.media_type and response.media_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def static(request: Request) -> Response:
    app: ConsoleApp = request.app.state.console
    found = app.assets.get(request.path_params["name"])
    if found is None:
        return PlainTextResponse("Not found", status_code=404)
    data, kind, _ = found
    response = Response(data, media_type=kind, headers={"Cache-Control": "public, max-age=31536000, immutable"})
    return _finish(app, response, None)


async def healthz(_request: Request) -> Response:
    return PlainTextResponse("ok")


def create_app(settings: Settings | None = None) -> Starlette:
    from arceus_runner.console import views_arch, views_auth, views_home, views_qa, views_users
    settings = settings or Settings.from_env()
    console = ConsoleApp(settings)
    routes = [Route("/static/{name}", static), Route("/healthz", healthz)]
    for module in (views_auth, views_home, views_qa, views_arch, views_users):
        routes += module.routes()
        console.sections.update(getattr(module, "SECTIONS", ()))
    app = Starlette(routes=routes)
    app.state.console = console
    return app


def message(ctx: Ctx, title: str, text: str, status: int = 200, back: str = "") -> HTMLResponse:
    return ctx.render("message.html", status=status, title=title, message=text, back=back)


def notice(ctx: Ctx) -> str:
    return ctx.query("notice")[:300]


__all__ = ["ConsoleApp", "ConsoleError", "Ctx", "Denied", "Gone", "Markup", "Settings", "create_app", "message",
           "notice", "page", "safe_next"]
