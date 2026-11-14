"""The console's sign-in, registration, approval, roles and the rules every form goes through."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from arceus_runner.console import cli as console_cli  # noqa: E402
from arceus_runner.console.store import ConsoleStore  # noqa: E402
from arceus_runner.console.web import Settings, create_app  # noqa: E402

from tests.conftest import example_raw  # noqa: E402

PASSWORD = "correct horse battery"


@pytest.fixture
def setup(tmp_path: Path):
    raw = example_raw()
    raw["paths"] = {"db": str(tmp_path / "runner.db"), "secrets": str(tmp_path / "secrets.env")}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    settings = Settings(config_path=config, db_path=tmp_path / "console" / "console.db")
    return settings


def client(settings: Settings, **kw) -> TestClient:
    if "x-real-ip" in (kw.get("headers") or {}):
        kw.setdefault("client", ("127.0.0.1", 50000))  # as nginx on the machine, whose X-Real-IP is believed
    return TestClient(create_app(settings), follow_redirects=False, **kw)


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf field on the page"
    return m.group(1)


def post(c: TestClient, path: str, data: dict, page: str | None = None) -> object:
    """A form sent the way a browser sends it from the console's own page: the CSRF token of that page."""
    html = c.get(page or path, follow_redirects=True).text
    return c.post(path, data={**data, "csrf": csrf_of(html)})


def make_admin(settings: Settings, email: str = "owner@example.com") -> None:
    with ConsoleStore(settings.db_path) as s:
        token = s.make_link("invite", email, {"admin"}, actor="test")
        s.use_link(token, PASSWORD, "Owner")


def signed_in(settings: Settings, email: str, password: str = PASSWORD) -> TestClient:
    c = client(settings)
    r = post(c, "/login", {"email": email, "password": password})
    assert r.status_code == 303, r.text
    return c


def register(c: TestClient, email: str, name: str = "Quinn QA") -> object:
    return post(c, "/register", {"name": name, "email": email, "password": PASSWORD, "password2": PASSWORD,
                                 "note": "QA on the project"})


def user_id(settings: Settings, email: str) -> int:
    with ConsoleStore(settings.db_path) as s:
        u = s.user_by_email(email)
        assert u
        return u.id


def test_without_a_session_every_page_sends_to_the_sign_in(setup):
    c = client(setup)
    for path in ("/", "/users/", "/account/", "/users/1/"):
        r = c.get(path)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/login?next=")
    assert c.get("/healthz").text == "ok"
    r = c.get("/login")
    assert r.status_code == 200 and "Sign in" in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_a_registration_waits_for_an_admin_and_signs_in_only_once_approved(setup):
    make_admin(setup)
    c = client(setup)
    r = register(c, "quinn@example.com")
    assert r.status_code == 200 and "waiting for an admin" in r.text
    r = post(c, "/login", {"email": "quinn@example.com", "password": PASSWORD})
    assert r.status_code == 403 and "waiting for an admin" in r.text
    assert c.get("/users/").status_code == 303  # still signed out

    admin = signed_in(setup, "owner@example.com")
    page = admin.get("/users/").text
    assert "Waiting for approval" in page and "Quinn QA" in page and "QA on the project" in page
    r = post(admin, f"/users/{user_id(setup, 'quinn@example.com')}/", {"what": "approve", "role": ["qa"]},
             page="/users/")
    assert r.status_code == 303
    q = signed_in(setup, "quinn@example.com")
    assert q.get("/account/").status_code == 200
    assert "QA" in q.get("/account/").text


def test_registering_an_address_that_has_an_account_says_the_same_and_changes_nothing(setup):
    make_admin(setup)
    r = register(client(setup), "owner@example.com", name="Mallory")
    assert r.status_code == 200 and "waiting for an admin" in r.text
    with ConsoleStore(setup.db_path) as s:
        assert s.user_by_email("owner@example.com").name == "Owner"
        assert s.pending_count() == 0


def test_only_admins_manage_people_whatever_the_page_shows(setup):
    make_admin(setup)
    c = client(setup)
    register(c, "ops@example.com", name="Olga Ops")
    admin = signed_in(setup, "owner@example.com")
    post(admin, f"/users/{user_id(setup, 'ops@example.com')}/", {"what": "approve", "role": ["operator", "qa"]},
         page="/users/")
    ops = signed_in(setup, "ops@example.com")
    assert ops.get("/users/").status_code == 403
    page = ops.get("/account/").text
    # A direct POST with a valid CSRF token of its own page is still refused: the role is checked, not the button.
    r = ops.post(f"/users/{user_id(setup, 'owner@example.com')}/", data={"what": "disable", "csrf": csrf_of(page)})
    assert r.status_code == 403
    with ConsoleStore(setup.db_path) as s:
        assert s.user_by_email("owner@example.com").status == "active"
    assert "People" not in ops.get("/account/").text.split("<main>")[0]


