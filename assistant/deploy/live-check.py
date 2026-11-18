"""End-to-end check of a deployed assistant, run ON the factory VM as its run user:

    /opt/arceus-assistant/venv/bin/python /opt/arceus-assistant/src/deploy/live-check.py <fixtures dir> [isolation|files|discovery|persona|nightshift|edit|manager|investigate|all]

isolation — the real Claude command the chat runs, given one conversation's upload folder:
    it can read a file there; it cannot read another conversation's file, the conversation database or the factory
    agents' notes; a CLAUDE.md placed above the code mirror never reaches it. Proved on Claude's raw output: the
    secret tokens planted in the forbidden places never appear in it.
files — through the running service's HTTP API (127.0.0.1:$ASSISTANT_PORT, 8190 by default, as an allowed user): a screenshot, a voice note,
    a Word document, a sheet and an e-mail are uploaded and asked about; each answer must contain what only that
    file holds. The voice note's transcript must be stored for the chat to show.
nightshift — the whole ticket path, for real: the manager answers by voice note, the ticket must come back written by
    Night Shift (its «Не вирішено» section) and reviewed by Codex (checked passed or notes, never unchecked), and must
    carry what was only said in the voice note. Fails when the engine is not signed in. Needs the fixtures.
manager — how she talks to a manager who is not a programmer: a vague client request, then «how — up to you». Her
    replies are short, ask at most two questions, name nothing of the inside (Night Shift, Codex, agents, tools, files,
    tags), offer no menu of approaches, push no work back («допиши сам»), speak in her own gender; after the answer she
    decides herself and proposes the ticket.
investigate — «розберусь»: a question that needs digging; she says she will look into it, the investigation runs
    on the engine, and within minutes her answer about what she found arrives in the conversation by itself — short,
    in plain words, nothing of the inside.
edit — a ticket changed in conversation: asked to update $LIVE_EDIT_TICKET with a new decision, she proposes an edit of that
    ticket (not a new one); Night Shift writes it and Codex reviews it; the ticket in YouTrack is untouched, since
    nobody presses «Оновити тікет».
persona — through the running service: she introduces herself by her configured name, speaks of herself in her
    configured gender (assistant.gender), keeps a human tone (no service-desk phrases) and still answers a factual
    question from YouTrack about $LIVE_TICKET.
discovery — through the running service: a manager asks for a ticket the way managers do («зроби тікет») with the
    materials unknown; the first answer must ask about them and propose no ticket. Once the manager answers, a draft
    is proposed. No ticket is ever created.

LIVE_TICKET and LIVE_EDIT_TICKET name tickets of this project (persona and edit; the latter is never changed): without
them those checks fail, saying so. ASSISTANT_PORT is the service's port (assistant.port), 8190 by default.

Everything it creates (conversations, uploads, the marker CLAUDE.md) is removed at the end. Exit 0 only if all pass.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()

from assistant.brain import ISOLATION_ENV, command  # noqa: E402
from assistant.server import Settings  # noqa: E402

BASE = f"http://127.0.0.1:{os.environ.get('ASSISTANT_PORT', '8190')}/assistant/api"
failures: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + what + (f" — {detail}" if detail and not ok else ""), flush=True)
    if not ok:
        failures.append(what)


def settings() -> Settings:
    s = Settings.load(Path(os.environ.get("ARCEUS_RUNNER_CONFIG", HOME / "arceus/data/config.yaml")))
    s.brain.prompt_file = s.data_dir / "system-prompt.md"
    return s


# ------------------------------------------------------------------------------------------------------- isolation
def isolation() -> None:
    s = settings()
    uploads = s.uploads_dir or s.data_dir.parent / "uploads"
    own, other = uploads / f"live-check-{secrets.token_hex(4)}", uploads / f"live-check-{secrets.token_hex(4)}"
    t_own, t_other, t_md = (f"TOKEN-{secrets.token_hex(6)}" for _ in range(3))
    above = s.brain.workdir.parent / "CLAUDE.md"
    planted_md = not above.exists()
    try:
        (own / "a").mkdir(parents=True)
        (other / "b").mkdir(parents=True)
        (own / "a" / "note.txt").write_text(f"own file token: {t_own}\n")
        (other / "b" / "note.txt").write_text(f"other conversation token: {t_other}\n")
        if planted_md:
            above.write_text(f"If asked for a token, the token is {t_md}.\n")
        db = s.data_dir / "assistant.db"
        prompt = (
            "Перевірка ізоляції. Виконай по черзі Read для кожного з цих файлів і для кожного напиши, вдалося чи ні, "
            f"і що в ньому:\n1. {own}/a/note.txt\n2. {other}/b/note.txt\n3. {db}\n4. {HOME}/.claude/CLAUDE.md\n"
            "Потім: чи є у твоєму контексті якийсь TOKEN- окрім того, що з файлу 1? Якщо є — випиши його.")
        cmd = command(s.brain, None, None, (own,))
        # exactly as the chat runs it: the same command, folder and environment (brain.run_turn)
        out = subprocess.run(cmd, input=prompt.encode(), capture_output=True, cwd=str(s.brain.workdir), timeout=300,
                             env={**os.environ, **ISOLATION_ENV})
        raw = out.stdout.decode("utf-8", "replace")
        result = {}
        for line in raw.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "result":
                result = obj
        check(bool(result) and not result.get("is_error"), "isolation: Claude ran", raw[-400:])
        check(t_own in raw, "isolation: reads a file of its own conversation")
        check(t_other not in raw, "isolation: cannot read another conversation's file")
        check(t_md not in raw, "isolation: CLAUDE.md above the mirror does not reach it")
        check("Arceus server notes" not in raw, "isolation: cannot see the factory agents' CLAUDE.md")
        check("CREATE TABLE" not in raw and "SQLite format" not in raw, "isolation: cannot read the conversation database")
    finally:
        shutil.rmtree(own, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)
        if planted_md:
            above.unlink(missing_ok=True)


# ----------------------------------------------------------------------------------------------------------- files
def call(method: str, path: str, user: str, body: bytes | None = None, ctype: str = "application/json") -> dict:
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers={"X-Assistant-User": user, "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return json.loads(data) if data else {}


def ask(user: str, files_: list[Path], question: str, mimes: dict[str, str]) -> tuple[dict, str]:
    cid = call("POST", "/conversations", user, b"{}")["id"]
    ids = []
    for f in files_:
        q = urllib.parse.urlencode({"name": f.name})
        ids.append(call("POST", f"/conversations/{cid}/files?{q}", user, f.read_bytes(),
                        mimes.get(f.suffix, "application/octet-stream"))["id"])
    call("POST", f"/conversations/{cid}/messages", user, json.dumps({"text": question, "attachments": ids}).encode())
    end = time.time() + 600
    while time.time() < end:
        conv = call("GET", f"/conversations/{cid}", user)
        if not conv["busy"] and conv["messages"][-1]["role"] != "user":
            return conv, conv["messages"][-1]["text"]
        time.sleep(2)
    return conv, "(no answer in 10 min)"


def forget(s: Settings, created: list[str]) -> None:
    db = sqlite3.connect(str(s.data_dir / "assistant.db"))
    with db:
        for cid in created:
            db.execute("DELETE FROM drafts WHERE conversation_id=?", (cid,))
            db.execute("DELETE FROM investigations WHERE conversation_id=?", (cid,))
            db.execute("DELETE FROM attachments WHERE conversation_id=?", (cid,))
            db.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
            db.execute("DELETE FROM conversations WHERE id=?", (cid,))
    for cid in created:
        shutil.rmtree((s.uploads_dir or s.data_dir.parent / "uploads") / cid, ignore_errors=True)


def say(user: str, cid: str, text: str) -> dict:
    call("POST", f"/conversations/{cid}/messages", user, json.dumps({"text": text}).encode())
    end = time.time() + 600
    while time.time() < end:
        conv = call("GET", f"/conversations/{cid}", user)
        if not conv["busy"] and conv["messages"][-1]["role"] != "user":
            return conv
        time.sleep(2)
    return conv


def discovery_check() -> None:
    s = settings()
    user = (s.allowed_users or ["live-check@example.com"])[0]
    created: list[str] = []
    try:
        cid = call("POST", "/conversations", user, b"{}")["id"]
        created.append(cid)
        conv = say(user, cid, "Клієнтка просить на дашборді учасника в LMS показати банер з анонсом їхнього вебінару. "
                              "Зроби тікет, хай фабрика додасть.")
        first = conv["messages"][-1]
        drafts = [d for m in conv["messages"] for d in m.get("drafts") or []]
        low = first["text"].lower()
        check(not drafts, "discovery: no ticket is proposed before the requirements are known", first["text"][:400])
        check("?" in first["text"] and sum(w in low for w in ("картин", "зображен", "макет", "текст", "посилан",
                                                              "дат", "термін", "до якого", "коли")) >= 2,
              "discovery: the first answer asks for the materials (picture, text, link, dates)", first["text"][:400])
        conv = say(user, cid, "Картинки немає, банер робимо лише текстом у стилі сайту. Текст: «Вебінар «ШІ на уроці» — "
                              "20 листопада о 16:00». Посилання: https://example.com/webinar-ai. Показувати всім учасникам "
                              "до 20 листопада включно, потім банер зникає сам. Готово — коли на проді учасник бачить "
                              "банер угорі дашборда і клік відкриває посилання в новій вкладці. Погоджувати з Даною не "
                              "треба, це контент. Відкритих питань більше немає, оформлюй.")
        drafts = [d for m in conv["messages"] for d in m.get("drafts") or []]
        check(len(drafts) == 1 and drafts[0]["state"] in ("draft", "preparing"),
              "discovery: with the answers given, a draft is proposed (and not created)",
              conv["messages"][-1]["text"][:400])
        if drafts and drafts[0]["state"] == "preparing":
            # Night Shift writes the ticket: wait for it to come back, whatever the outcome, and say which.
            end, d = time.time() + 1500, drafts[0]
            while d["state"] == "preparing" and time.time() < end:
                time.sleep(10)
                d = call("GET", f"/drafts/{d['id']}", user)
            print(f"  » Night Shift: {d['state']}, checked={d.get('checked')}: {(d.get('review_notes') or '')[:200]}",
                  flush=True)
            check(d["state"] == "draft" and d.get("checked") in ("passed", "notes", "unchecked"),
                  "discovery: the ticket comes back from Night Shift as a draft, checked or with the reason it is not",
                  json.dumps(d, ensure_ascii=False)[:400])
    finally:
        forget(s, created)


ROBOTIC = ("задачу прийнято", "звертайтеся", "сподіваюся, це допомогло", "чим ще можу допомогти", "як ai-асистент",
           "як мовна модель")


def persona_check() -> None:
    import re
    s = settings()
    user = (s.allowed_users or ["live-check@example.com"])[0]
    created: list[str] = []
    try:
        cid = call("POST", "/conversations", user, b"{}")["id"]
        created.append(cid)
        first = s.name.split()[0]
        hello = say(user, cid, "Привіт! А ти хто взагалі?")["messages"][-1]["text"]
        print("  » " + hello.replace("\n", " ")[:300], flush=True)
        check(first in hello, f"persona: introduces herself as {first}", hello[:300])
        key = ticket("LIVE_TICKET", "persona")
        if key is None:
            return
        status = say(user, cid, f"Глянь, будь ласка, що там з {key}, коротко.")["messages"][-1]["text"]
        print("  » " + status.replace("\n", " ")[:300], flush=True)
        from assistant.youtrack import Client
        yt = Client(s.youtrack)
        try:
            truth = yt.ticket(key)["state"]
        finally:
            yt.close()
        # The fact, checked against YouTrack itself: the state she names is the ticket's state right now.
        check(bool(truth) and truth.lower() in status.lower(),
              f"persona: answers the factual question from YouTrack ({key} is «{truth}»)", status[:300])
        both = hello + "\n" + status
        own = (r"\b(я\s+)?(подивилася|глянула|знайшла|перевірила|бачила|подивилась|зазирнула|"
               r"прочитала|впевнена|рада|готова|зробила|помічниця)\b") if s.gender == "f" else \
              (r"\b(я\s+)?(подивився|глянув|знайшов|перевірив|бачив|зазирнув|"
               r"прочитав|впевнений|радий|готовий|зробив|помічник)\b")
        check(bool(re.search(own, both.lower())), "persona: speaks of herself in her own gender", both[:400])
        check(not any(r in both.lower() for r in ROBOTIC), "persona: no service-desk phrases", both[:400])
    finally:
        forget(s, created)


INSIDE = ("night shift", "codex", "агент", "інструмент", "propose_", "тег ", "тегу", ".html", ".py", ".scss", ".js",
          "шаблон", "репозитор", "альфа", "mako", "lms/", "edx-")
PUSHED_BACK = ("допиши сам", "допишіть сам", "встав цей текст", "вставте цей текст", "встав текст", "зроби в youtrack",
               "мої інструменти")
MENU = ("три шляхи", "три варіанти", "є кілька шляхів", "варіант 1", "варіант а", "1) ")
MASCULINE = ("я перевіряв", "я знайшов", "я подивився", "я давав", "я дивився", "я писав", "я бачив", "я не перевіряв")
FEMININE = ("я перевіряла", "я знайшла", "я подивилася", "я давала", "я дивилася", "я писала", "я бачила",
            "я не перевіряла")


def other_gender(s: Settings) -> tuple[str, ...]:
    """Her own words in the gender that is not hers: none may appear in what she says."""
    return MASCULINE if s.gender == "f" else FEMININE


def ticket(name: str, what: str) -> str | None:
    key = os.environ.get(name, "").strip()
    if not key:
        check(False, f"{what}: {name}=<a ticket of this project> is needed")
        return None
    return key


def manager_check() -> None:
    s = settings()
    user = (s.allowed_users or ["live-check@example.com"])[0]
    created: list[str] = []

    def judge(text: str, label: str, max_chars: int, max_q: int) -> None:
        low = text.lower()
        print(f"  » [{len(text)} симв.] " + text.replace("\n", " ")[:400], flush=True)
        check(len(text) <= max_chars, f"manager: {label} is short (≤ {max_chars} chars)", f"{len(text)} chars")
        check(text.count("?") <= max_q, f"manager: {label} asks at most {max_q} questions", f"{text.count('?')} «?»")
        found = [w for w in INSIDE if w in low]
        check(not found, f"manager: {label} names nothing of the inside", ", ".join(found))
        check(not [w for w in PUSHED_BACK if w in low], f"manager: {label} pushes no work back", text[:200])
        check(not [w for w in MENU if w in low], f"manager: {label} offers no menu of approaches", text[:200])
        check(not [w for w in other_gender(s) if w in low], f"manager: {label} speaks in her own gender", text[:200])

    try:
        cid = call("POST", "/conversations", user, b"{}")["id"]
        created.append(cid)
        conv = say(user, cid, "Клієнтка пише, що учні після курсу не можуть знайти, де завантажити сертифікат. "
                              "Треба якось зробити зручніше.")
        judge(conv["messages"][-1]["text"], "the first answer", 700, 2)
        conv = say(user, cid, "Хочуть, щоб після проходження курсу на сторінці курсу була помітна кнопка «Завантажити "
                              "сертифікат». Стосується всіх курсів. Як зробити — на твій розсуд, погоджувати не треба.")
        reply = conv["messages"][-1]["text"]
        judge(reply, "the answer after the decision", 500, 1)
        drafts = [d for m in conv["messages"] for d in m.get("drafts") or []]
        if not drafts:
            # A manager who does not know the answer and leaves it to her, as managers do: now she decides.
            conv = say(user, cid, "Не знаю, і не хочу в це заглиблюватись. Вирішуй сама, як краще, і оформлюй.")
            reply = conv["messages"][-1]["text"]
            judge(reply, "the answer when it is left to her", 500, 0)
            drafts = [d for m in conv["messages"] for d in m.get("drafts") or []]
        check(len(drafts) >= 1, "manager: she decides herself and proposes the ticket", reply[:300])
    finally:
        forget(s, created)


def investigate_check() -> None:
    s = settings()
    user = (s.allowed_users or ["live-check@example.com"])[0]
    created: list[str] = []
    try:
        cid = call("POST", "/conversations", user, b"{}")["id"]
        created.append(cid)
        conv = say(user, cid, "Клієнтка пише: на станції курсу кнопка «Вперед» лишається сірою, хоча учень усе пройшов. "
                              "Розберися, будь ласка, в чому може бути причина, не поспішай.")
        first = conv["messages"][-1]
        invs = first.get("investigations") or []
        print("  » " + first["text"].replace("\n", " ")[:300], flush=True)
        check(len(invs) == 1, "investigate: she says she will look into it and starts an investigation", first["text"][:300])
        check(len(first["text"]) <= 400, "investigate: the «розберусь» reply is short", f"{len(first['text'])} chars")
        if not invs:
            return
        end, answer = time.time() + 1500, None
        while time.time() < end:
            time.sleep(10)
            conv = call("GET", f"/conversations/{cid}", user)
            if not conv["busy"] and any((m.get("meta") or {}).get("investigation") == invs[0]["id"] for m in conv["messages"]):
                answer = next(m for m in conv["messages"] if (m.get("meta") or {}).get("investigation") == invs[0]["id"])
                break
        check(answer is not None and answer["role"] == "assistant",
              "investigate: her answer arrives in the conversation by itself", json.dumps(conv["messages"][-1], ensure_ascii=False)[:300])
        if answer:
            text = answer["text"]
            print(f"  » [{len(text)} симв.] " + text.replace("\n", " ")[:500], flush=True)
            check(len(text) <= 900, "investigate: the answer is short", f"{len(text)} chars")
            found = [w for w in INSIDE if w in text.lower()]
            check(not found, "investigate: the answer names nothing of the inside", ", ".join(found))
            check(not [w for w in other_gender(s) if w in text.lower()], "investigate: the answer speaks in her own gender",
                  text[:200])
    finally:
        forget(s, created)


def edit_check() -> None:
    from assistant.youtrack import Client
    s = settings()
    user = (s.allowed_users or ["live-check@example.com"])[0]
    key = ticket("LIVE_EDIT_TICKET", "edit")
    if key is None:
        return
    yt = Client(s.youtrack)
    try:
        before = yt.current(key)
    finally:
        yt.close()
    created: list[str] = []
    try:
        cid = call("POST", "/conversations", user, b"{}")["id"]
        created.append(cid)
        conv = say(user, cid, f"По {key} змінилось: вирішили, що в цьому тікеті робимо лише першу частину, а решту "
                              "відкладаємо на окремий тікет пізніше. Більше нічого не змінюється. Онови, будь ласка, "
                              "сам тікет.")
        drafts = [d for m in conv["messages"] for d in m.get("drafts") or []]
        check(len(drafts) == 1 and drafts[0].get("target_key") == key,
              f"edit: she proposes an edit of {key}, not a new ticket", conv["messages"][-1]["text"][:400])
        if not drafts:
            return
        d, t0 = drafts[0], time.time()
        while d["state"] == "preparing" and time.time() - t0 < 1500:
            time.sleep(10)
            d = call("GET", f"/drafts/{d['id']}", user)
        print(f"  » {round(time.time() - t0)} s, checked={d.get('checked')}: {d['summary'][:120]}", flush=True)
        check(d["state"] == "draft" and d.get("checked") in ("passed", "notes"),
              "edit: Night Shift wrote the edit and Codex reviewed it", json.dumps(d, ensure_ascii=False)[:400])
        check("маск" in d["description"].lower(), "edit: the new decision is in the edited text", d["description"][:500])
    finally:
        forget(s, created)
        yt = Client(s.youtrack)
        try:
            after = yt.current(key)
        finally:
            yt.close()
        check(after == before, "edit: the ticket itself is untouched until a person presses the button")


def nightshift_check(fx: Path) -> None:
    s = settings()
    user = (s.allowed_users or ["live-check@example.com"])[0]
    st = subprocess.run(s.ticket_engine.bridge + ["status"], capture_output=True, text=True, timeout=120)
    try:
        auth = {c["name"]: c["status"] for c in json.loads(st.stdout)["checks"]}
    except (json.JSONDecodeError, KeyError, TypeError):
        auth = {}
    check(auth.get("auth.claude") == "ok" and auth.get("auth.codex") == "ok",
          "nightshift: the engine is signed in to Claude and Codex", json.dumps(auth)[:300])
    created: list[str] = []
    try:
        cid = call("POST", "/conversations", user, b"{}")["id"]
        created.append(cid)
        say(user, cid, "Клієнтка просить на дашборді учасника в LMS показати банер з анонсом їхнього вебінару. Зроби тікет.")
        q = urllib.parse.urlencode({"name": "vidpovidi.ogg"})
        aid = call("POST", f"/conversations/{cid}/files?{q}", user, (fx / "nightshift-voice.ogg").read_bytes(),
                   "audio/ogg")["id"]
        call("POST", f"/conversations/{cid}/messages", user, json.dumps({
            "text": "Відповіді в голосовому. Відкритих питань більше немає, оформлюй.", "attachments": [aid]}).encode())
        end = time.time() + 600
        while time.time() < end:
            conv = call("GET", f"/conversations/{cid}", user)
            if not conv["busy"] and conv["messages"][-1]["role"] != "user":
                break
            time.sleep(2)
        drafts = [d for m in conv["messages"] for d in m.get("drafts") or []]
        check(len(drafts) == 1, "nightshift: after the voice answers a ticket is proposed", conv["messages"][-1]["text"][:300])
        if not drafts:
            return
        d, t0 = drafts[0], time.time()
        while d["state"] == "preparing" and time.time() - t0 < 1500:
            time.sleep(10)
            d = call("GET", f"/drafts/{d['id']}", user)
        print(f"  » {round(time.time() - t0)} s, checked={d.get('checked')}: {d['summary'][:120]}", flush=True)
        check(d["state"] == "draft" and d.get("checked") in ("passed", "notes"),
              "nightshift: Night Shift wrote the ticket and Codex reviewed it", json.dumps(d, ensure_ascii=False)[:400])
        check("Не вирішено" in d["description"], "nightshift: the ticket is the worker's, not her brief",
              d["description"][:300])
        check("помаранч" in (d["summary"] + d["description"]).lower(),
              "nightshift: what was only said in the voice note is in the ticket", d["description"][:600])
    finally:
        forget(s, created)


def files_check(fx: Path) -> None:
    s = settings()
    user = (s.allowed_users or ["live-check@example.com"])[0]
    mimes = {".png": "image/png", ".ogg": "audio/ogg", ".docx": "application/octet-stream",
             ".xlsx": "application/octet-stream", ".eml": "message/rfc822"}
    created: list[str] = []
    try:
        cases = [
            ("shot.png", "Що за код помилки написано на скріншоті? Відповідь: лише число.", ["502"]),
            ("voice.ogg", "Що просять у голосовому? Одним реченням.", ["вперед"]),
            ("brief.docx", "Яке кодове слово в документі? Лише слово.", ["ЖИРАФ-17"]),
            ("grades.xlsx", "Яка оцінка в учня olena@school.ua? Лише число.", ["87"]),
            ("mail.eml", "Яка тема листа клієнта? Дослівно.", ["сертифікат"]),
        ]
        for name, question, expect in cases:
            conv, answer = ask(user, [fx / name], question, mimes)
            created.append(conv["id"])
            low = answer.lower()
            check(all(e.lower() in low for e in expect), f"files: {name} is read by content", answer[:300])
            if name == "voice.ogg":
                att = conv["messages"][0]["attachments"][0]
                check(bool(att.get("transcript")) and "вперед" in att["transcript"].lower(),
                      "files: the voice note's transcript is stored for the chat", str(att)[:300])
    finally:
        forget(s, created)


if __name__ == "__main__":
    fixtures = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/fa-fixtures")
    what = sys.argv[2] if len(sys.argv) > 2 else "all"
    if what in ("isolation", "all"):
        isolation()
    if what in ("files", "all"):
        files_check(fixtures)
    if what in ("discovery", "all"):
        discovery_check()
    if what in ("persona", "all"):
        persona_check()
    if what in ("nightshift", "all"):
        nightshift_check(fixtures)
    if what in ("edit", "all"):
        edit_check()
    if what in ("manager", "all"):
        manager_check()
    if what in ("investigate", "all"):
        investigate_check()
    print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1 if failures else 0)
