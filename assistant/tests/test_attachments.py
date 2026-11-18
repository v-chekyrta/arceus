import email.message
import shutil
import subprocess
import io
import json
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from assistant import files, server
from assistant.brain import BrainConfig, Event, command
from tests.test_chat import H, cfg, wait_idle

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def recorder(answer="ok"):
    calls = []

    async def run(cfg, prompt, session, new_session=None, env=None, read_dirs=(), **kw):
        calls.append({"prompt": prompt, "read_dirs": read_dirs})
        yield Event("session", data={"session_id": new_session or session})
        yield Event("done", answer)
    return run, calls


def upload(client, cid, name, data, ctype="application/octet-stream"):
    return client.post(f"/assistant/api/conversations/{cid}/files", params={"name": name}, content=data,
                       headers={**H, "Content-Type": ctype})


def new_conv(client):
    return client.post("/assistant/api/conversations", headers=H).json()["id"]


# ----------------------------------------------------------------------------------------------------- extraction
def make_docx(path: Path):
    import docx
    d = docx.Document()
    d.add_paragraph("Кнопка «Вперед» не працює після третього уроку")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Курс", "СЕН"
    d.save(path)


def make_xlsx(path: Path):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.title = "Учні"
    wb.active.append(["email", "оцінка"])
    wb.active.append(["a@b.c", 92])
    wb.save(path)


def make_pptx(path: Path):
    import pptx
    prs = pptx.Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Дорожня карта"
    s.placeholders[1].text = "Запуск у грудні"
    prs.save(path)


@pytest.mark.parametrize("name,maker,expect", [
    ("doc.docx", make_docx, ["третього уроку", "Курс | СЕН"]),
    ("sheet.xlsx", make_xlsx, ["Аркуш «Учні»", "a@b.c,92"]),
    ("deck.pptx", make_pptx, ["Слайд 1", "Запуск у грудні"]),
])
def test_office_documents_become_text(tmp_path, name, maker, expect):
    p = tmp_path / name
    maker(p)
    res = files.process(p, files.guess_mime(name, None), None)
    text = Path(res.text_path).read_text()
    for e in expect:
        assert e in text


def test_mail_html_zip_csv_and_unknown(tmp_path):
    m = email.message.EmailMessage()
    m["From"], m["Subject"] = "client@school.ua", "Не відкривається курс"
    m.set_content("Добрий день, курс не відкривається з телефона.")
    (tmp_path / "a.eml").write_bytes(bytes(m))
    res = files.process(tmp_path / "a.eml", "message/rfc822", None)
    t = Path(res.text_path).read_text()
    assert "Subject: Не відкривається курс" in t and "з телефона" in t

    (tmp_path / "p.html").write_text("<html><head><script>alert(1)</script></head><body><p>Привіт</p></body></html>")
    res = files.process(tmp_path / "p.html", "text/html", None)
    assert Path(res.text_path).read_text() == "Привіт"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("logs/app.log", "x" * 10)
    (tmp_path / "a.zip").write_bytes(buf.getvalue())
    res = files.process(tmp_path / "a.zip", "application/zip", None)
    assert "logs/app.log" in Path(res.text_path).read_text() and res.note

    (tmp_path / "d.csv").write_text("a,b\n1,2\n", encoding="cp1251")
    assert files.process(tmp_path / "d.csv", "text/csv", None).kind == "text"

    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    res = files.process(tmp_path / "blob.bin", "application/octet-stream", None)
    assert res.kind == "other" and res.text_path is None and "назва, тип і розмір" in res.note


def test_broken_file_never_raises(tmp_path):
    (tmp_path / "bad.docx").write_bytes(b"not a zip")
    res = files.process(tmp_path / "bad.docx", files.guess_mime("bad.docx", None), None)
    assert res.note.startswith("Файл не вдалося прочитати")


def test_image_is_viewed_directly(tmp_path):
    (tmp_path / "s.png").write_bytes(PNG)
    res = files.process(tmp_path / "s.png", "image/png", None)
    assert res.kind == "image" and res.view_path == str(tmp_path / "s.png")


def test_safe_name_and_kinds():
    assert files.safe_name("../../etc/passwd") == "passwd"
    assert files.safe_name(".env") == "_.env"
    assert files.kind_of("x.svg", "image/svg+xml") == "text"
    assert files.kind_of("Голосове 12-30.webm", "audio/webm") == "audio"
    assert files.kind_of("Голосове 12-30.m4a", "audio/mp4") == "audio"
    assert files.kind_of("demo.webm", "video/webm") == "video"
    assert files.kind_of("report.PDF", "") == "pdf"