def test_roles_combine_and_a_change_applies_on_the_next_page(setup):
    make_admin(setup)
    register(client(setup), "a@example.com", name="Ann")
    admin = signed_in(setup, "owner@example.com")
    uid = user_id(setup, "a@example.com")
    post(admin, f"/users/{uid}/", {"what": "approve", "role": ["qa"]}, page="/users/")
    ann = signed_in(setup, "a@example.com")
    assert ann.get("/users/").status_code == 403
    post(admin, f"/users/{uid}/", {"what": "roles", "role": ["qa", "admin"]})
    assert ann.get("/users/").status_code == 200  # the same session, no new sign-in
    with ConsoleStore(setup.db_path) as s:
        assert s.user(uid).roles == {"qa", "admin"}


def test_disabling_ends_the_sessions_at_once_and_the_last_admin_stays(setup):
    make_admin(setup)
    register(client(setup), "b@example.com", name="Bob")
    admin = signed_in(setup, "owner@example.com")
    uid = user_id(setup, "b@example.com")
    post(admin, f"/users/{uid}/", {"what": "approve", "role": ["admin"]}, page="/users/")
    bob = signed_in(setup, "b@example.com")
    assert bob.get("/users/").status_code == 200

    owner_id = user_id(setup, "owner@example.com")
    # Bob takes over: the first admin is disabled, and their session stops working at once.
    r = post(bob, f"/users/{owner_id}/", {"what": "disable"})
    assert r.status_code == 303
    assert admin.get("/users/").status_code == 303
    assert post(client(setup), "/login", {"email": "owner@example.com", "password": PASSWORD}).status_code == 403
    # Bob is now the last active admin: he can neither drop the role nor disable himself.
    r = post(bob, f"/users/{uid}/", {"what": "roles", "role": ["qa"]})
    assert r.status_code == 400 and "last active admin" in r.text
    r = post(bob, f"/users/{uid}/", {"what": "disable"})
    assert r.status_code == 400 and "last active admin" in r.text
    with ConsoleStore(setup.db_path) as s:
        assert [a.email for a in s.active_admins()] == ["b@example.com"]


def test_a_reset_link_sets_a_new_password_once_and_signs_out_the_old_sessions(setup):
    make_admin(setup)
    register(client(setup), "c@example.com", name="Cid")
    admin = signed_in(setup, "owner@example.com")
    uid = user_id(setup, "c@example.com")
    post(admin, f"/users/{uid}/", {"what": "approve", "role": ["architect"]}, page="/users/")
    cid = signed_in(setup, "c@example.com")
    r = post(admin, f"/users/{uid}/", {"what": "reset"})
    link = re.search(r'value="(http://testserver/link/[^"]+)"', r.text).group(1)
    path = link.removeprefix("http://testserver")
    other = client(setup)
    r = post(other, path, {"password": "a brand new password", "password2": "a brand new password"})
    assert r.status_code == 303
    assert cid.get("/account/").status_code == 303  # signed out
    assert other.get(path).status_code == 410  # used
    assert post(client(setup), "/login", {"email": "c@example.com", "password": PASSWORD}).status_code == 401
    signed_in(setup, "c@example.com", "a brand new password")


def test_wrong_passwords_are_throttled_even_for_the_right_one_next(setup):
    make_admin(setup)
    c = client(setup)
    for _ in range(5):
        assert post(c, "/login", {"email": "owner@example.com", "password": "nope nope nope"}).status_code == 401
    r = post(c, "/login", {"email": "owner@example.com", "password": PASSWORD})
    assert r.status_code == 429 and "Too many failed attempts" in r.text


