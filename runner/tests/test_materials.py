"""The ticket whole for the agent: people's comments and the files they attached, screened before the agent gets them."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from arceus_runner import materials
from arceus_runner.config import HttpConfig
from arceus_runner.http import ServiceClient, TooLarge
from arceus_runner.screening import MAX_CHARS, ClaudeClassifier, ScreenFile, calls, digest, file_blocks
from arceus_runner.youtrack import YouTrackConfig, YouTrackTracker

from .conftest import example_raw
from .test_delivery import World
from .test_review import DEBOUNCE, published
from .test_delivery import take
from .test_security import BLOCK, EVIL, asking, decide, hold_code, queue

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PDF = b"%PDF-1.4\n" + b"1 0 obj\n<<>>\nendobj\n"


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])


@pytest.fixture
def w(tmp_path, secrets):
    world = World(tmp_path, secrets)
    yield world
    world.events.close()
    world.store.close()


def dispatched(w: World) -> str:
    calls = w.calls("dispatch")
    assert len(calls) == 1, calls
    return calls[0]["argv"][-1]


def folder(w: World, key: str = "ACME-1"):
    return materials.folder(w.cfg.paths.workspace.resolve(), key)


def test_the_agent_gets_the_comments_and_every_file_people_attached(w):
    db_id = queue(w, description="Build the page as in ![](mockup.png).")
    w.yt.comment(db_id, "a.smith", "Also support dark mode; the spec is attached below.")
    w.yt.comment(db_id, "j.doe", "")  # a comment with nothing but a file
    files_only = w.yt.comments[db_id][-1]["id"]
    w.yt.attach(db_id, "j.doe", "mockup.png", PNG)
    w.yt.attach(db_id, "a.smith", "spec.pdf", PDF, files_only)
    w.yt.attach(db_id, "a.smith", "mockup.png", PNG + b"v2", files_only)  # the same name again, another file
    w.tick()

    text = dispatched(w)
    assert "Also support dark mode" in text and "a.smith, 2026-10-20" in text
    assert "(no text, only files: see below)" in text
    where = folder(w)
    on_disk = sorted(p.name for p in where.iterdir() if p.name != materials.MANIFEST)
    assert on_disk == ["74%2D1-mockup.png", "74%2D2-spec.pdf", "74%2D3-mockup.png"]
    assert (where / "74%2D1-mockup.png").read_bytes() == PNG
    assert (where / "74%2D3-mockup.png").read_bytes() == PNG + b"v2"
    for name in on_disk:
        assert str(where / name) in text
    assert "`mockup.png`, attached to the ticket by j.doe" in text
    assert "`spec.pdf`, attached to a comment by j.doe of 2026-10-20" in text

    # Screened in one go with the text, every author among those who may not release it.
    message = w.screen.calls[-1]
    assert [f.name for f in message["files"]] == on_disk
    assert {"j.doe", "a.smith"} <= set(message["author"].split(", "))

    manifest = json.loads((where / materials.MANIFEST).read_text())["items"]
    assert [(e["name"], e["status"]) for e in manifest] == [("mockup.png", "ok"), ("spec.pdf", "ok"),
                                                             ("mockup.png", "ok")]
    # The signed download address is a credential: it never reaches the agent, the manifest or the event log.
    for where_to_look in (text, (where / materials.MANIFEST).read_text(), w.cfg.paths.events.read_text()):
        assert "SECRETSIGN" not in where_to_look


def test_files_the_agent_cannot_get_are_named_with_the_reason(w):
    db_id = queue(w)
    w.yt.attach(db_id, "j.doe", "design.fig", b"figma")
    w.yt.attach(db_id, "j.doe", "huge.png", PNG, size=13 * 1024 * 1024)
    w.yt.attach(db_id, "j.doe", "liar.png", PNG + b"\x00" * (13 * 1024 * 1024), size=10)  # larger than it says
    w.yt.attach(db_id, "j.doe", "fake.png", b"not an image at all")
    w.yt.attach(db_id, "j.doe", "gone.png", PNG, url="/api/files/74-999?sign=SECRETSIGN74-999")
    hidden = w.yt.attach(db_id, "j.doe", "hidden.pdf", PDF)
    w.yt.fail[f"/api/files/{hidden}"] = 403
    w.yt.attach(db_id, "j.doe", "../../../etc/passwd.png", PNG)
    w.yt.attach(db_id, "j.doe", "old.png", PNG, removed=True)
    w.yt.attach(db_id, "j.doe", "draft.png", PNG, draft=True)
    w.tick()

    text = dispatched(w)
    assert "`design.fig`, attached to the ticket by j.doe: not given to you, .fig files cannot be read" in text
    assert "`huge.png`" in text and "larger than 12 MB" in text
    assert "`liar.png`, attached to the ticket by j.doe: not given to you, larger than 12 MB" in text
    assert "`fake.png`, attached to the ticket by j.doe: not given to you, its content is not png" in text
    assert "`gone.png`, attached to the ticket by j.doe: not given to you, it is gone from the tracker" in text
    assert "`hidden.pdf`, attached to the ticket by j.doe: not given to you, the runner's account may not read it" \
        in text
    assert "old.png" not in text and "draft.png" not in text
    assert "Never guess what it contains" in text
    # Only the one good file is on disk, inside the folder, under a safe name; nothing half-written.
    assert sorted(p.name for p in folder(w).iterdir()) == ["74%2D7-passwd.png", materials.MANIFEST]
    # A file of a kind the screen cannot read is not even downloaded; one file refused is not a tracker outage.
    assert not any(r.url.path == "/api/files/74-1" for r in w.yt.requests)
    assert w.runner.tracker.client.available()


def test_a_planted_instruction_in_a_file_holds_the_ticket_and_its_files(w):
    w.screen.rules.append((EVIL, BLOCK))
    db_id = queue(w)
    w.yt.comment(db_id, "mallory", "see the notes")
    w.yt.attach(db_id, "mallory", "notes.txt", EVIL.encode(), w.yt.comments[db_id][-1]["id"])
    w.tick()

    assert w.calls("dispatch") == []
    assert w.store.task("ACME-1").state == "security_hold"
    assert not folder(w).exists()  # the agent's workspace never had the file
    notice = w.yt.comments[db_id][-1]["text"]
    assert "`74%2D1-notes.txt`" in notice and "and these files" in notice

    decide(w, "allow", hold_code(w), who="mallory")  # the file's author cannot release it
    assert w.calls("dispatch") == []
    decide(w, "allow", hold_code(w))
    assert "notes.txt" in dispatched(w)
    assert (folder(w) / "74%2D1-notes.txt").read_bytes() == EVIL.encode()


def test_a_file_replaced_after_an_approval_is_screened_again(w):
    w.screen.rules.append(("secret plan", BLOCK))
    db_id = queue(w)
    fid = w.yt.attach(db_id, "j.doe", "plan.md", b"secret plan")
    w.tick()
    code = hold_code(w)
    w.yt.files[db_id][0]["data"] = b"secret plan, edited"
    w.yt.files[db_id][0]["size"] += 8
    decide(w, "allow", code)
    assert w.calls("dispatch") == []  # the approval was for the other bytes
    assert hold_code(w) != code and fid


def test_an_answer_with_a_file_carries_the_file(w):
    asking(w)
    w.yt.comment("2-1", "a.smith", "Like this, see the screenshot.")
    w.yt.attach("2-1", "a.smith", "expected.png", PNG, w.yt.comments["2-1"][-1]["id"])
    w.tick()
    text = w.calls("worker-send")[-1]["argv"][-1]
    assert text.startswith("Like this, see the screenshot.") and "expected.png" in text
    assert any(p.name.endswith("expected.png") for p in folder(w).iterdir())


def test_a_review_round_from_the_ticket_carries_its_files(w):
    published(w)
    w.yt.comment("2-1", "j.doe", "The button is the wrong colour, compare with the attached design.")
    w.yt.attach("2-1", "j.doe", "design-v2.png", PNG, w.yt.comments["2-1"][-1]["id"])
    w.now += DEBOUNCE
    w.tick()
    w.now += DEBOUNCE
    w.tick()
    text = w.calls("worker-send")[-1]["argv"][-1]
    assert "wrong colour" in text and "design-v2.png" in text
    assert any(p.name.endswith("design-v2.png") for p in folder(w).iterdir())


def test_every_file_is_listed_however_many_there_are(w):
    db_id = queue(w)
    for i in range(105):  # more than one page of the tracker's list
        w.yt.attach(db_id, "j.doe", f"log-{i}.txt", f"line {i}\n".encode())
    w.tick()
    text = dispatched(w)
    assert "`log-104.txt`" in text
    assert text.count(": not given to you, more than 20 files at once") == 85
    assert len(w.screen.calls[-1]["files"]) == 20


def test_a_crash_before_the_dispatch_starts_once_with_the_same_files(w, monkeypatch):
    db_id = queue(w)
    w.yt.attach(db_id, "j.doe", "mockup.png", PNG)
    real = w.runner.delivery.dispatch
    monkeypatch.setattr(w.runner.delivery, "dispatch", lambda *a, **k: (_ for _ in ()).throw(SystemExit("crash")))
    with pytest.raises(SystemExit):
        w.tick()
    assert w.calls("dispatch") == [] and (folder(w) / "74%2D1-mockup.png").exists()
    monkeypatch.setattr(w.runner.delivery, "dispatch", real)
    w.restart()
    w.tick()
    w.tick()
    assert "mockup.png" in dispatched(w)
    assert len([c for c in w.screen.calls if c.get("files")]) == 1  # the same text and files: no second screening


# -- the parts on their own ---------------------------------------------------------------------------


def _tracker(url: str, handler) -> YouTrackTracker:
    cfg = YouTrackConfig.model_validate({**example_raw()["tracker"], "url": url})
    return YouTrackTracker(cfg, ServiceClient("youtrack", url, {"Authorization": "Bearer t"}, HttpConfig(),
                                              transport=httpx.MockTransport(handler)))


def test_a_youtrack_url_keeps_the_installation_path_and_never_leaves_the_server():
    t = _tracker("https://acme.test/youtrack", lambda r: httpx.Response(200))
    assert t.file_path("/youtrack/api/files/7-2?sign=X&updated=1") == "/api/files/7-2?sign=X&updated=1"
    assert t.file_path("https://acme.test/youtrack/api/files/7-2?sign=X") == "/api/files/7-2?sign=X"
    assert _tracker("https://x.youtrack.cloud", lambda r: None).file_path("/api/files/1-1?sign=Y") \
        == "/api/files/1-1?sign=Y"
    for bad in ("https://evil.test/api/files/7-2", "/youtrack/api/issues/1", "/youtrack/api/files/../admin",
                "//evil.test/api/files/1"):
        with pytest.raises(ValueError):
            t.file_path(bad)


@pytest.mark.parametrize("headers", [{}, {"content-length": "10"}])
def test_a_download_stops_at_its_limit_whatever_the_server_says(headers):
    def stream(request):
        def body():
            for _ in range(100):
                yield b"x" * 1024
        return httpx.Response(200, headers=headers, content=body())
    c = ServiceClient("youtrack", "https://acme.test", {}, HttpConfig(), transport=httpx.MockTransport(stream))
    with pytest.raises(TooLarge) as e:
        c.download("/api/files/1?sign=S", what="GET attachment 1", limit=4096, seconds=60)
    assert "sign" not in str(e.value)
    assert c.download("/api/files/1", what="x", limit=200 * 1024, seconds=60) == b"x" * 100 * 1024


def test_the_classifier_sees_images_and_pdfs_as_such_and_text_as_text():
    files = [ScreenFile("a.png", "image", "image/png", PNG, "attached to the ticket by j.doe"),
             ScreenFile("b.pdf", "pdf", "application/pdf", PDF, "attached to a comment by a.smith"),
             ScreenFile("c.md", "text", "text/plain", b"# notes", "attached to the ticket by j.doe")]
    blocks = file_blocks("the message", files)
    assert [b["type"] for b in blocks] == ["text", "text", "image", "text", "document", "text"]
    assert blocks[2]["source"]["media_type"] == "image/png" and "# notes" in blocks[5]["text"]
    assert digest("t", files) != digest("t", files[:2]) != digest("t")


def test_the_classifier_reads_files_through_stream_json(tmp_path):
    seen = tmp_path / "seen.jsonl"
    fake = tmp_path / "claude"
    fake.write_text(f"""#!/bin/sh
