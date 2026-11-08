"""The background agents' runs from the console: a test run of the platform (qa) or an architecture review (arch).
Queuing and stopping one is what `arceus-runner qa|arch run|stop` does on the server, with the person's email as
who asked; the runner does the rest on its next pass. Their instructions are edited with files.py."""

from __future__ import annotations

from pathlib import Path

from starlette.responses import Response

from arceus_runner.config import Config
from arceus_runner.console import files
from arceus_runner.console.store import ConsoleError, User
from arceus_runner.console.web import Ctx, Denied
from arceus_runner.db import RUN_UNDER_WAY, ReviewStore
from arceus_runner.workspace_run import MAX_INSTRUCTIONS, read_instructions

KINDS = {"qa": ("qa", "test run", "Test runs"), "arch": ("architect", "architecture review", "Architecture reviews")}


STATE_TEXT = {"pending": "Queued", "preparing": "Starting", "running": "Running", "reviewing": "Second check",
              "restoring": "Putting the workspace back", "publishing": "Writing the report"}
RESULT_CLASS = {"passed": "passed", "clean": "passed", "found": "found", "partial": "partial", "blocked": "blocked",
                "unfinished": "unfinished", "stopped": "stopped", "unchanged": "stopped"}


def dress(rows: list[dict], head_of) -> list[dict]:
    """The list's rows with their words: under way, the step; done, the result as the reports word it."""
    for r in rows:
        r["state_text"] = STATE_TEXT.get(r["state"], r["state"])
        r["result_text"] = head_of(r.get("outcome"), r.get("findings") or 0) if r["state"] == "done" else ""
        r["result_class"] = RESULT_CLASS.get(r.get("outcome") or "", "")
    return sorted(rows, key=lambda r: r["n"], reverse=True)


def section(cfg: Config, kind: str):
    return getattr(cfg, KINDS[kind][0])


def start(cfg: Config, kind: str, user: User) -> int:
    """Queue a run; returns its number. Refused while one of the kind is queued or under way, or the kind is off."""
    name, noun, _ = KINDS[kind]
    if not section(cfg, kind).enabled:
        raise ConsoleError(f"{noun.capitalize()}s are off in config.yaml ({name}.enabled: false): nothing was queued.")
    try:
        store = ReviewStore.existing(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        raise ConsoleError(f"Nothing was queued: {e}") from None
    try:
        with store.transaction():
            under_way = store.runs(kind, *RUN_UNDER_WAY)
            if under_way:
                r = under_way[0]
                raise ConsoleError(f"Run {r.n} is {'queued' if r.state == 'pending' else 'under way'}: one at a time. "
                                   "Stop it first to start another.")
            run = store.add_run(kind, "command", user.email)
    finally:
        store.close()
    assert run is not None
    return run.n


def stop(cfg: Config, kind: str, n: int) -> None:
    try:
        store = ReviewStore.existing(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        raise ConsoleError(f"Nothing was stopped: {e}") from None
    try:
        run = store.run(f"{kind}-{n}")
        if run is None or run.state not in RUN_UNDER_WAY:
            raise ConsoleError(f"Run {n} is not queued or under way.")
        if not run.stop_requested:
            store.update_run(run.id, stop_requested=1)
    finally:
        store.close()


def file_page(ctx: Ctx, *, kind: str, action: str, path: Path, title: str, lede: str, back: tuple[str, str],
              required: bool, sensitive: bool = False) -> Response:
    """The page that shows and edits one of the factory's text files, for the people `action` allows."""
    if not ctx.can(action):
        raise Denied(action)
    user = ctx.user
    assert user is not None
    current = files.read(path)
    text, sha, error, notice, conflict = current.text, current.sha, current.problem, "", ""
    if ctx.request.method == "POST":
        text = ctx.field("text")
        try:
            if files.save(ctx.store, kind, path, text, ctx.field("sha"), user, ctx.field("note")):
                return ctx.redirect(ctx.path, "Saved. A run that starts from now on reads this version.")
            notice = "Nothing changed: the file is already this text."
        except files.Conflict as c:
            conflict, sha = c.diff, c.current.sha
            error = ("Someone changed the file since you opened it. The difference between what is on the server "
                     "now and your text is below; your text is still in the editor. Save again to replace theirs.")
        except OSError as e:
            error = f"Not saved: {e.strerror or e}"
            if getattr(e, "errno", None) == 30:  # read-only file system: a folder mounted read-only
                error = (f"Not saved: {path} is outside the data folder, which the console sees read-only. Edit it "
                         "on the server, or keep it in the data folder.")
    restoring = None
    if ctx.request.method == "GET" and ctx.query("version").isdigit():
        restoring = ctx.store.version_of(kind, int(ctx.query("version")))
        if restoring:
            text = restoring["after_text"]
    agent_gets, why = read_instructions(path, required=required)
    return ctx.render("file_edit.html", status=409 if conflict else 400 if error else 200,
                      title=title, lede=lede, path=path, text=text, sha=sha, exists=current.exists, error=error,
                      notice=notice or ctx.query("notice"), conflict=conflict, back=back, restoring=restoring,
                      versions=ctx.store.versions(kind), agent_ok=agent_gets is not None and bool(agent_gets.strip()),
                      agent_why=why, agent_bytes=len((agent_gets or "").encode()), limit=MAX_INSTRUCTIONS,
                      required=required, sensitive=sensitive, readonly=bool(current.problem))
