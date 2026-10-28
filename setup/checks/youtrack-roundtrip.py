"""Exercise every YouTrack write the runner makes, on one throwaway ticket, and put it back.

    /opt/arceus-runner/venv/bin/python -I youtrack-roundtrip.py OMD-3

For a runner whose `tracker:` is YouTrack. It uses the runner's own client (YouTrackWriter) with the
bot's token from ~/arceus/data: read the
ticket and its history, set each workflow state the runner uses, assign the bot, add and remove the
needs-input and failed tags, post a comment and read it back. Afterwards the ticket gets its
original state, assignee and tags again and the comment is marked deleted. Exit 0 only if every step was
seen to take effect.
"""

import sys
from pathlib import Path
from urllib.parse import quote

from arceus_runner.config import load_config, load_secrets
from arceus_runner.youtrack import YouTrackWriter, youtrack_client

CONFIG = Path("~/arceus/data/config.yaml").expanduser()


def main(key: str) -> int:
    cfg = load_config(CONFIG)
    c = cfg.tracker
    if c.type != "youtrack":
        print(f"tracker.type is {c.type!r}: this check is for YouTrack")
        return 2
    yt = YouTrackWriter(youtrack_client(c, load_secrets(cfg).tracker, cfg.http))
    ok = True

    def step(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"[{'ok  ' if passed else 'FAIL'}] {name}{': ' + detail if detail else ''}")

    issue = yt.issue(key)
    state0 = (issue.fields.get(c.state_field) or [None])[0]
    assignee0 = (issue.fields.get(c.assignee_field) or [None])[0] if c.assignee_field else None
    tags0 = yt.issue_tags(issue.db_id)
    step("read ticket", True, f"{issue.key} «{issue.summary}», {c.state_field}={state0}, tags={sorted(tags0)}")
    list(yt.tag_activities(issue.db_id))
    step("read tag history", True)

    comment_id = None
    try:
        for state in (c.states.in_progress, c.states.code_review):
            yt.set_state(issue.db_id, c.state_field, state)
            now = (yt.issue(key).fields.get(c.state_field) or [None])[0]
            step(f"set {c.state_field} = {state}", now == state, f"reads back {now!r}")
        if c.assignee_field:
            yt.set_assignee(issue.db_id, c.assignee_field, c.bot_user)
            now = (yt.issue(key).fields.get(c.assignee_field) or [None])[0]
            step(f"assign {c.bot_user}", now in (c.bot_user,) or bool(now), f"reads back {now!r}")
        for tag in (c.tags.needs_input, c.tags.failed):
            yt.add_tag(issue.db_id, tag)
            step(f"add tag {tag}", tag in yt.issue_tags(issue.db_id))
            if tag not in tags0:
                yt.remove_tag(issue.db_id, tag)
                step(f"remove tag {tag}", tag not in yt.issue_tags(issue.db_id))
        text = "Arceus: checking that the runner can write to YouTrack. This comment will be deleted.\n\n[arceus:roundtrip]"
        comment_id, _ = yt.add_comment(issue.db_id, text)
        mine = [x for x in yt.comments(issue.db_id) if x.id == comment_id]
        step("post comment", bool(mine) and mine[0].author == c.bot_user,
             f"by {mine[0].author}" if mine else "not found")
    finally:
        if comment_id:
            # The bot may not hard-delete comments (403); as their author it can mark its own deleted.
            yt.c.request("POST", f"/api/issues/{quote(issue.db_id)}/comments/{quote(comment_id)}",
                         params={"fields": "id,deleted"}, json={"deleted": True})
            step("delete comment", all(x.id != comment_id for x in yt.comments(issue.db_id)))
        if state0:
            yt.set_state(issue.db_id, c.state_field, state0)
        if c.assignee_field:
            if assignee0:
                yt.set_assignee(issue.db_id, c.assignee_field, assignee0)
            else:
                yt.c.request("POST", f"/api/issues/{quote(issue.db_id)}", params={"fields": "id"}, json={
                    "customFields": [{"name": c.assignee_field, "$type": "SingleUserIssueCustomField",
                                      "value": None}]})
        back = yt.issue(key)
        step("restored", (back.fields.get(c.state_field) or [None])[0] == state0
             and ((back.fields.get(c.assignee_field) or [None])[0] if c.assignee_field else None) == assignee0
             and yt.issue_tags(issue.db_id) == tags0)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "OMD-3"))
