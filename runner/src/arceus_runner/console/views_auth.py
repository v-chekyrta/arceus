"""Signing in and out, registering, one-time links (an invitation, a password reset), and a person's own account."""

from __future__ import annotations

from starlette.responses import Response
from starlette.routing import Route

from arceus_runner.console.store import ConsoleError
from arceus_runner.console.web import Ctx, message, notice, page, safe_next

PENDING = ("Your account is waiting for an admin's approval. You can sign in once they approve it; they will tell "
           "you, or just try again later.")


def login(ctx: Ctx) -> Response:
    nxt = safe_next(ctx.query("next") or ctx.field("next") or "/")
    if ctx.user:
        return ctx.redirect(nxt)
    if ctx.request.method == "GET":
        return ctx.render("login.html", next=nxt, notice=notice(ctx), email="")
    email = ctx.field("email").strip()
    password = ctx.field("password")
    try:
        user = ctx.store.authenticate(email, password, ctx.ip)
    except ConsoleError as e:
        return ctx.render("login.html", status=429, next=nxt, error=str(e), email=email)
    if user is None:
        return ctx.render("login.html", status=401, next=nxt, error="Wrong email or password.", email=email)
    if user.status == "pending":
        return ctx.render("login.html", status=403, next=nxt, error=PENDING, email=email)
    if user.status != "active":
        return ctx.render("login.html", status=403, next=nxt, email=email,
                          error="This account is disabled. Ask an admin if that is a mistake.")
    s = ctx.settings
    token = ctx.store.start_session(user, ctx.ip, ctx.request.headers.get("user-agent", ""), s.session_idle,
                                    s.session_max)
    response = ctx.redirect(nxt)
    ctx.cookies_out[ctx.app.session_cookie] = (token, int(s.session_max.total_seconds()))
    ctx.cookies_out[ctx.app.csrf_cookie] = ("", None)
    return response


def logout(ctx: Ctx) -> Response:
    if ctx.token:
        ctx.store.end_session(ctx.token)
    ctx.cookies_out[ctx.app.session_cookie] = ("", None)
    return ctx.redirect("/login", "You are signed out.")


def register(ctx: Ctx) -> Response:
    if ctx.user:
        return ctx.redirect("/")
    if ctx.request.method == "GET":
        return ctx.render("register.html", values={})
    values = {k: ctx.field(k) for k in ("name", "email", "note")}
    password, again = ctx.field("password"), ctx.field("password2")
    if password != again:
        return ctx.render("register.html", status=400, values=values, error="The two passwords are not the same.")
    try:
        ctx.store.register(values["email"], values["name"], password, values["note"])
    except ConsoleError as e:
        return ctx.render("register.html", status=400, values=values, error=str(e))
    # The same answer whether or not the address had an account: the form does not tell strangers who has one.
    return message(ctx, "Request sent", "Thanks. " + PENDING, back="/login")


def link(ctx: Ctx) -> Response:
    token = ctx.param("token")
    found = ctx.store.link(token)
    if found is None:
        return message(ctx, "This link does not work", "It has expired or was used already. Ask an admin for a "
                                                       "new one.", status=410, back="/login")
    if ctx.request.method == "GET":
        return ctx.render("link.html", link=found, values={})
    name, password, again = ctx.field("name"), ctx.field("password"), ctx.field("password2")
    if password != again:
        return ctx.render("link.html", status=400, link=found, values={"name": name},
                          error="The two passwords are not the same.")
    try:
        ctx.store.use_link(token, password, name)
    except ConsoleError as e:
        return ctx.render("link.html", status=400, link=found, values={"name": name}, error=str(e))
    if ctx.token:
        ctx.store.end_session(ctx.token)
        ctx.cookies_out[ctx.app.session_cookie] = ("", None)
    words = "Your account is ready." if found.kind == "invite" else "Your password is changed."
    return ctx.redirect("/login", f"{words} Sign in with your email and the new password.")


def account(ctx: Ctx) -> Response:
    user = ctx.user
    assert user is not None
    error = ""
    if ctx.request.method == "POST":
        what = ctx.field("what")
        if what == "password":
            current, new, again = ctx.field("current"), ctx.field("password"), ctx.field("password2")
            try:
                known = ctx.store.authenticate(user.email, current, ctx.ip) is not None
            except ConsoleError as e:  # too many wrong ones lately: the same wait as on the sign-in page
                return ctx.render("account.html", status=429, error=str(e), notice="",
                                  sessions=ctx.store.session_count(user.id))
            if not known:
                error = "The current password is wrong."
            elif new != again:
                error = "The two new passwords are not the same."
            else:
                try:
                    ctx.store.set_password(user.id, new, user, keep_session=ctx.token)
                    return ctx.redirect("/account/", "Password changed. Your other sessions are signed out.")
                except ConsoleError as e:
                    error = str(e)
        elif what == "sessions":
            n = ctx.store.end_sessions(user.id, keep=ctx.token)
            ctx.store.log(user, user.label, "signed_out_elsewhere", f"user:{user.id}", {"sessions": n})
            return ctx.redirect("/account/", f"Signed out of {n} other session{'s' if n != 1 else ''}.")
    return ctx.render("account.html", status=400 if error else 200, error=error, notice=notice(ctx),
                      sessions=ctx.store.session_count(user.id))


def home(ctx: Ctx) -> Response:
    return ctx.redirect("/account/")


def routes() -> list[Route]:
    return [
        Route("/", page(home)),
        Route("/login", page(login, signed_in=False), methods=["GET", "POST"]),
        Route("/logout", page(logout), methods=["POST"]),
        Route("/register", page(register, signed_in=False), methods=["GET", "POST"]),
        Route("/link/{token}", page(link, signed_in=False), methods=["GET", "POST"]),
        Route("/account/", page(account), methods=["GET", "POST"]),
    ]