printf '%s\\n' "$@" > {tmp_path}/args
cat > {seen}
echo '{{"type":"system"}}'
echo '{{"type":"result","is_error":false,"structured_output":{{"decision":"allow","categories":[],"reason":"ok","unseen_files":[]}},"modelUsage":{{"m":1}}}}'
""")
    fake.chmod(0o755)
    c = ClaudeClassifier({"PATH": "/usr/bin:/bin"}, binary=str(fake))
    verdict = c({"payload": "hi", "files": [ScreenFile("a.png", "image", "image/png", PNG, "x")]})
    assert verdict.decision == "allow"
    args = (tmp_path / "args").read_text().split("\n")
    assert "stream-json" in args and "--input-format" in args
    turn = json.loads(seen.read_text())
    assert turn["message"]["content"][2]["type"] == "image"


# -- the second round: links, file-only comments, ambiguous names, error bodies, what is kept in memory -------------


def _item(name: str, data: bytes = PNG, fid: str = "74-1", comment_id: str | None = None) -> materials.Material:
    from arceus_runner.integrations import Attachment
    a = Attachment(fid, name, "image/png", len(data), "j.doe", 1, comment_id)
    return materials.Material(a, "the ticket", materials.local_name(a), "image", "image/png", "ok", data=data)


@pytest.mark.parametrize("planted", [".arceus", ".arceus/ticket", ".arceus/ticket/ACME-1"])
def test_publishing_never_writes_through_a_link_the_agent_left_on_the_way(tmp_path, planted):
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    (ws / planted).parent.mkdir(parents=True, exist_ok=True)
    (ws / planted).symlink_to(outside)
    materials.publish([_item("mockup.png")], ws, "ACME-1", fresh=True)
    assert list(outside.iterdir()) == []
    target = ws / ".arceus" / "ticket" / "ACME-1"
    assert not any(p.is_symlink() for p in (ws / ".arceus", ws / ".arceus/ticket", target))
    assert (target / "74%2D1-mockup.png").read_bytes() == PNG


def test_publishing_replaces_a_planted_file_link_instead_of_following_it(tmp_path):
    ws = tmp_path / "ws"
    target = ws / ".arceus" / "ticket" / "ACME-1"
    target.mkdir(parents=True)
    victim, other = tmp_path / "victim", tmp_path / "other"
    victim.write_text("keep")
    other.write_text("keep too")
    (target / "74%2D1-mockup.png").symlink_to(victim)
    (target / materials.MANIFEST).symlink_to(other)
    (target / ".74%2D1-mockup.png.tmp").symlink_to(victim)  # a guess at a temporary name
    materials.publish([_item("mockup.png")], ws, "ACME-1")
    assert victim.read_text() == "keep" and other.read_text() == "keep too"
    assert not (target / "74%2D1-mockup.png").is_symlink() and (target / "74%2D1-mockup.png").read_bytes() == PNG
    assert not (target / materials.MANIFEST).is_symlink()
    assert json.loads((target / materials.MANIFEST).read_text())["items"][0]["name"] == "mockup.png"
    assert not [p for p in target.iterdir() if p.name.endswith(".tmp") and not p.is_symlink()]


def test_an_answer_that_is_only_a_screenshot_is_an_answer(w):
    asking(w)
    w.yt.comment("2-1", "a.smith", "")
    w.yt.attach("2-1", "a.smith", "this.png", PNG, w.yt.comments["2-1"][-1]["id"])
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    text = w.calls("worker-send")[-1]["argv"][-1]
    assert text.startswith("a.smith answered with files only.") and "this.png" in text


def test_a_comment_from_someone_not_allowed_with_only_a_file_is_not_an_answer(w):
    asking(w)
    w.yt.comment("2-1", "mallory", "")
    w.yt.attach("2-1", "mallory", "this.png", PNG, w.yt.comments["2-1"][-1]["id"])
    w.tick()
    assert w.store.task("ACME-1").state == "needs_input" and w.calls("worker-send") == []


def test_a_review_comment_that_is_only_a_file_reaches_the_agent(w):
    published(w)
    w.yt.comment("2-1", "j.doe", "")
    w.yt.attach("2-1", "j.doe", "bug.png", PNG, w.yt.comments["2-1"][-1]["id"])
    w.now += DEBOUNCE
    w.tick()
    w.now += DEBOUNCE
    w.tick()
    text = w.calls("worker-send")[-1]["argv"][-1]
    assert "j.doe on the ticket: (no text, only the files listed below)" in text and "bug.png" in text


def test_a_name_in_the_text_is_bound_to_one_file_or_said_to_be_ambiguous(w):
    db_id = queue(w, description="As in ![](mockup.png) and ![](mockup.png).")
    w.yt.comment(db_id, "a.smith", "Updated: ![](mockup.png)")
    mine = w.yt.comments[db_id][-1]["id"]
    w.yt.attach(db_id, "j.doe", "mockup.png", PNG)
    w.yt.attach(db_id, "j.doe", "mockup.png", PNG + b"2")
    w.yt.attach(db_id, "a.smith", "mockup.png", PNG + b"3", mine)
    w.tick()
    text = dispatched(w)
    where = folder(w)
    ambiguous = next(ln for ln in text.splitlines() if ln.startswith("- `mockup.png` in the description"))
    assert "is ambiguous: 2 files have that name" in ambiguous and "Do not pick one" in ambiguous
    assert str(where / "74%2D1-mockup.png") in ambiguous and str(where / "74%2D2-mockup.png") in ambiguous
    own = next(ln for ln in text.splitlines() if ln.startswith("- `mockup.png` in the comment by a.smith"))
    assert str(where / "74%2D3-mockup.png") in own and "ambiguous" not in own


@pytest.mark.parametrize("status", [404, 400])
def test_an_error_body_is_read_only_as_far_as_its_message_needs(status):
    sent = []

    def stream(request):
        def body():
            for _ in range(64):
                sent.append(1024)
                yield b"e" * 1024
        return httpx.Response(status, content=body())
    c = ServiceClient("youtrack", "https://acme.test", {}, HttpConfig(), transport=httpx.MockTransport(stream))
    with pytest.raises(Exception) as e:
        c.download("/api/files/1", what="GET attachment 1", limit=1024, seconds=60)
    assert sum(sent) <= 4096 and len(str(e.value)) < 700


def test_fetched_files_are_kept_only_until_delivered_and_a_refused_one_is_asked_for_again(w):
    take(w)
    task = w.store.task("ACME-1")
    d = w.runner.delivery
    fid = w.yt.attach("2-1", "j.doe", "spec.pdf", PDF)
    w.yt.fail[f"/api/files/{fid}"] = 403
    [first] = d.ticket_files(task, None, [])
    assert first.status == "unavailable" and d._fetched == {}
    del w.yt.fail[f"/api/files/{fid}"]
    [second] = d.ticket_files(task, None, [])
    assert second.status == "ok" and len(d._fetched) == 1
    d.publish_files(task, [second])
    assert d._fetched == {}


@pytest.mark.parametrize("status", [400, 200])
def test_a_slow_body_is_cut_off_by_the_clock_not_only_by_its_size(status):
    now = [0.0]

    def stream(request):
        def body():
            for _ in range(2048):
                now[0] += 1.0  # one byte a second
                yield b"e"
        return httpx.Response(status, content=body())
    c = ServiceClient("youtrack", "https://acme.test", {}, HttpConfig(), transport=httpx.MockTransport(stream),
                      clock=lambda: now[0])
    with pytest.raises(Exception):
        c.download("/api/files/1", what="GET attachment 1", limit=1024 * 1024, seconds=2)
    assert now[0] <= 4  # stopped within a piece of the deadline, long before the body ended


def test_a_download_waits_for_the_server_no_longer_than_it_may_take():
    seen = []

    def stream(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, content=b"ok")
    c = ServiceClient("youtrack", "https://acme.test", {}, HttpConfig(), transport=httpx.MockTransport(stream))
    c.download("/api/files/1", what="x", limit=10, seconds=3)
    assert seen[0]["read"] == 3 and seen[0]["connect"] <= 3


def _gzip(data: bytes, name: bytes = b"") -> bytes:
    """gzip with an optional file name in its header (FNAME), the way a server may send it."""
    import struct
    import zlib
    deflate = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = deflate.compress(data) + deflate.flush()
    head = b"\x1f\x8b\x08" + (b"\x08" if name else b"\x00") + b"\x00" * 4 + b"\x00\xff" + (name + b"\x00" if name else b"")
    return head + body + struct.pack("<II", zlib.crc32(data), len(data) & 0xFFFFFFFF)


def _client_for(content, headers: dict, clock=None) -> ServiceClient:
    sent = []

    def handler(request):
        sent.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, headers=headers, content=content())
    c = ServiceClient("youtrack", "https://acme.test", {}, HttpConfig(), transport=httpx.MockTransport(handler),
                      **({"clock": clock} if clock else {}))
    c.sent_encodings = sent
    return c


def test_a_compressed_body_whose_header_trickles_in_is_cut_off_by_the_clock():
    now = [0.0]
    body = _gzip(b"x" * 10, name=b"n" * 2048)

    def slow():
        for byte in body:
            now[0] += 1.0  # one raw byte a second, the decoder still waiting for the end of the header
            yield bytes([byte])
    c = _client_for(slow, {"content-encoding": "gzip"}, clock=lambda: now[0])
    with pytest.raises(TooLarge):
        c.download("/api/files/1", what="GET attachment 1", limit=1024 * 1024, seconds=2)
    assert now[0] <= 4
    assert c.sent_encodings == ["identity"]  # it asked for no compression in the first place


def test_a_compressed_body_is_decoded_and_never_inflated_past_the_limit():
    ok = _client_for(lambda: iter([_gzip(PNG)]), {"content-encoding": "gzip"})
    assert ok.download("/api/files/1", what="x", limit=1024, seconds=60) == PNG
    bomb = _client_for(lambda: iter([_gzip(b"\0" * (50 * 1024 * 1024))]), {"content-encoding": "gzip"})
    with pytest.raises(TooLarge):
        bomb.download("/api/files/1", what="x", limit=1024 * 1024, seconds=60)
    for broken in (_gzip(PNG)[:-12], b"\x1f\x8b not gzip"):
        c = _client_for(lambda b=broken: iter([b]), {"content-encoding": "gzip"})
        with pytest.raises(Exception, match="compressed body"):
            c.download("/api/files/1", what="x", limit=1024, seconds=60)
    with pytest.raises(Exception, match="does not decode"):
        _client_for(lambda: iter([b"?"]), {"content-encoding": "br"}).download("/api/files/1", what="x", limit=10,
                                                                               seconds=60)


# -- the third round: files larger than one classifier call carries -------------------------------------------------

def test_files_go_to_the_screen_in_calls_no_larger_than_a_call_carries():
    f = lambda n: ScreenFile(f"{n}.pdf", "pdf", "application/pdf", b"x" * n, "x")
    files = [f(6), f(6), f(5), f(20), f(1)]
    # In order, each call at most 12 bytes; a file larger than that goes alone.
    assert calls(files, 12) == [[0, 1], [2], [3], [4]]
    assert calls([], 12) == []
    assert calls(files[:3], 100) == [[0, 1, 2]]


def _stub_classifier(tmp_path, answers: list[dict]) -> tuple[ClaudeClassifier, Path]:
    """A `claude` that records each call's turn and answers the next of `answers`."""
    log = tmp_path / "turns"
    log.mkdir()
    (tmp_path / "answers.json").write_text(json.dumps(answers))
    stub = tmp_path / "claude"
    stub.write_text(f"""#!{sys.executable}
import json, os, sys
turns = sorted(os.listdir({str(log)!r}))
n = len(turns)
open(os.path.join({str(log)!r}, f"{{n:02}}.json"), "w").write(sys.stdin.read())
answer = json.load(open({str(tmp_path / "answers.json")!r}))[n]
print(json.dumps({{"type": "result", "is_error": False, "modelUsage": {{"m": 1}}, "structured_output": answer}}))
""")
    stub.chmod(0o755)
    return ClaudeClassifier({"PATH": "/usr/bin:/bin"}, binary=str(stub)), log


