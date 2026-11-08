"""People: who has an account, who waits for approval, their roles; invitations and reset links. Admins only."""

from __future__ import annotations

from starlette.responses import Response
from starlette.routing import Route

from arceus_runner.console.store import ROLES, ConsoleError
from arceus_runner.console.web import Ctx, Gone, notice, page

SECTIONS = ("/users/",)


def _link_url(ctx: Ctx, token: str) -> str:
    base = ctx.settings.public_url or f"{ctx.request.url.scheme}://{ctx.request.headers.get('host', '')}"
    return f"{base}/link/{token}"


def _roles(ctx: Ctx) -> list[str]:
    return [r for r in ctx.fields("role") if r in ROLES]


def people(ctx: Ctx) -> Response:
    admin = ctx.require("users.manage")
    error, made = "", None
    if ctx.request.method == "POST":
        email = ctx.field("email").strip()
        try:
            token = ctx.store.make_link("invite", email, _roles(ctx), by=admin)
            made = {"email": email, "url": _link_url(ctx, token), "kind": "invite"}
        except ConsoleError as e:
            error = str(e)
    users = ctx.store.users()
    return ctx.render("people.html", status=400 if error else 200, users=users, error=error, made=made,
                      notice=notice(ctx), roles=ROLES, admins=len(ctx.store.active_admins()))


def person(ctx: Ctx) -> Response:
    admin = ctx.require("users.manage")
    try:
        user_id = int(ctx.param("id"))
    except ValueError:
        raise Gone() from None
    user = ctx.store.user(user_id)
    if user is None:
        raise Gone()
    error, made = "", None
    if ctx.request.method == "POST":
        what = ctx.field("what")
        try:
            if what == "approve":
                ctx.store.approve(user.id, _roles(ctx), admin)
                return ctx.redirect("/users/", f"{user.name} can sign in now.")
            if what == "decline":
                ctx.store.decline(user.id, admin)
                return ctx.redirect("/users/", f"The request of {user.name} is declined.")
            if what == "roles":
                ctx.store.set_roles(user.id, _roles(ctx), admin)
                return ctx.redirect(f"/users/{user.id}/", "Roles saved. They apply on the next page they open.")
            if what in ("disable", "enable"):
                ctx.store.set_status(user.id, "disabled" if what == "disable" else "active", admin)
                words = "is disabled and signed out everywhere" if what == "disable" else "can sign in again"
                return ctx.redirect(f"/users/{user.id}/", f"{user.name} {words}.")
            if what == "signout":
                n = ctx.store.end_sessions(user.id)
                ctx.store.log(admin, admin.label, "signed_out", f"user:{user.id}", {"sessions": n})
                return ctx.redirect(f"/users/{user.id}/", f"Signed {user.name} out of {n} "
                                                          f"session{'s' if n != 1 else ''}.")
            if what == "reset":
                token = ctx.store.make_link("reset", user.email, by=admin)
                made = {"email": user.email, "url": _link_url(ctx, token), "kind": "reset"}
            else:
                raise Gone()
        except ConsoleError as e:
            error = str(e)
        user = ctx.store.user(user.id) or user
    approver = ctx.store.user(user.approved_by) if user.approved_by else None
    return ctx.render("person.html", status=400 if error else 200, person=user, error=error, made=made,
                      notice=notice(ctx), roles=ROLES, approver=approver, sessions=ctx.store.session_count(user.id),
                      journal=ctx.store.journal(30, target=f"user:{user.id}"))


def journal(ctx: Ctx) -> Response:
    ctx.require("users.manage")
    return ctx.render("journal.html", entries=ctx.store.journal(300))


def routes() -> list[Route]:
    return [
        Route("/users/", page(people), methods=["GET", "POST"]),
        Route("/users/journal/", page(journal)),
        Route("/users/{id}/", page(person), methods=["GET", "POST"]),
    ]
