"""The architecture reviews' pages (architect.py), and the small server that shows them on localhost.

The pages share the test runs' look (qa_report.py): plain HTML, styles inline, nothing loaded from elsewhere. Each
run's folder under `architect.reports` holds `report.html` and `result.json` as the run left them, so a copy of the
folder still opens; it shows the findings as they were, without the buttons.

The server answers on `architect.address:architect.port` (127.0.0.1, through an SSH tunnel like Grafana) and builds
every page from runner.db on each visit, so a decision shows at once:

- `GET /` the runs and a count of the findings; `GET /<n>/` a run's report; `GET /findings/` every finding kept.
- `POST /findings/<n>` a person's decision on finding n: the risk accepted or a false positive, with why, or taken
  back. The form carries a token the server made when it started, and the request has to come from its own pages
  (Origin, Sec-Fetch-Site), so another site open in the same browser cannot decide for them.

Nothing else is served: not the instructions, not result.json, not a file of the folder.
"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from arceus_runner import qa_report
from arceus_runner.qa_report import _page, _took, _when, e, rich

HEADS = {"clean": "Nothing to raise", "partial": "Nothing to raise in what was read; some changes were not read",
         "found": None, "unchanged": None, "unfinished": "Did not finish", "stopped": "Stopped"}
SEVERITY_WORDS = {"high": "High", "medium": "Medium", "low": "Low"}
LENS_WORDS = {"coherence": "How the parts fit", "slop": "Code written to look busy", "tests": "Tests",
              "architecture": "Module worth deepening", "drift": "Drift from the team's rules"}
# The second check of each finding (architect.py, `_review`), in words that name no tool: it may change.
VERDICT_WORDS = {"confirmed": "Confirmed on second check", "doubtful": "Doubtful on second check",
                 "rejected": "Not confirmed on second check"}
DECISION_WORDS = {"accepted": "Risk accepted", "false_positive": "False positive"}
MAX_NOTE = 1000

STYLE = """
.strip { display: flex; flex-wrap: wrap; gap: 8px 24px; margin: 16px 0 24px; padding: 14px 18px;
         background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
.strip a { font-weight: 600; }
.strip span { color: var(--muted); }
.num { font: 600 0.85rem/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: var(--muted); }
.tag.high { color: var(--bad); background: var(--bad-soft); }
.tag.medium { color: var(--warn); background: var(--warn-soft); }
.tag.low, .tag.still, .tag.proposal { color: var(--quiet); background: var(--bg); }
.tag.new, .tag.violation { color: var(--blue); background: var(--blue-soft); }
.tag.fixed, .tag.accepted, .tag.false_positive { color: var(--good); background: var(--good-soft); }
.finding .lede { margin: 12px 0 0; }
.finding h4 { margin: 0 0 4px; font-size: 0.85rem; color: var(--muted); font-weight: 600; }
.finding .part { margin: 16px 0 0; }
.finding .part p { margin: 0; white-space: pre-line; }
.rule { margin: 16px 0 0; padding: 10px 14px; border-left: 3px solid var(--blue); background: var(--blue-soft);
        border-radius: 0 6px 6px 0; }
.rule p { margin: 0; }
.refs { margin: 6px 0 0; padding-left: 20px; }
.refs .note { color: var(--muted); }
.opinion { margin: 16px 0 0; padding: 12px 14px; border-radius: 6px; background: var(--bg); }
.opinion.confirmed { background: var(--good-soft); } .opinion.doubtful { background: var(--warn-soft); }
.opinion.rejected { background: var(--bad-soft); }
.opinion p { margin: 0; white-space: pre-line; }
.opinion strong { display: block; margin-bottom: 2px; }
.decision { margin: 20px 0 0; padding: 16px 0 0; border-top: 1px solid var(--line); }
.decision p { margin: 0 0 10px; }
.decision label { display: block; font-size: 0.9rem; color: var(--muted); margin: 0 0 6px; }
.decision textarea { display: block; width: 100%; min-height: 4.5rem; padding: 8px 10px; resize: vertical;
                     font: inherit; color: var(--ink); background: var(--bg); border: 1px solid var(--line);
                     border-radius: 6px; }
.decision textarea:focus-visible, .btn:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 0; }
.btn { font: inherit; font-size: 0.925rem; font-weight: 600; padding: 7px 14px; border-radius: 6px; cursor: pointer;
       color: var(--ink); background: var(--surface); border: 1px solid var(--line); }