def _answer(decision="allow", categories=(), unseen=()):
    return {"decision": decision, "categories": list(categories), "reason": f"{decision} it", "unseen_files": list(unseen)}


def _pdf(name: str, size: int) -> ScreenFile:
    return ScreenFile(name, "pdf", "application/pdf", PDF + b"\0" * (size - len(PDF)), "attached to the ticket by j.doe")


def test_large_files_are_screened_over_several_calls_and_the_strictest_verdict_counts(tmp_path):
    c, log = _stub_classifier(tmp_path, [_answer(), _answer("block", ["prompt_injection"])])
    big = 10 * 1024 * 1024
    v = c({"payload": "build it", "files": [_pdf("a.pdf", big), _pdf("b.pdf", big)]})
    assert (v.decision, v.categories) == ("block", ("prompt_injection",))
    turns = [json.loads(p.read_text())["message"]["content"] for p in sorted(log.iterdir())]
    assert len(turns) == 2
    # Each call has the whole text and says which file it carries; the files keep their numbers.
    for i, content in enumerate(turns, 1):
        assert "build it" in content[0]["text"] and f"this call carries file(s) {i}." in content[1]["text"]
        assert [b["type"] for b in content].count("document") == 1
        assert f"File {i} of 2" in content[2]["text"]


def test_a_file_the_screen_could_not_see_goes_to_a_person(tmp_path):
    c, _ = _stub_classifier(tmp_path, [_answer(unseen=[2])])
    v = c({"payload": "build it", "files": [_pdf("a.pdf", 100), _pdf("spec.pdf", 100)]})
    assert v.decision == "review" and "unscreened" in v.categories and "spec.pdf" in v.reason
    # What the model did find stays: a block is never softened to a review.
    (tmp_path / "2").mkdir()
    c, _ = _stub_classifier(tmp_path / "2", [_answer("block", ["secrets"], unseen=[1])])
    v = c({"payload": "build it", "files": [_pdf("a.pdf", 100)]})
    assert (v.decision, v.categories) == ("block", ("secrets", "unscreened"))


