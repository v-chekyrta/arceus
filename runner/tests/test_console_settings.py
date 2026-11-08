"""Settings in the console: config.yaml as a form and as YAML, the difference before saving, the runner's own check,
only the changed lines written, conflicts, history and putting a version back, and who may see or change it."""

from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
from pathlib import Path

import pytest
import yaml

pytest.importorskip("starlette")

from arceus_runner.config import Config  # noqa: E402
from arceus_runner.console import settings_form as sf  # noqa: E402
from arceus_runner.console.store import ConsoleStore  # noqa: E402
from arceus_runner.console.web import Settings  # noqa: E402
from arceus_runner.db import ReviewStore  # noqa: E402

from tests.conftest import EXAMPLE  # noqa: E402
from tests.test_console_auth import csrf_of, make_admin, signed_in  # noqa: E402
from tests.test_console_qa import person  # noqa: E402


@pytest.fixture
def w(tmp_path: Path):
    config = tmp_path / "config.yaml"
    text = EXAMPLE.read_text().replace("~/arceus/data/runner.db", str(tmp_path / "runner.db"))
    config.write_text(text)
    config.chmod(0o600)
    settings = Settings(config_path=config, db_path=tmp_path / "console" / "console.db")
    make_admin(settings)
    ReviewStore(tmp_path / "runner.db").close()

    class W:
        pass
    x = W()
    x.settings, x.config, x.db = settings, config, tmp_path / "runner.db"
    return x


def form_values(page: str) -> list[tuple[str, str]]:
    """What the browser sends for the section's form as it is: every input, select, textarea and checked box."""
    form = page.split('class="card settings-form"', 1)[1].split("</form>", 1)[0]
    out = []
    for m in re.finditer(r'<input([^>]*)>', form):
        attrs = m.group(1)
        name = re.search(r'name="([^"]+)"', attrs)
        if not name:
            continue
        kind = re.search(r'type="([^"]+)"', attrs)
        value = re.search(r'value="([^"]*)"', attrs)
        if kind and kind.group(1) == "checkbox":
            if " checked" in attrs:
                out.append((name.group(1), html.unescape(value.group(1)) if value else "on"))
            continue
        out.append((name.group(1), html.unescape(value.group(1)) if value else ""))
    for m in re.finditer(r'<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', form, re.S):
        out.append((m.group(1), html.unescape(m.group(2))))
    for m in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', form, re.S):
        chosen = re.search(r"<option(?: value=\"([^\"]*)\")? selected>([^<]*)</option>", m.group(2))
        out.append((m.group(1), html.unescape(chosen.group(1) if chosen.group(1) is not None else chosen.group(2))))
    return out


def send(c, path: str, pairs: list[tuple[str, str]]) -> object:
    """A form with repeated names (checkboxes), encoded as a browser sends it."""
    return c.post(path, content=urllib.parse.urlencode(pairs).encode(),
                  headers={"Content-Type": "application/x-www-form-urlencoded"})


def change(c, section: str, **values) -> object:
    """Open a section, change some values as a person would, and send it for checking."""
    page = c.get(f"/settings/{section}/").text
    sent = [(k, v) for k, v in form_values(page) if k not in values]
    for k, v in values.items():
        sent += [(k, x) for x in v] if isinstance(v, list) else [(k, v)]
    return send(c, f"/settings/{section}/", sent)


def save_preview(c, preview: str, note: str = "") -> object:
    sha = re.search(r'name="sha" value="([^"]+)"', preview).group(1)
    text = html.unescape(re.search(r'<textarea name="text" hidden>(.*?)</textarea>', preview, re.S).group(1))
    origin = re.search(r'name="origin" value="([^"]+)"', preview).group(1)
    return c.post("/settings/save", data={"csrf": csrf_of(preview), "sha": sha, "text": text, "origin": origin,
                                          "note": note})