.btn:hover { border-color: var(--muted); }
.brief { list-style: none; margin: 0; padding: 0; background: var(--surface); border: 1px solid var(--line);
         border-radius: 8px; }
.brief li { padding: 10px 16px; border-top: 1px solid var(--line); }
.brief li:first-child { border-top: 0; }
.brief .note { display: block; color: var(--muted); font-size: 0.9rem; }
details { margin: 12px 0 0; }
details summary { cursor: pointer; color: var(--blue); }
.criteria { margin: 8px 0 0; padding-left: 20px; } .criteria li { margin: 0 0 8px; }
.alert .objections { margin: 8px 0 0; white-space: pre-line; }
.who-decided { display: block; margin: -4px 0 10px; color: var(--muted); font-size: 0.875rem; }
.history { margin: 12px 0 0; } .history .journal { margin: 6px 0 0; }
.history .journal li { padding: 6px 0; border-top: 1px solid var(--line); font-size: 0.9rem; overflow-wrap: anywhere; }
.history .journal li:first-child { border-top: 0; }
.history .at { display: block; color: var(--muted); font-size: 0.8rem; }
.history .note { display: block; color: var(--muted); }
"""


def head_of(outcome: str | None, findings: int, since_run: int | None = None) -> str:
    if outcome == "found":
        return f"{findings} finding{'s' if findings != 1 else ''}"
    if outcome == "unchanged":
        return f"Nothing changed since run {since_run}" if since_run else "Nothing changed to review"
    return HEADS.get(outcome or "", None) or "In progress"


def _count(n: int, noun: str) -> str:
    return f"{e(n)} {noun}{'s' if n != 1 else ''}"


def _strong(text: str) -> str:
    """Escaped text with the criteria's **bold** kept."""
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rich(text))


def _criteria(text: str) -> str:
    items, tail = [], ""
    for block in text.split("\n\n"):
        m = re.match(r"^\d+\. (.*)$", block, re.S)
        if m:
            items.append(f"<li>{_strong(m.group(1))}</li>")
        else:
            tail = f"<p>{_strong(block)}</p>"
    return f'<ol class="criteria">{"".join(items)}</ol>{tail}'


@dataclass(frozen=True)
class Forms:
    """How the live pages' decision forms post: on the runner's own page behind the SSH tunnel, or in the web
    console (console/views_arch.py), where only some people may decide."""
    token: str                # the form's token
    field: str = "token"      # its field's name
    prefix: str = ""          # before /findings/<n>: "" on the tunnel's page, "/architecture" in the console
    can_decide: bool = True   # False: the decision and its history are shown, the buttons are not
    when: Callable[[str | None], str] | None = None  # a stored time in words (the console's own)


DECIDED_BY = {"accepted": "Risk accepted", "false_positive": "False positive", "open": "Taken back"}


def _history(row: dict, forms: Forms) -> str:
    """Every decision on the finding, newest first: who, when, what, why."""
    items = row.get("history") or []
    if not items:
        return ""
    when = forms.when or (lambda s: s or "")
    lines = []
    for d in reversed(items):
        why = f' <span class="note">{rich(d["note"])}</span>' if d.get("note") else ""
        lines.append(f'<li><span class="at">{e(when(d["at"]))}</span>{e(d["decided_by"])}: '
                     f'{e(DECIDED_BY.get(d["decision"], d["decision"]))}{why}</li>')
    return (f'<details class="history"><summary>Decisions on it ({len(items)})</summary>'
            f'<ul class="journal">{"".join(lines)}</ul></details>')