def test_a_file_number_the_call_did_not_carry_is_a_malformed_answer(tmp_path):
    c, _ = _stub_classifier(tmp_path, [_answer(unseen=[2])])
    with pytest.raises(ValueError):
        c({"payload": "x", "files": [_pdf("a.pdf", 100)]})


def test_the_agent_is_told_to_read_large_pdfs_by_pages(tmp_path):
    from arceus_runner.integrations import Attachment

    def item(name, size):
        a = Attachment(name, name, "application/pdf", size, "j.doe", 1, None)
        return materials.Material(a, "the ticket", name, "pdf", "application/pdf", "ok", data=b"x" * size)
    small = materials.section([item("a.pdf", 1024)], tmp_path)
    large = materials.section([item("a.pdf", 5 * 1024 * 1024), item("b.pdf", 5 * 1024 * 1024)], tmp_path)
    assert "a few pages at a time" not in small
    assert "a few pages at a time (Read's `pages`" in large


def test_a_pdf_over_the_old_limit_is_now_given(w):
    db_id = queue(w)
    w.yt.attach(db_id, "j.doe", "spec.pdf", PDF + b"\0" * (14 * 1024 * 1024))
    w.tick()
    text = dispatched(w)
    assert "`spec.pdf`, attached to the ticket by j.doe: `" in text and "not given" not in text