def test_every_setting_of_the_schema_is_in_the_form(w):
    admin = signed_in(w.settings, "owner@example.com")
    cfg = Config.model_validate(yaml.safe_load(w.config.read_text()))
    names = set()
    for section, fields in sf.sections(cfg, w.config.read_text()):
        page = admin.get(f"/settings/{section}/").text
        for f in sf.flatten(fields):
            assert f'name="{f.name}"' in page, f.name
            names.add(f.name)

    def leaves(model, value, prefix):
        for name, info in model.model_fields.items():
            key = info.alias or name
            v = getattr(value, name)
            if isinstance(v, Config.__mro__[1]) and f"{prefix}{key}" not in names:  # a nested model: its fields
                yield from leaves(type(v), v, f"{prefix}{key}.")
            else:
                yield f"{prefix}{key}"
    missing = [n for n in leaves(Config, cfg, "") if n not in names and not any(n.startswith(x + ".") for x in names)]
    assert missing == []


def test_a_change_shows_its_difference_and_then_writes_only_its_lines(w):
    before = w.config.read_text()
    admin = signed_in(w.settings, "owner@example.com")
    r = change(admin, "schedule", **{"schedule.poll_working": "3m", "schedule.working_hours.from": "08:30"})
    assert r.status_code == 200
    assert "-  poll_working: 2m" in r.text and "+  poll_working: 3m" in r.text
    assert "restart the runner" in r.text and "Saving restarts nothing" in r.text
    assert w.config.read_text() == before  # nothing written yet
    r = save_preview(admin, r.text, "fewer polls")
    assert r.status_code == 303 and "restart" in r.headers["location"]
    after = w.config.read_text()
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert changed == [('  working_hours: { days: [mon, tue, wed, thu, fri], from: "09:00", to: "19:00" }',
                        '  working_hours: { days: [mon, tue, wed, thu, fri], from: "08:30", to: "19:00" }'),
                       ("  poll_working: 2m", "  poll_working: 3m")]
    assert len(before.splitlines()) == len(after.splitlines())
    assert w.config.stat().st_mode & 0o777 == 0o600
    with ConsoleStore(w.settings.db_path) as cs:
        v = cs.versions("config")[0]
    assert v["note"] == "fewer polls" and v["before_text"] == before and v["after_text"] == after


def test_nothing_changed_leaves_the_file_alone(w):
    before = w.config.read_bytes()
    mtime = w.config.stat().st_mtime_ns
    admin = signed_in(w.settings, "owner@example.com")
    for section in ("general", "tracker", "code_host", "schedule", "bulava", "cleanup", "paths"):
        r = change(admin, section)
        assert r.status_code == 200 and "Nothing changed" in r.text, section
    assert w.config.read_bytes() == before and w.config.stat().st_mtime_ns == mtime


def test_a_value_the_runner_refuses_shows_its_error_and_cannot_be_saved(w):
    before = w.config.read_text()
    admin = signed_in(w.settings, "owner@example.com")
    r = change(admin, "schedule", **{"schedule.poll_working": "soon"})
    assert "The runner would refuse this config.yaml" in r.text and "schedule.poll_working" in r.text
    assert "Save config.yaml" not in r.text
    # Sent anyway, straight to save: refused there too.
    page = admin.get("/settings/yaml/").text
    sha = re.search(r'name="sha" value="([^"]+)"', page).group(1)
    r = admin.post("/settings/save", data={"csrf": csrf_of(page), "sha": sha, "origin": "/settings/yaml/",
                                           "text": before.replace("mode: observe", "mode: sometimes")})
    assert "The runner would refuse" in r.text
    assert w.config.read_text() == before


def test_a_text_value_yaml_would_read_otherwise_is_written_quoted(w):
    admin = signed_in(w.settings, "owner@example.com")
    r = change(admin, "tracker", **{"tracker.tags.queue": "off"})
    assert "+    queue: &#34;off&#34;" in r.text
    assert save_preview(admin, r.text).status_code == 303
    assert yaml.safe_load(w.config.read_text())["tracker"]["tags"]["queue"] == "off"


