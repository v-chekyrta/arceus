"""Run on the VM with the assistant's python: exits 0 only if the assistant has read a Slack channel into her log.

    ssh <vm> '/opt/arceus-assistant/venv/bin/python - C090TAJ12TW' < deploy/check-channel-read.py   # as its run user

Read means: more than MIN messages with their authors' names, the history read to the end (channel_backfill.done),
and Slack (or the owner's confirmation) holding the channel as ours alone (channel_status.shared = 0).
"""
import sqlite3
import sys
from pathlib import Path

MIN = 50
channel = sys.argv[1]
db = sqlite3.connect(str(Path(sys.argv[2] if len(sys.argv) > 2 else Path.home() / "assistant").expanduser()
                         / "data/assistant.db"))   # assistant.home
logged = db.execute("SELECT count(*) FROM channel_log WHERE channel=? AND deleted=0 AND author IS NOT NULL",
                    (channel,)).fetchone()[0]
done = db.execute("SELECT done FROM channel_backfill WHERE channel=?", (channel,)).fetchone()
shared = db.execute("SELECT shared FROM channel_status WHERE channel=?", (channel,)).fetchone()
print(f"{channel}: {logged} messages with authors, history read to the end: {bool(done and done[0])}, "
      f"ours only: {shared is not None and shared[0] == 0}")
sys.exit(0 if logged > MIN and done and done[0] == 1 and shared and shared[0] == 0 else 1)
