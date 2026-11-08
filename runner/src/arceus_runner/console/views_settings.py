"""Settings: config.yaml in the console. Operators see it, admins change it.

A change goes in two steps. The form (a section's fields, or the whole file as YAML) is sent for a look first: the
page shows the exact difference in config.yaml, what the runner would say about the result (its own check,
config.parse_config) and what the change needs before it takes effect. Only a result the runner would load can be
saved. Saving writes the file atomically if it is still what the form was made from (files.save), and keeps the
version in console.db with who saved it and why. Nothing restarts by itself: the runner reads config.yaml when it
starts, so the pages say when the file differs from the one it runs with."""

from __future__ import annotations

import base64

from starlette.responses import Response
from starlette.routing import Route

from arceus_runner.config import ConfigError, parse_config
from arceus_runner.console import files
from arceus_runner.console import settings_form as sf
from arceus_runner.console.web import Ctx, Gone, page
from arceus_runner.db import ReviewStore

SECTIONS = ("/settings/",)
KIND = "config"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _path(ctx: Ctx):
    return ctx.settings.config_path


def _loaded(ctx: Ctx, current_sha: str) -> dict:
    """Which config.yaml the runner runs with, against the file now: {state, at, version}."""
    cfg, _ = ctx.config()
    if cfg is None:
        return {"state": "unknown", "why": "config.yaml does not load, so the runner's database cannot be found"}
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        return {"state": "unknown", "why": str(e)}
    try:
        found = store.state("config")
    finally:
        store.close()
    if not found or not found[0].get("sha256"):
        return {"state": "unknown", "why": "the runner has not recorded which file it started with (it does from this "
                                          "version on, at its next start)"}
    value = found[0]
    same = value["sha256"] == current_sha
    return {"state": "same" if same else "differs", "at": value.get("loaded_at"), "version": value.get("version"),
            "sha": value["sha256"]}


def _restart_hint(ctx: Ctx) -> str:
    return "/operations/" if "/operations/" in ctx.app.sections else ""


def overview(ctx: Ctx) -> Response:
    ctx.require("settings.view")
    current = files.read(_path(ctx))
    cfg, problems = ctx.config()
    groups = []
    if cfg is not None:
        for name, fields in sf.sections(cfg, current.text):
            title, what = sf.TITLES.get(name, (name, ""))
            flat = sf.flatten(fields)
            groups.append({"name": name, "title": title, "what": what, "count": len(flat),
                           "in_file": sum(1 for f in flat if f.in_file)})
    return ctx.render("settings.html", title="Settings", groups=groups, problems=problems, path=_path(ctx),
                      current=current, loaded=_loaded(ctx, current.sha), restart=_restart_hint(ctx),
                      legacy=sf.legacy(current.text), notice=ctx.query("notice"),
                      versions=ctx.store.versions(KIND, 5))


def section(ctx: Ctx) -> Response:
    ctx.require("settings.view")
    name = ctx.param("name")
    current = files.read(_path(ctx))
    if current.problem:
        return ctx.render("message.html", status=503, title="Settings", message=current.problem)
    try:
        cfg = parse_config(current.text, _path(ctx))
    except Exception:
        return ctx.redirect("/settings/", "config.yaml does not load: fix it as YAML first.")
    found = dict(sf.sections(cfg, current.text)).get(name)
    if found is None:
        raise Gone()
    title, what = sf.TITLES.get(name, (name, ""))
    if ctx.request.method == "POST":
        ctx.require("settings.edit")
        if sf.legacy(current.text):
            return ctx.redirect("/settings/yaml/", "This config.yaml has the older layout: edit it as YAML.")
        if ctx.field("sha") != current.sha:
            return _stale_section(ctx, current, name)
        try:
            change = sf.apply(current.text, found, ctx.form, _path(ctx).parent)
        except sf.Invalid as e:
            return ctx.render("settings_section.html", status=400, title=title, what=what, name=name, fields=found,
                              sha=current.sha, base=_b64(current.text), error=str(e), can_edit=True)
        if not change.changed:
            return ctx.render("settings_section.html", title=title, what=what, name=name, fields=found,
                              sha=current.sha, base=_b64(current.text),
                              notice="Nothing changed: every value is as it was.", can_edit=True)
        return _preview(ctx, current, change.text, origin=f"/settings/{name}/", changed=change.changed)
    return ctx.render("settings_section.html", title=title, what=what, name=name, fields=found, sha=current.sha,
                      base=_b64(current.text), can_edit=ctx.can("settings.edit") and not sf.legacy(current.text),
                      notice=ctx.query("notice"))