def test_a_save_over_a_file_changed_meanwhile_is_refused_with_the_difference(w):
    admin = signed_in(w.settings, "owner@example.com")
    r = change(admin, "limits", **{"limits.max_review_rounds": "7"})
    assert "Save config.yaml" in r.text
    w.config.write_text(w.config.read_text().replace("mode: observe", "mode: live\nsecurity_note: x")
                        .replace("security_note: x\n", ""))  # another admin, over SSH
    r2 = save_preview(admin, r.text)
    assert r2.status_code == 409 and "changed while you were looking" in r2.text
    assert "max_review_rounds: 7" not in w.config.read_text()
    # A section opened before the file changed: its form is refused too.
    page = admin.get("/settings/limits/").text
    w.config.write_text(w.config.read_text() + "\n")
    r3 = send(admin, "/settings/limits/", form_values(page))
    assert r3.status_code == 409 and "changed since you opened this section" in r3.text


def test_the_whole_file_as_yaml_and_putting_a_version_back(w):
    original = w.config.read_text()
    admin = signed_in(w.settings, "owner@example.com")
    page = admin.get("/settings/yaml/").text
    sha = re.search(r'name="sha" value="([^"]+)"', page).group(1)
    edited = original.replace("max_ci_fix_attempts: 3", "max_ci_fix_attempts: 4")
    r = admin.post("/settings/yaml/", data={"csrf": csrf_of(page), "sha": sha, "text": edited, "note": "more CI"})
    assert "+  max_ci_fix_attempts: 4" in r.text
    assert save_preview(admin, r.text, "more CI").status_code == 303
    assert "max_ci_fix_attempts: 4" in w.config.read_text()
    with ConsoleStore(w.settings.db_path) as cs:
        vid = cs.versions("config")[0]["id"]
    page = admin.get("/settings/history/").text
    assert "more CI" in page and "Now in the file" in page
    page = admin.get(f"/settings/history/{vid}/").text
    assert "-  max_ci_fix_attempts: 3" in page and "+  max_ci_fix_attempts: 4" in page
    # Put back what was there before it: through the same check and difference.
    page = admin.get(f"/settings/yaml/?version={vid}&which=before").text
    assert "what was there before" in page and "max_ci_fix_attempts: 3" in page
    text = html.unescape(re.search(r'<textarea class="code" id="text" name="text"[^>]*>(.*?)</textarea>', page,
                                   re.S).group(1))
    r = admin.post("/settings/yaml/", data={"csrf": csrf_of(page), "text": text,
                                            "sha": re.search(r'name="sha" value="([^"]+)"', page).group(1)})
    assert "-  max_ci_fix_attempts: 4" in r.text
    assert save_preview(admin, r.text, "back").status_code == 303
    assert w.config.read_text() == original


def test_a_change_setup_applies_says_so(w):
    admin = signed_in(w.settings, "owner@example.com")
    r = change(admin, "console", **{"console.port": "8080"})
    assert "setup.sh" in r.text and "run setup, then restart the runner" in r.text
    r = save_preview(admin, r.text)
    assert "run+setup" in r.headers["location"] or "setup" in r.headers["location"]


def test_a_map_is_edited_as_yaml_in_place(w):
    admin = signed_in(w.settings, "owner@example.com")
    r = change(admin, "review_agent", **{"review_agent.repos": "services/api:\n  tests: none\n"})
    assert "services/api" in r.text and "Save config.yaml" in r.text
    assert save_preview(admin, r.text).status_code == 303
    assert yaml.safe_load(w.config.read_text())["review_agent"]["repos"] == {"services/api": {"tests": "none"}}