def _decision(n: int, row: dict | None, forms: Forms, back: str) -> str:
    """What a person decided on finding n, who and when, its history, and the form to decide or take it back.
    Only on the live pages; the buttons only for whoever may decide."""
    if row is None:
        return ""
    if row["status"] == "fixed":
        return (f'<div class="decision"><p><span class="tag fixed">Fixed</span> A later review found it gone '
                f'(run {e(row["fixed_run"])}).</p>{_history(row, forms)}</div>')
    # The version the page shows: a decision sent from an older page than the finding is refused (conflicts).
    hidden = (f'<input type="hidden" name="{e(forms.field)}" value="{e(forms.token)}">'
              f'<input type="hidden" name="back" value="{e(back)}#f{n}">'
              f'<input type="hidden" name="version" value="{e(row.get("updated_at"))}">')
    action = f"{forms.prefix}/findings/{n}"
    when = forms.when or (lambda s: s or "")
    if row.get("decision"):
        word = DECISION_WORDS.get(row["decision"], e(row["decision"]))
        who = ""
        if row.get("decided_by") or row.get("decided_at"):
            who = (f'<span class="who-decided">{e(row.get("decided_by") or "Someone")}'
                   f'{", " + e(when(row.get("decided_at"))) if row.get("decided_at") else ""}</span>')
        note = f" {rich(row['decision_note'])}" if row.get("decision_note") else ""
        form = ""
        if forms.can_decide:
            form = (f'<form method="post" action="{action}">{hidden}<input type="hidden" name="decision" value="open">'
                    f'<button class="btn" type="submit">Take the decision back</button></form>')
        return (f'<div class="decision"><p><span class="tag {e(row["decision"])}">{word}</span>{note}</p>'
                f'{who}{form}{_history(row, forms)}</div>')
    if not forms.can_decide:
        return (f'<div class="decision"><p><span class="tag still">Open</span> Nobody has decided on it yet.</p>'
                f'{_history(row, forms)}</div>')
    return (f'<form class="decision" method="post" action="{action}">{hidden}'
            f'<label for="why{n}">Why, for whoever reads this later (the next review gets it too)</label>'
            f'<textarea id="why{n}" name="note" maxlength="{MAX_NOTE}" required></textarea>'
            '<div class="actions">'
            '<button class="btn" type="submit" name="decision" value="accepted">Accept the risk</button>'
            '<button class="btn" type="submit" name="decision" value="false_positive">False positive</button>'
            f"</div>{_history(row, forms)}</form>")


def _finding(f: dict, n: int | None, standing: str | None, verdict: dict | None, row: dict | None,
             forms: Forms | None = None, back: str = "", seen: str = "") -> str:
    """One finding: its tags, what is wrong and what it risks, the rule, the way out, the code, its second check,
    and on the live pages the decision."""
    sev = f.get("severity") or "medium"
    kind = f.get("kind") or "proposal"
    tags = [f'<span class="tag {e(sev)}">{SEVERITY_WORDS.get(sev, e(sev))}</span>',
            f'<span class="tag {e(kind)}">{"Rule broken" if kind == "violation" else "Proposal"}</span>']
    if standing:
        tags.append(f'<span class="tag {e(standing)}">{"New" if standing == "new" else "Still open"}</span>')
    anchor = f' id="f{n}"' if n else ""
    number = f'<span class="num">#{e(n)}</span>' if n else ""
    parts = [f'<article class="finding"{anchor}>',
             f'<header>{number}<h3>{rich(f["title"])}</h3>{"".join(tags)}</header>']
    meta = [e(seen), LENS_WORDS.get(f.get("lens") or "", "")]
    if f.get("components"):
        meta.append("Parts: " + ", ".join(f'<code class="lit">{e(c)}</code>' for c in f["components"]))
    parts.append(f'<p class="meta">{" · ".join(m for m in meta if m)}</p>')
    if f.get("rule"):
        parts.append(f'<div class="rule"><h4>The rule it breaks</h4><p>{rich(f["rule"])}</p></div>')
    for key, label in (("what", "What is wrong"), ("risk", "What it risks"), ("direction", "The way out")):
        if f.get(key):
            parts.append(f'<div class="part"><h4>{label}</h4><p>{rich(f[key])}</p></div>')
    refs = []
    for c in f.get("where") or []:
        where = f"{c['repo']}/{c['file']}" + (f":{c['line']}" if c.get("line") else "")
        label = f"<code>{e(where)}</code>" + (f" at <code>{e(c['sha'][:12])}</code>" if c.get("sha") else "")
        link = f'<a href="{e(c["url"])}">{label}</a>' if c.get("url") else label
        refs.append(f"<li>{link}" + (f' <span class="note">{rich(c["note"])}</span>' if c.get("note") else "") + "</li>")
    if refs:
        parts.append(f'<div class="part"><h4>In the code</h4><ul class="refs">{"".join(refs)}</ul></div>')
    if verdict:
        v = verdict.get("verdict")
        parts.append(f'<div class="opinion {e(v)}"><strong>{VERDICT_WORDS.get(v, e(v))}</strong>'
                     f'<p>{rich(verdict.get("reason"))}</p></div>')
    if n and forms:
        parts.append(_decision(n, row, forms, back))
    parts.append("</article>")
    return "\n".join(parts)


