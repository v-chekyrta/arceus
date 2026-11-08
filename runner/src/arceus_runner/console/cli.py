"""`arceus-runner console ...`: the console from the server's shell.

- `serve` starts the web server (what the console's container runs).
- `invite EMAIL [--admin] [--role R]` prints a one-time link that makes an account: the first admin comes from here.
- `reset-password EMAIL` prints a one-time link to choose a new password; the way back in when nobody can sign in.
- `users` lists the accounts. `env` prints what setup.sh needs to install the console, from config.yaml.

The commands that touch accounts need only console.db, so they work from the runner's virtualenv on the machine
(no web packages there) and while the console's container is down.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from arceus_runner.config import Config, ConfigError, load_config
from arceus_runner.console.store import ROLES, ConsoleError, ConsoleStore


def db_path(config_path: Path) -> Path:
    return config_path.parent / "console" / "console.db"


def _public_url(config_path: Path, given: str | None) -> str:
    if given:
        return given.rstrip("/")
    try:
        cfg = load_config(config_path)
    except ConfigError:
        cfg = None
    if cfg and cfg.console.public_url:
        return cfg.console.public_url
    return ""


def _print_link(url: str, token: str, what: str) -> None:
    if url:
        print(f"{what}: {url}/link/{token}")
    else:
        print(f"{what}: <console address>/link/{token}")
        print("(console.public_url is not set in config.yaml: put the console's address in front of /link/)")
    print("It works once, for 48 hours. Send it to the person yourself.")


def cmd_invite(args, config_path: Path) -> int:
    roles = set(args.role or [])
    if args.admin:
        roles.add("admin")
    with ConsoleStore(db_path(config_path)) as store:
        try:
            token = store.make_link("invite", args.email, roles, actor="server: arceus-runner console invite")
        except ConsoleError as e:
            print(f"console: {e}", file=sys.stderr)
            return 1
    _print_link(_public_url(config_path, args.url), token, f"Invitation for {args.email}")
    return 0


def cmd_reset(args, config_path: Path) -> int:
    with ConsoleStore(db_path(config_path)) as store:
        user = store.user_by_email(args.email)
        try:
            if user and user.status == "disabled" and args.enable:
                store.set_status(user.id, "active", None, actor="server: arceus-runner console reset-password")
            token = store.make_link("reset", args.email, actor="server: arceus-runner console reset-password")
        except ConsoleError as e:
            hint = " (--enable enables it too)" if user and user.status == "disabled" else ""
            print(f"console: {e}{hint}", file=sys.stderr)
            return 1
    _print_link(_public_url(config_path, args.url), token, f"Password reset for {args.email}")
    return 0


def cmd_users(_args, config_path: Path) -> int:
    with ConsoleStore(db_path(config_path)) as store:
        users = store.users()
    if not users:
        print("No accounts yet. `arceus-runner console invite --admin EMAIL` makes the first admin.")
        return 0
    for u in users:
        roles = ", ".join(r for r in ROLES if r in u.roles) or "viewer"
        print(f"{u.email}\t{u.name}\t{u.status}\t{roles}\tlast sign-in {u.last_login_at or 'never'}")
    return 0


def env_lines(cfg: Config) -> list[str]:
    """What the console's compose project and nginx need, from config.yaml (setup.sh writes them into .env)."""
    c = cfg.console
    return [f"CONSOLE_ENABLED={'1' if c.enabled else '0'}",
            f"CONSOLE_PUBLIC_URL={c.public_url or ''}",
            f"CONSOLE_PORT={c.port}",
            f"CONSOLE_TRUSTED_PROXIES={' '.join(c.trusted_proxies)}",
            f"CONSOLE_SESSION_IDLE={int(c.session_idle.total_seconds())}s",
            f"CONSOLE_SESSION_MAX={int(c.session_max.total_seconds())}s",
            # Folders the console reads (mounted read-only at the same path when outside the data folder), and the
            # secret files it must never see; one line each.
            *(f"CONSOLE_READ_DIR={p}" for p in _read_dirs(cfg)),
            *(f"CONSOLE_SECRET_FILE={p}" for p in (cfg.paths.secrets, cfg.paths.bulava_env))]


def _read_dirs(cfg: Config) -> list[Path]:
    """Folders the pages read: the runner's database's, the reports', the instructions'. Each once, the outermost."""
    wanted = [cfg.paths.db.parent, cfg.qa.reports, cfg.architect.reports, cfg.qa.instructions.parent,
              cfg.architect.instructions.parent, *([cfg.paths.project_notes.parent] if cfg.paths.project_notes else [])]
    out: list[Path] = []
    for p in sorted({w.resolve() for w in wanted}, key=lambda p: len(p.parts)):
        if not any(p == o or o in p.parents for o in out):
            out.append(p)
    return out


def cmd_env(_args, config_path: Path) -> int:
    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        for p in e.problems:
            print(f"config: {p}", file=sys.stderr)
        return 2
    print("\n".join(env_lines(cfg)))
    return 0


def cmd_serve(args, config_path: Path) -> int:
    import os

    import uvicorn

    from arceus_runner.console.web import create_app
    os.environ.setdefault("ARCEUS_RUNNER_CONFIG", str(config_path))
    uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=False, server_header=False,
                access_log=False, log_level="warning", timeout_graceful_shutdown=5)
    return 0


def add_parser(sub) -> None:
    c = sub.add_parser("console", help="the web console: start it, invite people, reset a password")
    csub = c.add_subparsers(dest="action", required=True)
    s = csub.add_parser("serve", help="start the web server (the console's container runs this)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8180)
    s.set_defaults(console_fn=cmd_serve)
    i = csub.add_parser("invite", help="a one-time link that makes an account, active at once")
    i.add_argument("email")
    i.add_argument("--admin", action="store_true", help="an admin: may do everything, including inviting others")
    i.add_argument("--role", action="append", choices=ROLES, help="a role to give (repeat for more)")
    i.add_argument("--url", help="the console's address, if config.yaml has no console.public_url")
    i.set_defaults(console_fn=cmd_invite)
    r = csub.add_parser("reset-password", help="a one-time link to choose a new password")
    r.add_argument("email")
    r.add_argument("--enable", action="store_true", help="enable the account too, if it is disabled")
    r.add_argument("--url", help="the console's address, if config.yaml has no console.public_url")
    r.set_defaults(console_fn=cmd_reset)
    u = csub.add_parser("users", help="list the accounts")
    u.set_defaults(console_fn=cmd_users)
    e = csub.add_parser("env", help="what setup.sh needs to install the console, from config.yaml")
    e.set_defaults(console_fn=cmd_env)
    c.set_defaults(fn=run)


def run(args) -> int:
    import os
    config_path = Path(args.config or os.environ.get("ARCEUS_RUNNER_CONFIG") or "~/arceus/data/config.yaml").expanduser()
    return args.console_fn(args, config_path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arceus-runner")
    p.add_argument("--config")
    add_parser(p.add_subparsers(dest="cmd", required=True))
    args = p.parse_args(argv)
    return args.fn(args)