def test_a_file_in_ones_own_comment_names_its_author_once():
    from arceus_runner.integrations import Attachment

    def item(where, author):
        a = Attachment("74-1", "x.png", "image/png", 1, author, 1, "4-1")
        return materials.Material(a, where, "x.png", "image", "image/png", "ok", data=PNG)
    assert item("a comment by j.doe of 2026-10-20 10:00 UTC", "j.doe").attached == \
        "attached to a comment by j.doe of 2026-10-20 10:00 UTC"
    assert item("a comment by j.doe of 2026-10-20 10:00 UTC", "a.smith").attached == \
        "attached to a comment by j.doe of 2026-10-20 10:00 UTC by a.smith"
    assert item("a comment by j.doe.admin", "j.doe").attached == "attached to a comment by j.doe.admin by j.doe"
    assert item("the ticket", "").attached == "attached to the ticket by someone unknown"


def test_a_text_file_longer_than_the_screen_reads_is_left_out_instead_of_holding_the_ticket(w):
    db_id = queue(w)
    w.yt.attach(db_id, "j.doe", "build.log", b"line\n" * 30_000)  # 150 000 characters, the longest
    w.yt.attach(db_id, "j.doe", "notes.md", b"# notes\n" + b"x" * 40_000)
    w.yt.attach(db_id, "j.doe", "more.md", b"y" * 30_000)
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"  # screened by the model, not held for a person
    text = dispatched(w)
    assert "`build.log`, attached to the ticket by j.doe: not given to you, 150000 characters, more than the " \
        "security screen reads together with the rest of this text (100000 in all); paste the part that matters " \
        "into a comment" in text
    # The two that fit together with the text are given.
    assert "`notes.md`, attached to the ticket by j.doe: `" in text and "`more.md`, attached to the ticket by j.doe: `" in text
    assert [f.name for f in w.screen.calls[-1]["files"]] == ["74%2D2-notes.md", "74%2D3-more.md"]
    assert len(text) + 40_008 + 30_000 <= MAX_CHARS