def test_a_form_from_another_site_or_without_its_token_is_refused(setup):
    make_admin(setup)
    admin = signed_in(setup, "owner@example.com")
    page = admin.get("/account/").text
    token = csrf_of(page)
    assert admin.post("/account/", data={"what": "sessions"}).status_code == 403
    assert admin.post("/account/", data={"what": "sessions", "csrf": "forged"}).status_code == 403
    r = admin.post("/account/", data={"what": "sessions", "csrf": token}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = admin.post("/account/", data={"what": "sessions", "csrf": token}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert admin.post("/account/", data={"what": "sessions", "csrf": token}).status_code == 303
    # Signing in from another site's form is refused too.
    c = client(setup)
    login_token = csrf_of(c.get("/login").text)
    r = c.post("/login", data={"email": "owner@example.com", "password": PASSWORD, "csrf": login_token},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_behind_https_the_cookie_is_secure_host_only_and_not_for_scripts(setup):
    make_admin(setup)
    s = Settings(config_path=setup.config_path, db_path=setup.db_path, public_url="https://console.example.com")
    c = TestClient(create_app(s), base_url="https://console.example.com", follow_redirects=False)
    token = csrf_of(c.get("/login").text)
    r = c.post("/login", data={"email": "owner@example.com", "password": PASSWORD, "csrf": token},
               headers={"Origin": "https://console.example.com"})
    assert r.status_code == 303
    cookie = [v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("__Host-arceus=")][0]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Domain" not in cookie


def test_sign_in_goes_back_only_to_a_page_of_the_console(setup):
    make_admin(setup)
    c = client(setup)
    for nxt, where in (("/users/", "/users/"), ("//evil.example/x", "/"), ("https://evil.example", "/"),
                       ("/\\evil.example", "/")):
        c.cookies.clear()
        r = post(c, "/login?next=" + nxt, {"email": "owner@example.com", "password": PASSWORD, "next": nxt},
                 page="/login")
        assert r.headers["location"] == where


def test_the_server_command_invites_the_first_admin_and_resets_a_password(setup, capsys):
    config = setup.config_path
    raw = yaml.safe_load(config.read_text())
    raw["console"] = {"enabled": True, "public_url": "https://console.example.com"}
    config.write_text(yaml.safe_dump(raw))
    assert console_cli.main(["--config", str(config), "console", "invite", "--admin", "boss@example.com"]) == 0
    out = capsys.readouterr().out
    link = re.search(r"https://console\.example\.com(/link/\S+)", out).group(1)
    c = client(setup)
    assert "Create your account" in c.get(link).text
    post(c, link, {"name": "Boss", "password": PASSWORD, "password2": PASSWORD})
    admin = signed_in(setup, "boss@example.com")
    assert admin.get("/users/").status_code == 200

    assert console_cli.main(["--config", str(config), "console", "reset-password", "boss@example.com"]) == 0
    link = re.search(r"https://console\.example\.com(/link/\S+)", capsys.readouterr().out).group(1)
    assert "Choose a new password" in c.get(link).text
    assert console_cli.main(["--config", str(config), "console", "reset-password", "nobody@example.com"]) == 1
    assert console_cli.main(["--config", str(config), "console", "users"]) == 0
    assert "boss@example.com\tBoss\tactive\tadmin" in capsys.readouterr().out


def test_env_for_setup_names_the_address_the_folders_to_read_and_the_secrets_to_hide(setup, capsys):
    raw = yaml.safe_load(setup.config_path.read_text())
    raw["console"] = {"enabled": True, "public_url": "https://console.example.com", "port": 8080}
    raw["qa"] = {"enabled": True, "reports": "/srv/qa-runs"}
    setup.config_path.write_text(yaml.safe_dump(raw))
    assert console_cli.main(["--config", str(setup.config_path), "console", "env"]) == 0
    lines = [line.split("=", 1) for line in capsys.readouterr().out.splitlines()]
    env = dict(lines)
    assert env["CONSOLE_PUBLIC_URL"] == "https://console.example.com" and env["CONSOLE_PORT"] == "8080"
    assert env["CONSOLE_LISTEN"] == ""  # every address, as before
    assert ["CONSOLE_READ_DIR", "/srv/qa-runs"] in lines
    assert ["CONSOLE_SECRET_FILE", str(setup.config_path.parent / "secrets.env")] in lines


def test_the_console_section_needs_its_address_when_on(setup):
    from arceus_runner.config import ConfigError, load_config
    raw = yaml.safe_load(setup.config_path.read_text())
    raw["console"] = {"enabled": True}
    setup.config_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="public_url: required"):
        load_config(setup.config_path)
    raw["console"] = {"enabled": True, "public_url": "https://c.example.com", "trusted_proxies": ["nope"]}
    setup.config_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="not an address"):
        load_config(setup.config_path)


def test_wrong_current_passwords_on_the_account_page_are_throttled_with_a_message_not_an_error(setup):
    make_admin(setup)
    admin = signed_in(setup, "owner@example.com")
    form = {"what": "password", "current": "not my password", "password": "a brand new password",
            "password2": "a brand new password"}
    for _ in range(5):
        r = post(admin, "/account/", form)
        assert r.status_code == 400 and "The current password is wrong." in r.text
    r = post(admin, "/account/", {**form, "current": PASSWORD})
    assert r.status_code == 429 and "Too many failed attempts" in r.text
    assert "<form" in r.text  # the page itself, still signed in
    assert admin.get("/account/").status_code == 200
    with ConsoleStore(setup.db_path) as s:  # the password did not change (signing in waits too, by design)
        from arceus_runner.console.store import check_password
        row = s.conn.execute("SELECT password FROM users WHERE email = 'owner@example.com'").fetchone()
        assert check_password(PASSWORD, row["password"])


def test_a_strangers_wrong_passwords_do_not_keep_the_owner_out(setup):
    make_admin(setup)
    attacker = client(setup, headers={"x-real-ip": "6.6.6.6"})  # through nginx, which says who is asking
    for _ in range(12):
        post(attacker, "/login", {"email": "owner@example.com", "password": "nope nope nope"})
    r = post(attacker, "/login", {"email": "owner@example.com", "password": PASSWORD})
    assert r.status_code == 429  # the attacker's own address waits
    signed_in(setup, "owner@example.com")  # the owner, from their own address, gets in


def test_one_address_cannot_fill_the_list_of_requests(setup):
    make_admin(setup)
    c = client(setup, headers={"x-real-ip": "7.7.7.7"})
    for i in range(5):
        assert register(c, f"p{i}@example.com", name=f"P {i}").status_code == 200
    r = register(c, "p5@example.com", name="P 5")
    assert r.status_code == 400 and "Too many requests for accounts" in r.text
    with ConsoleStore(setup.db_path) as s:
        assert s.pending_count() == 5
    assert register(client(setup, headers={"x-real-ip": "8.8.8.8"}), "q@example.com").status_code == 200


@pytest.mark.parametrize("email", ["a\tb@example.com", "a\x00b@example.com", "pétr@example.com", "a b@example.com",
                                   "a@b", "a@.example.com", "a@example..com", "\n@example.com"])
def test_an_address_that_could_split_a_header_is_refused(setup, email):
    from arceus_runner.console.store import valid_email
    assert not valid_email(email)


def test_a_name_keeps_no_control_characters(setup):
    make_admin(setup)
    with ConsoleStore(setup.db_path) as s:
        u = s.register("n@example.com", "Ann\x01\x07 ‮Smith", PASSWORD)
        assert u.name == "Ann ‮Smith".replace("‮", "") or "\x01" not in u.name
        assert all(ch.isprintable() for ch in u.name)


def test_setup_gets_no_line_it_did_not_mean(setup, capsys):
    raw = yaml.safe_load(setup.config_path.read_text())
    raw["console"] = {"enabled": True, "public_url": "https://c.example.com"}
    raw["qa"] = {"enabled": True, "reports": "/srv/x\nCONSOLE_READ_DIR=/"}
    setup.config_path.write_text(yaml.safe_dump(raw))
    assert console_cli.main(["--config", str(setup.config_path), "console", "env"]) == 2
    out = capsys.readouterr()
    assert "CONSOLE_READ_DIR=/\n" not in out.out and "line break" in out.err


def test_behind_https_browsers_are_told_to_stay_on_https(setup):
    s = Settings(config_path=setup.config_path, db_path=setup.db_path, public_url="https://console.example.com")
    r = TestClient(create_app(s), base_url="https://console.example.com").get("/login")
    assert r.headers["strict-transport-security"] == "max-age=31536000"
    assert "strict-transport-security" not in client(setup).get("/login").headers


def test_console_listen_takes_an_address_and_is_handed_to_setup(setup, capsys):
    from arceus_runner.config import ConfigError, load_config
    raw = yaml.safe_load(setup.config_path.read_text())
    raw["console"] = {"enabled": True, "public_url": "https://console.example.com", "port": 8190, "listen": "172.18.0.1"}
    setup.config_path.write_text(yaml.safe_dump(raw))
    assert console_cli.main(["--config", str(setup.config_path), "console", "env"]) == 0
    env = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert env["CONSOLE_LISTEN"] == "172.18.0.1" and env["CONSOLE_PORT"] == "8190"
    raw["console"]["listen"] = "traefik"
    setup.config_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="not an IP address"):
        load_config(setup.config_path)