def second_check(so: dict | None, findings: int) -> str:
    """The second check of a run's findings in a line: how many it confirmed, how many got no verdict; or that it
    did not run, and why. Never a count for a check that did not run."""
    if not so:
        return ""
    if so.get("status") == "done":
        verdicts = list((so.get("verdicts") or {}).values())
        confirmed = sum(1 for v in verdicts if v["verdict"] == "confirmed")
        missing = findings - len(verdicts)
        return f"confirmed {confirmed} of {findings}" + (f", {missing} without a verdict" if missing > 0 else "")
    if so.get("status") == "skipped":
        return f"not run: {so.get('why')}"
    return f"did not run: {so.get('why') or 'no answer'}"


def render_run(view: dict, ledger: dict[int, dict], project: str, tz: ZoneInfo, token: str = "",
               criteria: str = "") -> str:
    """One run's report as a page of its own (report.html; with a `token`, the runner's live page)."""
    return _page(*run_body(view, ledger, project, tz, Forms(token) if token else None, criteria), STYLE)


def run_body(view: dict, ledger: dict[int, dict], project: str, tz: ZoneInfo, forms: Forms | None = None,
             criteria: str = "") -> tuple[str, str]:
    """One run's report, its title and body: the verdict and the facts, the summary, its findings (new and still
    open, each with its second check), what it found fixed and what it did not check again, then what it read. With
    `forms` (a live page) every kept finding has its decision; without, the page is the run as it was."""
    findings = view.get("findings") or []
    outcome = view.get("outcome")
    # Who typed the command on the server is the runner's own user: only the schedule says something.
    by = ", on schedule" if view.get("source") == "schedule" else ""
    since = f"run {view['since_run']}" if view.get("since_run") else "nothing yet: this is the first review"
    ins = view.get("instructions") or {}
    told = f"{ins.get('path')}, version {str(ins.get('sha256'))[:12]}" if ins.get("bytes") else \
        "none written: the runner's criteria only"
    facts = [("Started", f"{_when(view.get('started_at') or view.get('created_at'), tz)}{by}"),
             ("Finished", _when(view.get("finished_at"), tz)),
             ("Took", _took(view.get("started_at"), view.get("finished_at"))),
             ("Compared with", since if view.get("scope") or view.get("since_run") else ""),
             ("Instructions", told if ins else ""),
             ("Second check", second_check(view.get("second_opinion"), len(findings)))]
    body = [f'<p class="crumb"><a href="../">All reviews of {e(project)}</a></p>',
            f"<h1>Architecture review, run {e(view['n'])}</h1>",
            f'<p class="verdict {e(outcome)}">{e(head_of(outcome, len(findings), view.get("since_run")))}</p>',
            '<dl class="facts">' + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in facts if v) + "</dl>"]
    if view.get("summary"):
        body.append(f'<p class="summary">{rich(view["summary"])}</p>')
    fatal = view.get("fatal") or []
    if fatal:
        body.append('<div class="alert"><p><strong>The run did not finish as it should.</strong> What is below is '
                    'what it reported before that; its findings are not kept for the next review.</p><ul>'
                    + "".join(f"<li>{e(x)}</li>" for x in fatal) + "</ul></div>")
    if view.get("engine_review"):
        body.append('<div class="alert note"><p><strong>The agent\'s report as a whole did not pass review.</strong> '
                    "The objections it did not settle are below; each finding has its own second check:</p>"
                    f'<p class="objections">{rich(view["engine_review"])}</p></div>')
    doubts = view.get("doubts") or []
    if doubts:
        body.append('<div class="alert note"><p><strong>Not a clean result:</strong></p><ul>'
                    + "".join(f"<li>{e(x)}</li>" for x in doubts) + "</ul></div>")
    led = view.get("ledger") or {}
    numbers = led.get("findings") or []
    new = set(led.get("new") or [])
    verdicts = (view.get("second_opinion") or {}).get("verdicts") or {}
    prefix = forms.prefix if forms else ""
    back = f"{prefix}/{view['n']}/"
    if outcome != "unchanged":
        body.append("<h2>Findings</h2>")
        if findings:
            for i, f in enumerate(findings, 1):
                n = numbers[i - 1] if i <= len(numbers) else None
                standing = None if n is None else "new" if n in new else "still"
                body.append(_finding(f, n, standing, verdicts.get(str(i)), ledger.get(n) if n else None, forms, back))
        else:
            body.append('<p class="empty">None raised.</p>')
    notes = {p["id"]: p.get("note") or "" for p in view.get("previous") or []}
    for key, title, lead in (
            ("fixed", "Fixed since the last review", "Checked again and gone."),
            ("still", "Still open, checked again", "Seen again; not raised again in full."),
            ("unchecked", "Not checked this time", "Their code was not among what this run read.")):
        shown = [n for n in led.get(key) or [] if n not in numbers]
        if not shown:
            continue
        items = []
        for n in shown:
            row = ledger.get(n) or {}
            title_n = row.get("title") or f"Finding {n}"
            link = f'<a href="{prefix}/findings/#f{e(n)}">#{e(n)}</a>' if forms else f"#{e(n)}"
            note = f'<span class="note">{rich(notes[n])}</span>' if notes.get(n) else ""
            items.append(f"<li>{link} {rich(title_n)}{note}</li>")
        body.append(f"<h2>{title}</h2><p>{lead}</p><ul class=\"brief\">{''.join(items)}</ul>")
    scope = view.get("scope") or []
    if scope:
        read = {r["repo"]: r["what"] for r in view.get("reviewed") or []}
        skipped = {r["repo"]: r["why"] for r in view.get("skipped") or []}
        rows = []
        for s in scope:
            rng = f"<code>{e(s['base'][:12])}…{e(s['sha'][:12])}</code>" if s.get("base") else \
                f"everything at <code>{e(s['sha'][:12])}</code>"
            if s["repo"] in skipped:
                said = f"Not read: {rich(skipped[s['repo']] or 'no reason given')}"
            else:
                said = rich(read.get(s["repo"], "The agent did not say"))
            not_ours = " Not the team's own code." if s.get("not_ours") else (
                f" {e(s['not_ours_files'])} of the files are not the team's own." if s.get("not_ours_files") else "")
            rows.append(f"<li><code>{e(s['repo'])}</code>: changes since {e(s['since'])}, {rng}, "
                        f"{_count(s['commits'], 'commit')}, {_count(s['files'], 'file')} added or changed"
                        + (f", {e(s['deleted'])} deleted" if s.get("deleted") else "") + f".{not_ours}"
                        f'<span class="note">{said}</span></li>')
        body.append(f'<h2>What was read</h2><ul class="brief">{"".join(rows)}</ul>')
        if view.get("context"):
            body.append("<p>Unchanged, read only for context: "
                        + ", ".join(f"<code>{e(c)}</code>" for c in view["context"]) + ".</p>")
    problems = view.get("problems") or []
    if problems:
        body.append('<div class="alert note"><p><strong>Left out of this report</strong> (the agent\'s report did not '
                    'hold up there):</p><ul>' + "".join(f"<li>{e(p)}</li>" for p in problems) + "</ul></div>")
    if criteria:
        version = f", version {e(view.get('criteria'))}" if view.get("criteria") else ""
        body.append(f"<footer><details><summary>The criteria this review used (the runner's{version})</summary>"
                    f"{_criteria(criteria)}</details></footer>")
    title = f"Review {view['n']}: {head_of(outcome, len(findings), view.get('since_run'))}"
    return title, "\n".join(body)