def test_a_long_ticket_leaves_less_room_for_its_text_files(w):
    db_id = queue(w, description="Requirements:\n" + "r" * 50_000)
    w.yt.attach(db_id, "j.doe", "a.md", b"a" * 30_000)
    w.yt.attach(db_id, "j.doe", "b.md", b"b" * 25_000)
    w.yt.attach(db_id, "j.doe", "c.md", b"c" * 10_000)
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"
    text = dispatched(w)
    # 50 000 of the ticket and 65 000 of files: the longest file goes, and the rest fits.
    assert "`a.md`, attached to the ticket by j.doe: not given to you, 30000 characters" in text
    assert "`b.md`, attached to the ticket by j.doe: `" in text and "`c.md`, attached to the ticket by j.doe: `" in text
    assert [f.name for f in w.screen.calls[-1]["files"]] == ["74%2D2-b.md", "74%2D3-c.md"]
    assert len(text) + 25_000 + 10_000 <= MAX_CHARS


def test_a_ticket_too_long_by_itself_still_goes_to_a_person(w):
    db_id = queue(w, description="r" * 120_000)
    w.yt.attach(db_id, "j.doe", "a.md", b"a" * 1_000)
    w.tick()
    assert w.store.task("ACME-1").state == "security_hold"



def test_references_with_parentheses_or_spaces_in_the_name_find_their_file(tmp_path):
    shot, spec = _item("Screenshot 2026-11-01 at 10.00.00 (2).png"), _item("my spec.pdf", fid="74-2")
    text = "As here ![](Screenshot%202026-11-01%20at%2010.00.00%20(2).png){width=70%} and in [the spec](<my spec.pdf>)."
    lines = materials.references([("the description", None, text)], [shot, spec], tmp_path)
    assert lines == [f"- `Screenshot 2026-11-01 at 10.00.00 (2).png` in the description: `{tmp_path / shot.file}`, "
                     "attached to the ticket by j.doe",
                     f"- `my spec.pdf` in the description: `{tmp_path / spec.file}`, attached to the ticket by j.doe"]