def test_the_page_says_when_the_runner_runs_with_another_config(w):
    admin = signed_in(w.settings, "owner@example.com")
    assert "Not known whether the runner runs with this config.yaml" in admin.get("/settings/").text
    s = ReviewStore(w.db)
    s.set_state("config", {"sha256": hashlib.sha256(w.config.read_bytes()).hexdigest(),
                           "loaded_at": "2026-11-08T10:00:00+00:00"})
    s.close()
    assert "The runner runs with this config.yaml" in admin.get("/settings/").text
    r = change(admin, "limits", **{"limits.max_review_rounds": "6"})
    save_preview(admin, r.text)
    page = admin.get("/settings/").text
    assert "has changed since the runner started" in page and "still runs with the earlier settings" in page


def test_operators_see_everything_read_only_and_others_nothing(w):
    op = person(w.settings, "op@example.com", {"operator"})
    page = op.get("/settings/schedule/").text
    assert "poll_working" in page and " readonly" in page and "Check the change" not in page
    assert send(op, "/settings/schedule/", form_values(page)).status_code == 403
    yaml_page = op.get("/settings/yaml/").text
    assert "readonly" in yaml_page and "Check the change" not in yaml_page
    assert op.post("/settings/save", data={"csrf": csrf_of(page), "text": "x", "sha": "y"}).status_code == 403
    for roles in (set(), {"qa"}, {"architect"}):
        c = person(w.settings, f"{'-'.join(sorted(roles)) or 'n'}@example.com", roles)
        for path in ("/settings/", "/settings/schedule/", "/settings/yaml/", "/settings/history/"):
            assert c.get(path).status_code == 403, (roles, path)


def test_secrets_are_never_shown(w, tmp_path):
    (tmp_path / "secrets.env").write_text("GITLAB_TOKEN=glpat-very-secret\n")
    admin = signed_in(w.settings, "owner@example.com")
    for path in ("/settings/", "/settings/paths/", "/settings/yaml/", "/settings/code_host/"):
        assert "glpat-very-secret" not in admin.get(path).text


def test_a_broken_config_can_be_fixed_as_yaml(w):
    w.config.write_text(w.config.read_text().replace("mode: observe", "mode: sometimes"))
    admin = signed_in(w.settings, "owner@example.com")
    page = admin.get("/settings/").text
    assert "config.yaml does not load" in page and "Fix it as YAML" in page
    page = admin.get("/settings/yaml/").text
    text = html.unescape(re.search(r'name="text"[^>]*>(.*?)</textarea>', page, re.S).group(1))
    r = admin.post("/settings/yaml/", data={"csrf": csrf_of(page), "text": text.replace("mode: sometimes", "mode: observe"),
                                            "sha": re.search(r'name="sha" value="([^"]+)"', page).group(1)})
    assert save_preview(admin, r.text).status_code == 303
    assert "mode: observe" in w.config.read_text()


def test_a_value_inside_a_flow_mapping_changes_that_line_only_in_its_style():
    """`git_author: { name: ..., email: ... }` as people write it: a new name changes that line, with the spaces
    inside the braces kept, and nothing else in the file."""
    from starlette.datastructures import FormData
    text = EXAMPLE.read_text()
    cfg = Config.model_validate(yaml.safe_load(text))
    secs = dict(sf.sections(cfg, text))
    items = []
    for f in sf.flatten(secs["code_host"]):
        if f.kind == "bool":
            if f.shown:
                items.append((f.name, "on"))
        elif f.kind == "lines":
            items.append((f.name, "\n".join(f.shown)))
        else:
            items.append((f.name, {"code_host.git_author.name": "ACME Bot"}.get(f.name, f.shown or "")))
    out = sf.apply(text, secs["code_host"], FormData(items))
    diff = [(a, b) for a, b in zip(text.splitlines(), out.text.splitlines()) if a != b]
    assert diff == [('  git_author: { name: "ACME Agent", email: "acme-agent-bot@example.com" }',
                     '  git_author: { name: "ACME Bot", email: "acme-agent-bot@example.com" }')]
    assert len(text.splitlines()) == len(out.text.splitlines())