def _ledger_counts(rows: list[dict]) -> tuple[int, int, int]:
    open_ = sum(1 for r in rows if r["status"] == "open" and not r.get("decision"))
    decided = sum(1 for r in rows if r["status"] == "open" and r.get("decision"))
    fixed = sum(1 for r in rows if r["status"] == "fixed")
    return open_, decided, fixed


def render_index(runs: list[dict], ledger: list[dict], project: str, tz: ZoneInfo, live: bool = False) -> str:
    """Every run, newest first, under a count of the findings kept."""
    body = [f"<h1>Architecture reviews of {e(project)}</h1>"]
    if ledger:
        open_, decided, fixed = _ledger_counts(ledger)
        first = f'<a href="findings/">{open_} open finding{"s" if open_ != 1 else ""}</a>' if live else \
            f"<strong>{open_} open finding{'s' if open_ != 1 else ''}</strong>"
        body.append(f'<p class="strip">{first}<span>{decided} decided</span><span>{fixed} fixed</span></p>')
    if not runs:
        body.append('<p class="empty">No reviews yet. Start one on the server with <code>arceus-runner arch run'
                    "</code>.</p>")
        return _page(f"Architecture reviews of {project}", "\n".join(body), STYLE)
    trs = []
    for r in sorted(runs, key=lambda r: r["n"], reverse=True):
        if r["state"] != "done":
            result = "Queued" if r["state"] == "pending" else "Reviewing now" \
                if r["state"] in ("preparing", "running") else "Checking the findings again" \
                if r["state"] == "reviewing" else "Writing the report"
            result += ", stop asked" if r.get("stop_requested") else ""
        else:
            result = head_of(r["outcome"], r["findings"])
            if r["outcome"] == "unchanged":
                result = "Nothing changed"
            extra = [f"{r['new']} new" if r.get("new") else "", f"{r['fixed']} fixed" if r.get("fixed") else ""]
            if any(extra):
                result += f" ({', '.join(x for x in extra if x)})"
        done = r["state"] == "done"
        run = f'<a href="{e(r["n"])}/">Run {e(r["n"])}</a>' if done else f"Run {e(r['n'])}"
        summary = f'<span class="run-summary">{rich(r["summary"])}</span>' if r.get("summary") else ""
        by = "Schedule" if r["source"] == "schedule" else "Command"
        cls = {"clean": "passed", "unchanged": "stopped"}.get(r["outcome"], r["outcome"]) if done else ""
        trs.append(f'<tr><td class="nowrap">{run}</td><td><span class="{e(cls)}">{e(result)}</span>{summary}</td>'
                   f"<td class=\"hide-narrow nowrap\">{e(_when(r.get('started_at') or r.get('created_at'), tz))}</td>"
                   f"<td class=\"hide-narrow\">{by}</td></tr>")
    body.append('<table><thead><tr><th>Run</th><th>Result</th><th class="hide-narrow">Started</th>'
                '<th class="hide-narrow">How</th></tr></thead><tbody>' + "".join(trs) + "</tbody></table>")
    return _page(f"Architecture reviews of {project}", "\n".join(body), STYLE)