def test_files_fetched_again_are_given_only_as_the_screened_text_names_them(tmp_path):
    # The code reviewer's brief is screened when it is written and its files fetched again when it is dispatched:
    # a file attached or left out in between is neither published nor screened with that text.
    named, later = _item("mockup.png"), _item("new.png", fid="74-9")
    left_out = materials.Material(named.attachment, "the ticket", "", "image", "image/png", "skipped", "too large")
    text, given = materials.with_section("Review it.", [named], tmp_path)
    assert materials.given_in(text, [named, later], tmp_path) == [named]
    assert materials.given_in(text, [left_out], tmp_path) == []


def test_a_research_brief_below_the_files_counts_against_what_the_screen_reads(w):
    # Staging review, round 2: the brief was added after the files were fitted, and took a research ticket with a
    # text file past the screen's limit, so the whole ticket went to a person.
    from arceus_runner.delivery import research_brief
    brief = len(research_brief(w.cfg.paths.workspace / ".arceus" / "notes" / "ACME-1" / "answer-1.md"))
    db_id = queue(w, description="r" * (MAX_CHARS - 20_000 - 1_000 - brief // 2))
    w.yt.issue_tags[db_id].add("agent-research")
    w.yt.attach(db_id, "j.doe", "log.txt", b"l" * 20_000)
    w.tick()
    assert w.store.task("ACME-1").state == "dispatched"     # screened by the model, not held for a person
    text = dispatched(w)
    assert "`log.txt`, attached to the ticket by j.doe: not given to you, 20000 characters" in text
    assert "answer-1.md" in text and len(text) <= MAX_CHARS  # the brief is there, below the files, all screened
    # Without the brief the file would have fitted: the brief is what left it out.
    assert len(text) - brief + 20_000 <= MAX_CHARS