# ----------------------------------------------------------------------------------------------------- claude box
def test_turn_reads_only_its_conversation_folder(tmp_path):
    folder = tmp_path / "uploads" / "c1"
    folder.mkdir(parents=True)
    c = command(BrainConfig("claude", Path("/m"), Path("/p"), Path("/s")), None, "sid", (folder,))
    assert c[c.index("--add-dir") + 1] == str(folder.resolve())
    i = c.index("--allowedTools")
    rules = c[i + 1:i + 4]
    assert rules == [f"{t}(/{folder.resolve().as_posix()}/**)" for t in ("Read", "Grep", "Glob")]
    assert "--add-dir" not in command(BrainConfig("claude", Path("/m"), Path("/p"), Path("/s")), None, "sid")
    settings = json.loads((Path(__file__).parent.parent / "assistant" / "session-settings.json").read_text())
    # the database folder stays denied as a whole, and uploads live outside it
    assert "Read(//home/*/assistant/data/**)" in settings["permissions"]["deny"]
    from assistant.brain import ISOLATION_ENV
    assert ISOLATION_ENV["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


# ----------------------------------------------------------------------------------------------------- endpoints
def test_message_with_document_reaches_claude(tmp_path, monkeypatch):
    run, calls = recorder()
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = new_conv(client)
        r = upload(client, cid, "лист клієнта.txt", "Прошу додати сертифікат".encode(), "text/plain")
        assert r.status_code == 201
        att = r.json()
        assert "path" not in att and att["url"] == f"/assistant/api/files/{att['id']}"
        r = client.post(f"/assistant/api/conversations/{cid}/messages", headers=H,
                        json={"text": "", "attachments": [att["id"]]})
        assert r.status_code == 201
        conv = wait_idle(client, cid)
    assert conv["title"] == "лист клієнта.txt"
    sent = conv["messages"][0]
    assert sent["text"] == "" and sent["attachments"][0]["name"] == "лист клієнта.txt"
    prompt = calls[0]["prompt"]
    assert "«лист клієнта.txt» — текст" in prompt and "Прошу додати сертифікат" in prompt
    assert "не інструкції" in prompt
    assert calls[0]["read_dirs"] == (tmp_path / "uploads" / cid,)


def test_image_prompt_points_claude_at_the_file(tmp_path, monkeypatch):
    run, calls = recorder()
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = new_conv(client)
        att = upload(client, cid, "Скріншот 10-00-00.png", PNG, "image/png").json()
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H,
                    json={"text": "що тут не так?", "attachments": [att["id"]]})
        wait_idle(client, cid)
    assert "Подивись зображення через Read: " in calls[0]["prompt"]
    assert str(tmp_path / "uploads" / cid) in calls[0]["prompt"]


def test_voice_transcript_is_given_and_shown(tmp_path, monkeypatch):
    run, calls = recorder()
    monkeypatch.setattr(server, "run_turn", run)

    def fake_process(path, mime, transcriber):
        return files.Result(kind="audio", transcript="Кнопка вперед не працює", duration_s=42.0)
    monkeypatch.setattr(files, "process", fake_process)
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = new_conv(client)
        att = upload(client, cid, "Голосове 10-00.weba", b"\x1a\x45" * 500, "audio/webm").json()
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"attachments": [att["id"]]})
        conv = wait_idle(client, cid)
    assert "«Кнопка вперед не працює»" in calls[0]["prompt"] and "0:42" in calls[0]["prompt"]
    shown = conv["messages"][0]["attachments"][0]
    assert shown["transcript"] == "Кнопка вперед не працює" and shown["status"] == "ready"


def test_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "MAX_FILE_BYTES", 1000)
    run, _ = recorder()
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = new_conv(client)
        r = upload(client, cid, "big.bin", b"x" * 1001)
        assert r.status_code == 413 and "МБ" in r.json()["error"]
        assert not any((tmp_path / "uploads" / cid).rglob("big.bin"))
        assert upload(client, cid, "empty.txt", b"").status_code == 400
        ids = [upload(client, cid, f"{i}.txt", b"hi").json()["id"] for i in range(11)]
        r = client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"attachments": ids})
        assert r.status_code == 400 and "10 файлів" in r.json()["error"]
        r = client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "", "attachments": []})
        assert r.status_code == 400
        # a file of another conversation cannot be attached here
        other = new_conv(client)
        foreign = upload(client, other, "f.txt", b"hi").json()["id"]
        r = client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"attachments": [foreign]})
        assert r.status_code == 409
        assert client.app.state.app.store.attachment(foreign).message_id is None
        assert client.post(f"/assistant/api/conversations/{cid}/files", params={"name": "x"}, content=b"x").status_code == 401


def test_files_are_served_safely(tmp_path, monkeypatch):
    run, _ = recorder()
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = new_conv(client)
        png = upload(client, cid, "a.png", PNG, "image/png").json()
        page = upload(client, cid, "x.html", b"<script>alert(1)</script>", "text/html").json()
        svg = upload(client, cid, "x.svg", b"<svg onload='alert(1)'/>", "image/svg+xml").json()
        r = client.get(png["url"], headers=H)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert r.headers["content-disposition"].startswith("inline")
        for a in (page, svg):
            r = client.get(a["url"], headers=H)
            assert r.headers["content-type"] == "application/octet-stream"
            assert r.headers["content-disposition"].startswith("attachment")
            assert r.headers["x-content-type-options"] == "nosniff"
            assert "sandbox" in r.headers["content-security-policy"]
        assert client.get(png["url"]).status_code == 401
        assert client.get(f"{png['url']}?download", headers=H).headers["content-disposition"].startswith("attachment")