def render_findings(ledger: list[dict], project: str, token: str) -> str:
    """Every finding kept, as the runner's own live page."""
    return _page(*findings_body(ledger, project, Forms(token)), STYLE)


def findings_body(ledger: list[dict], project: str, forms: Forms) -> tuple[str, str]:
    """Every finding kept: the open ones first, then the ones a person decided on, then the fixed ones."""
    body = [f'<p class="crumb"><a href="../">All reviews of {e(project)}</a></p>', f"<h1>Findings of {e(project)}</h1>"]
    open_, decided, fixed = _ledger_counts(ledger)
    body.append(f'<p class="strip"><strong>{open_} open</strong><span>{decided} decided</span>'
                f"<span>{fixed} fixed</span></p>")
    groups = (("Open", [r for r in ledger if r["status"] == "open" and not r.get("decision")],
               "Nobody has decided on these yet."),
              ("Decided", [r for r in ledger if r["status"] == "open" and r.get("decision")],
               "The next review is told of these and does not raise them again without new grounds."),
              ("Fixed", [r for r in ledger if r["status"] == "fixed"], "A later review checked and found them gone."))
    if not ledger:
        body.append('<p class="empty">No findings kept yet.</p>')
    for title, rows, lead in groups:
        if not rows:
            continue
        body.append(f"<h2>{title}</h2><p>{lead}</p>")
        for r in sorted(rows, key=lambda r: r["n"], reverse=True):
            f = r["body"]
            seen = f"Raised in run {r['first_run']}" + (f", last seen in run {r['last_run']}"
                                                         if r["last_run"] != r["first_run"] else "")
            body.append(_finding(f, r["n"], None, f.get("verdict"), r, forms, f"{forms.prefix}/findings/", seen))
    return f"Findings of {project}", "\n".join(body)


