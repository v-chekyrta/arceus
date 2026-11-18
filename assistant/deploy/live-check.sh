#!/bin/bash
# Builds the fixtures for deploy/live-check.py on this Mac (a Ukrainian voice from `say`, a screenshot drawn by Pillow,
# Office files from assistant/.venv), copies them to the factory VM and runs the check there.
#   LIVE_TICKET=SHOP-12 LIVE_EDIT_TICKET=SHOP-13 deploy/live-check.sh [isolation|files|discovery|persona|nightshift|edit|manager|investigate|all]
# (tickets of the project: asked about, and proposed an edit that is never applied; see live-check.py)
# Needs: FA_SSH, the ssh command that reaches the VM (e.g. FA_SSH="ssh -p 22 deploy@vm.example"), macOS `say` with the Lesya voice, ffmpeg, ./.venv with
# python-docx, openpyxl and pillow.
set -euo pipefail
cd "$(dirname "$0")/.."
WHAT="${1:-all}"
SSH="${FA_SSH:?set FA_SSH to the ssh command that reaches the VM, e.g. FA_SSH=\"ssh -p 22 deploy@vm.example\"}"
FX="$(mktemp -d)"; trap 'rm -rf "$FX"' EXIT

say -v Lesya -o "$FX/voice.aiff" "Привіт. Клієнт пише, що на сторінці курсу не видно кнопки Вперед після третього уроку. Подивись, будь ласка."
ffmpeg -loglevel error -y -i "$FX/voice.aiff" -c:a libopus "$FX/voice.ogg"
say -v Lesya -o "$FX/nightshift-voice.aiff" "Картинки немає, банер лише текстом, помаранчевого кольору. Текст: вебінар штучний інтелект на уроці, двадцятого листопада о шістнадцятій. Показувати всім учасникам до двадцятого листопада включно, потім банер зникає сам. Погоджувати з Даною не треба."
ffmpeg -loglevel error -y -i "$FX/nightshift-voice.aiff" -c:a libopus "$FX/nightshift-voice.ogg"
.venv/bin/python - "$FX" <<'EOF'
import sys, email.message, docx, openpyxl
from PIL import Image, ImageDraw, ImageFont
fx = sys.argv[1]
img = Image.new("RGB", (900, 260), "white"); draw = ImageDraw.Draw(img)
draw.text((40, 100), "Помилка 502 — сторінка оплати", fill="black",
          font=ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 46))
img.save(f"{fx}/shot.png")
d = docx.Document(); d.add_paragraph("Бриф для фабрики"); d.add_paragraph("Кодове слово: ЖИРАФ-17"); d.save(f"{fx}/brief.docx")
wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Оцінки"
for row in (["email", "оцінка"], ["taras@school.ua", 64], ["olena@school.ua", 87]): ws.append(row)
wb.save(f"{fx}/grades.xlsx")
m = email.message.EmailMessage(); m["From"] = "client@school.ua"; m["Subject"] = "Не приходить сертифікат після курсу"
m.set_content("Добрий день! Учні закінчили курс, а сертифікат не приходить."); open(f"{fx}/mail.eml", "wb").write(bytes(m))
EOF
REMOTE="/tmp/fa-fixtures-$$"
tar -C "$FX" -cf - voice.ogg nightshift-voice.ogg shot.png brief.docx grades.xlsx mail.eml | $SSH "mkdir -p $REMOTE && tar -C $REMOTE -xf -"
$SSH "LIVE_TICKET=${LIVE_TICKET:-} LIVE_EDIT_TICKET=${LIVE_EDIT_TICKET:-} ASSISTANT_PORT=${ASSISTANT_PORT:-8190} /opt/arceus-assistant/venv/bin/python /opt/arceus-assistant/src/deploy/live-check.py $REMOTE $WHAT; rc=\$?; rm -rf $REMOTE; exit \$rc"