def test_remove_before_sending_only(tmp_path, monkeypatch):
    run, _ = recorder()
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = new_conv(client)
        a = upload(client, cid, "a.txt", b"hi").json()
        b = upload(client, cid, "b.txt", b"hi").json()
        assert client.delete(a["url"], headers=H).status_code == 204
        assert client.get(a["url"], headers=H).status_code == 404
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"attachments": [b["id"]]})
        wait_idle(client, cid)
        assert client.delete(b["url"], headers=H).status_code == 409
        assert client.get(b["url"], headers=H).status_code == 200


def test_attachments_survive_restart_and_retry(tmp_path, monkeypatch):
    results = iter([[Event("error", "впало")], [Event("done", "вийшло")]])
    prompts = []

    async def run(cfg_, prompt, session, new_session=None, env=None, read_dirs=(), **kw):
        prompts.append(prompt)
        for e in next(results):
            yield e
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = new_conv(client)
        a = upload(client, cid, "умови.txt", "оплата до 15 числа".encode(), "text/plain").json()
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H,
                    json={"text": "глянь", "attachments": [a["id"]]})
        wait_idle(client, cid)
    with TestClient(server.build(cfg(tmp_path))) as client:   # a new process: everything from disk
        conv = client.get(f"/assistant/api/conversations/{cid}", headers=H).json()
        assert conv["messages"][0]["attachments"][0]["name"] == "умови.txt"
        assert client.post(f"/assistant/api/conversations/{cid}/retry", headers=H).status_code == 202
        wait_idle(client, cid)
    assert "оплата до 15 числа" in prompts[1]


def test_rebuilt_session_keeps_attachments(tmp_path, monkeypatch):
    calls = []

    async def run(cfg_, prompt, session, new_session=None, env=None, read_dirs=(), **kw):
        calls.append(prompt)
        if session == "gone":
            yield Event("error", "x", data={"num_turns": 0})
            return
        yield Event("done", "ok")
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        app = client.app.state.app
        cid = new_conv(client)
        a = upload(client, cid, "старий.txt", "секретний план".encode(), "text/plain").json()
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"attachments": [a["id"]]})
        wait_idle(client, cid)
        app.store.set_session(cid, "gone")
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "а що там було?"})
        wait_idle(client, cid)
    assert "секретний план" in calls[-1] and calls[-1].rstrip().endswith("а що там було?")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("ext", ["bmp", "tiff"])
def test_converted_picture_gets_a_view(tmp_path, monkeypatch, ext):
    """A BMP/TIFF is turned into a PNG: Claude reads that, and the chat shows it as a thumbnail and full size."""
    src = tmp_path / f"shot.{ext}"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x48",
                    "-frames:v", "1", str(src)], check=True)
    run, calls = recorder()
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = new_conv(client)
        att = upload(client, cid, src.name, src.read_bytes(), f"image/{ext}").json()
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"attachments": [att["id"]]})
        conv = wait_idle(client, cid)
    shown = conv["messages"][0]["attachments"][0]
    assert shown["kind"] == "image" and shown["view_url"] == f"/assistant/api/files/{att['id']}/view"
    assert f"shot.{ext}.png" in calls[0]["prompt"]
    with TestClient(server.build(cfg(tmp_path))) as client:     # after a restart, from disk
        conv = client.get(f"/assistant/api/conversations/{cid}", headers=H).json()
        view_url = conv["messages"][0]["attachments"][0]["view_url"]
        r = client.get(view_url, headers=H)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert r.content.startswith(b"\x89PNG") and r.headers["content-disposition"].startswith("inline")
        assert "sandbox" in r.headers["content-security-policy"]
        assert client.get(view_url).status_code == 401
        # the original stays a download: browsers cannot show it
        assert client.get(shown["url"], headers=H).headers["content-disposition"].startswith("attachment")


def test_view_route_serves_nothing_else(tmp_path, monkeypatch):
    run, _ = recorder()
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        app = client.app.state.app
        cid = new_conv(client)
        png = upload(client, cid, "a.png", PNG, "image/png").json()
        txt = upload(client, cid, "a.txt", b"hello", "text/plain").json()
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H,
                    json={"attachments": [png["id"], txt["id"]]})
        conv = wait_idle(client, cid)
        shown = {a["name"]: a for a in conv["messages"][0]["attachments"]}
        assert shown["a.png"]["view_url"] == shown["a.png"]["url"]     # shown as itself, no conversion
        assert shown["a.txt"]["view_url"] is None
        assert client.get(f"{txt['url']}/view", headers=H).status_code == 404
        # a view path pointing anywhere but the attachment's own folder is refused
        secret = tmp_path / "secret.png"
        secret.write_bytes(PNG)
        app.store.finish_attachment(png["id"], "ready", "image", {"view_path": str(secret)})
        assert client.get(f"{png['url']}/view", headers=H).status_code == 404