def _base(ctx: Ctx) -> str | None:
    """The config.yaml the form was made from, as the form carried it (base64, so the browser cannot touch it), if it
    is still that text."""
    try:
        text = base64.b64decode(ctx.field("base"), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return text if files.digest(text.encode()) == ctx.field("sha") else None


def _stale_section(ctx: Ctx, current: files.Current, name: str) -> Response:
    """A section's form made from an older config.yaml: the person's changes, worked out against the file they saw,
    put on top of the file as it is now, for them to look at. Nothing is written until they save it."""
    base = _base(ctx)
    origin = f"/settings/{name}/"
    if base is None:
        return _preview(ctx, current, current.text, origin=origin, conflict=True,
                        error="config.yaml changed since you opened this section, and the page did not carry the "
                              "version it was made from. Open the section again and make your change there.")
    try:
        base_fields = dict(sf.sections(parse_config(base), base))[name]
        changes = sf.edits(base_fields, ctx.form)
    except (KeyError, sf.Invalid, ConfigError) as e:  # an older file that does not load, or a value that is not one
        return _preview(ctx, current, current.text, origin=origin, conflict=True, server_diff=files.diff(
            base, current.text, "when you opened it", "config.yaml now"),
            error=f"config.yaml changed since you opened this section, and your change could not be read against "
                  f"it ({e}). Open the section again.")
    server = files.diff(base, current.text, "when you opened it", "config.yaml now")
    if not changes:
        return _preview(ctx, current, current.text, origin=origin, conflict=True, server_diff=server,
                        error="config.yaml changed since you opened this section; you changed nothing in it.")
    merged = sf.write(current.text, changes)
    return _preview(ctx, current, merged, origin=origin, changed=[".".join(p) for p, _ in changes], conflict=True,
                    server_diff=server,
                    error="config.yaml changed since you opened this section. Below: what changed on the server, then "
                          "your changes on top of the file as it is now. Nothing is saved until you save this.")


def _preview(ctx: Ctx, current: files.Current, new_text: str, origin: str, changed: list[str] | None = None,
             conflict: bool = False, error: str = "", note: str = "", server_diff: str = "") -> Response:
    new_text = files.normal(new_text)
    problems = sf.check(new_text)
    difference = files.diff(current.text, new_text, "config.yaml now", "after saving")
    return ctx.render("settings_preview.html", status=409 if conflict else 200, title="Check the change",
                      diff=difference, problems=problems, text=new_text, sha=current.sha, origin=origin,
                      server_diff=server_diff,
                      changed=changed or [], error=error,
                      setup=sf.needs_setup(current.text, new_text, _path(ctx).parent),
                      same=new_text == current.text, note=note, restart=_restart_hint(ctx))


def yaml_editor(ctx: Ctx) -> Response:
    ctx.require("settings.view")
    current = files.read(_path(ctx))
    if ctx.request.method == "POST":
        ctx.require("settings.edit")
        if ctx.field("sha") != current.sha:
            base = _base(ctx)
            server = files.diff(base, current.text, "when you opened it", "config.yaml now") if base is not None else ""
            return _preview(ctx, current, ctx.field("text"), origin="/settings/yaml/", conflict=True,
                            server_diff=server,
                            error="config.yaml changed since you opened the editor. Below: what changed on the server, "
                                  "then the file now against your text. Saving yours replaces theirs: check first.")
        return _preview(ctx, current, ctx.field("text"), origin="/settings/yaml/", note=ctx.field("note"))
    text, restoring = current.text, None
    if ctx.query("version").isdigit():
        restoring = ctx.store.version_of(KIND, int(ctx.query("version")))
        if restoring:
            which = ctx.query("which")
            text = restoring["before_text"] if which == "before" and restoring["before_text"] is not None \
                else restoring["after_text"]
            restoring = {**restoring, "which": "before" if which == "before" else "after"}
    return ctx.render("settings_yaml.html", title="config.yaml", text=text, sha=current.sha, path=_path(ctx),
                      base=_b64(current.text),
                      problems=sf.check(current.text) if current.exists else [], restoring=restoring,
                      can_edit=ctx.can("settings.edit") and not current.problem, error=current.problem)


def save(ctx: Ctx) -> Response:
    user = ctx.require("settings.edit")
    text = files.normal(ctx.field("text"))
    origin = ctx.field("origin") if ctx.field("origin").startswith("/settings/") else "/settings/"
    problems = sf.check(text)
    current = files.read(_path(ctx))
    if problems:
        return _preview(ctx, current, text, origin=origin)
    try:
        saved = files.save(ctx.store, KIND, _path(ctx), text, ctx.field("sha"), user, ctx.field("note"))
    except files.Conflict:
        return _preview(ctx, current, text, origin=origin, conflict=True,
                        error="config.yaml changed while you were looking at the change. The difference below is "
                              "against the file as it is now: check it, then save again.")
    except OSError as e:
        return ctx.render("message.html", status=500, title="Not saved", message=f"config.yaml was not written: "
                                                                                 f"{e.strerror or e}")
    if not saved:
        return ctx.redirect(origin, "Nothing changed: config.yaml is already this text.")
    words = "Saved. The runner reads config.yaml when it starts: restart it to apply."
    if sf.needs_setup(current.text, text, _path(ctx).parent):
        words = "Saved. This change is read by setup.sh: run setup, then restart the runner."
    return ctx.redirect("/settings/", words)


def history(ctx: Ctx) -> Response:
    ctx.require("settings.view")
    current = files.read(_path(ctx))
    loaded = _loaded(ctx, current.sha)
    return ctx.render("settings_history.html", title="Settings history", versions=ctx.store.versions(KIND, 200),
                      current_sha=current.sha, loaded=loaded)


def version(ctx: Ctx) -> Response:
    ctx.require("settings.view")
    value = ctx.param("id")
    if not value.isdigit():
        raise Gone()
    v = ctx.store.version_of(KIND, int(value))
    if v is None:
        raise Gone()
    current = files.read(_path(ctx))
    return ctx.render("settings_version.html", title=f"Version {v['id']}", v=v,
                      diff=files.diff(v["before_text"] or "", v["after_text"], "before", "saved"),
                      against_now=files.diff(current.text, v["after_text"], "config.yaml now", "this version"),
                      current_sha=current.sha, can_edit=ctx.can("settings.edit"))


def routes() -> list[Route]:
    return [Route("/settings/", page(overview)),
            Route("/settings/yaml/", page(yaml_editor), methods=["GET", "POST"]),
            Route("/settings/save", page(save), methods=["POST"]),
            Route("/settings/history/", page(history)),
            Route("/settings/history/{id}/", page(version)),
            Route("/settings/{name}/", page(section), methods=["GET", "POST"])]