# -- the server -----------------------------------------------------------------------------------------------------

_RUN = re.compile(r"^/(\d{1,9})/$")
_DECIDE = re.compile(r"^/findings/(\d{1,9})$")
_BACK = re.compile(r"^/(?:\d{1,9}/|findings/)?(?:#f\d{1,9})?$")


class _Handler(qa_report._Handler):
    pages: Callable[[str, str], str | None]
    decide: Callable[[int, str, str, str | None], str | None]
    token = ""
    server_version = "arceus-arch"
    csp = ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; "
           "frame-ancestors 'none'")

    def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
        bare = self.path.split("?", 1)[0].split("#", 1)[0]
        if re.match(r"^/(\d{1,9}|findings)$", bare):
            self.send_response(301)
            self.send_header("Location", bare + "/")
            self.end_headers()
            return
        if bare == "/" or bare == "/findings/" or _RUN.match(bare):
            page = self.pages(bare, self.token)
            if page is not None:
                self._send(page.encode(), "text/html; charset=utf-8")
                return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        m = _DECIDE.match(self.path)
        if not m:
            self.send_error(404, "Not found")
            return
        if not self._same_origin():
            self.send_error(403, "Decisions are taken on this server's own pages")
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = -1
        if not 0 < size <= 16 * 1024:
            self.send_error(400, "No form, or one too large")
            return
        form = urllib.parse.parse_qs(self.rfile.read(size).decode("utf-8", "replace"), max_num_fields=10)
        token = (form.get("token") or [""])[0]
        if not hmac.compare_digest(token.encode(), self.token.encode()):
            self.send_error(403, "The page is out of date: reload it and decide again")
            return
        decision = (form.get("decision") or [""])[0]
        note = (form.get("note") or [""])[0].strip()
        back = (form.get("back") or ["/"])[0]
        if decision not in ("accepted", "false_positive", "open"):
            self.send_error(400, "No such decision")
            return
        if decision != "open" and not note:
            self.send_error(400, "Say why: the next review and whoever reads this later get the reason")
            return
        version = (form.get("version") or [""])[0] or None
        problem = self.decide(int(m.group(1)), decision, note[:MAX_NOTE], version)
        if problem:
            self.send_error(404 if "no finding" in problem else 409, problem)
            return
        self.send_response(303)
        self.send_header("Location", back if _BACK.match(back) else "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _same_origin(self) -> bool:
        """The browser's own word on where the form came from: a request another site made is refused. A client
        that sends neither header (curl through the tunnel) still needs the token."""
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        return not origin or origin == f"http://{self.headers.get('Host')}"


def serve(root: Path, address: str, port: int, pages: Callable[[str, str], str | None],
          decide: Callable[[int, str, str], str | None]):
    """Start the server in a thread of its own. Returns the port it listens on and a function that stops it.
    `pages(path, token)` builds a page from runner.db; `decide(n, decision, note)` records a decision, or says why
    it could not."""
    root.mkdir(parents=True, exist_ok=True)
    handler = type("Handler", (_Handler,), {"root": root.resolve(), "pages": staticmethod(pages),
                                            "decide": staticmethod(decide), "token": secrets.token_urlsafe(24)})
    server = ThreadingHTTPServer((address, port), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="arch-reports", daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return server.server_address[1], stop
