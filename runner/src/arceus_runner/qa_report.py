"""The test runs' reports (qa.py) as pages, and the small server that shows them on localhost.

Each run's folder under `qa.reports` holds `report.html`, `result.json`, the copy of the instructions it was given
and `media/` with its screenshots; `index.html` beside the folders lists every run. The pages are plain HTML with
their styles inline and nothing loaded from elsewhere, so a run's folder copied off the server still opens.

The server answers on `qa.address:qa.port` (127.0.0.1 by default, reached through an SSH tunnel like Grafana) and
serves exactly three kinds of path: `/` (the list, built from runner.db on every visit, so a queued or running run
shows there at once), `/<n>/` (a run's report) and `/<n>/media/<file>` (its screenshots). Nothing else under the folder is served: not the instructions, which hold the test accounts, not
result.json, not a link.
"""

from __future__ import annotations

import html
import re
import threading
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from arceus_runner import media
from arceus_runner.literals import spans

HEADS = {"passed": "No problems found", "partial": "No problems in what was checked; some could not be checked",
         "found": None, "blocked": "Could not test",
         "unfinished": "Did not finish", "stopped": "Stopped"}
RESULT_WORDS = {"ok": "Works", "problem": "Problem", "blocked": "Could not check", "skipped": "Skipped"}
SEVERITY_WORDS = {"critical": "Critical", "major": "Major", "minor": "Minor"}

STYLE = """
:root {
  --bg: #f4f5f7; --surface: #ffffff; --ink: #1f2430; --muted: #5d6576; --line: #dfe3ea;
  --blue: #2f74c8; --good: #157f5b; --warn: #a66e00; --bad: #c03333; --quiet: #7a7f88;
  --bad-soft: #fbeaea; --warn-soft: #fbf3e2; --good-soft: #e6f4ee; --blue-soft: #e9f1fb;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #181b1f; --surface: #22252b; --ink: #e4e7ec; --muted: #a2a9b6; --line: #343840;
    --blue: #6ea8f0; --good: #3fbf8f; --warn: #e3a92c; --bad: #ef6b6b; --quiet: #8d929b;
    --bad-soft: #3a2326; --warn-soft: #3a3121; --good-soft: #1e352d; --blue-soft: #1f2d40;
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, "Helvetica Neue", Arial, sans-serif;
}
main { max-width: 920px; margin: 0 auto; padding: 32px 16px 64px; }
a { color: var(--blue); text-underline-offset: 2px; }
a:focus-visible, summary:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; border-radius: 3px; }
code { font: 0.875em/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; }
code.lit { padding: 0 4px; border-radius: 4px; background: var(--bg); border: 1px solid var(--line); }
h1 { font-size: 1.75rem; line-height: 1.2; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 1.15rem; margin: 40px 0 12px; }
h3 { font-size: 1.05rem; line-height: 1.35; margin: 0; }
.crumb { margin: 0 0 20px; font-size: 0.9rem; }
.verdict { font-size: 1.15rem; font-weight: 600; margin: 0 0 16px; }
.verdict.passed { color: var(--good); } .verdict.found, .verdict.unfinished, .verdict.partial { color: var(--warn); }
.verdict.blocked { color: var(--bad); } .verdict.stopped { color: var(--quiet); }
.facts { display: grid; grid-template-columns: max-content 1fr; gap: 4px 20px; margin: 0; color: var(--muted);
         font-size: 0.925rem; }
.facts dt { color: var(--muted); } .facts dd { margin: 0; color: var(--ink); overflow-wrap: anywhere; }
.summary { margin: 24px 0 0; padding: 16px 20px; background: var(--surface); border: 1px solid var(--line);
           border-radius: 8px; white-space: pre-line; }
.alert { margin: 16px 0 0; padding: 12px 16px; border-radius: 8px; background: var(--bad-soft); }
.alert.note { background: var(--warn-soft); }
.alert p { margin: 0; } .alert ul { margin: 6px 0 0; padding-left: 20px; }
.checks { list-style: none; margin: 0; padding: 0; background: var(--surface); border: 1px solid var(--line);
          border-radius: 8px; }
.checks li { display: grid; grid-template-columns: 9.5rem 1fr; gap: 12px; padding: 10px 16px;
             border-top: 1px solid var(--line); }
.checks li:first-child { border-top: 0; }
.checks .note { display: block; color: var(--muted); font-size: 0.9rem; }
.tag { display: inline-block; font-size: 0.8rem; font-weight: 600; padding: 1px 8px; border-radius: 999px;
       white-space: nowrap; align-self: start; justify-self: start; }
.tag.ok { color: var(--good); background: var(--good-soft); }
.tag.problem, .tag.critical { color: var(--bad); background: var(--bad-soft); }
.tag.major { color: var(--warn); background: var(--warn-soft); }
.tag.blocked, .tag.skipped, .tag.minor { color: var(--quiet); background: var(--bg); }
.finding { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 20px;
           margin: 0 0 16px; }
.finding header { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.finding header h3 { flex: 1 1 18rem; }
.finding .meta { margin: 6px 0 0; color: var(--muted); font-size: 0.9rem; overflow-wrap: anywhere; }
.steps { margin: 16px 0 0; padding: 0; list-style: none; counter-reset: step; }
.steps > li { counter-increment: step; position: relative; padding: 6px 0 6px 40px; }
.steps > li::before { content: counter(step); position: absolute; left: 0; top: 6px; width: 26px; height: 26px;
                      border-radius: 50%; background: var(--bg); color: var(--muted); font-size: 0.8rem;
                      font-weight: 600; display: grid; place-items: center; }
.steps > li.failed::before { background: var(--bad); color: #fff; }
.steps > li.failed > .text { font-weight: 600; }
.shots { display: flex; flex-wrap: wrap; gap: 12px; margin: 10px 0 4px; }
.shots figure { margin: 0; max-width: 100%; }
.shots img, .shots video { display: block; max-width: min(100%, 420px); max-height: 320px; border-radius: 6px;
                           border: 1px solid var(--line); background: var(--bg); }
.shots figcaption { font-size: 0.85rem; color: var(--muted); margin-top: 4px; max-width: 420px; }
.compare { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 16px 0 0; }
.compare div { padding: 12px 14px; border-radius: 6px; background: var(--bg); }
.compare h4, .cause h4 { margin: 0 0 4px; font-size: 0.85rem; color: var(--muted); font-weight: 600; }
.compare p, .cause p { margin: 0; white-space: pre-line; }
.compare .actual { background: var(--bad-soft); }
.cause { margin: 16px 0 0; padding: 12px 14px; border: 1px dashed var(--line); border-radius: 6px; }
.cause ul { margin: 8px 0 0; padding-left: 20px; }
table { width: 100%; border-collapse: collapse; background: var(--surface); border: 1px solid var(--line);
        border-radius: 8px; overflow: hidden; }
th, td { text-align: left; padding: 10px 14px; border-top: 1px solid var(--line); vertical-align: top; }
th { font-size: 0.85rem; color: var(--muted); font-weight: 600; border-top: 0; }
td.nowrap { white-space: nowrap; }
td .passed { color: var(--good); } td .found, td .unfinished, td .partial { color: var(--warn); }
td .blocked { color: var(--bad); } td .stopped { color: var(--quiet); }
.run-summary { color: var(--muted); font-size: 0.9rem; display: block; margin-top: 2px; }
.empty { padding: 24px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
footer { margin-top: 48px; color: var(--muted); font-size: 0.875rem; }
footer ul { padding-left: 20px; }
@media (max-width: 640px) {
  main { padding-top: 20px; }
  .compare { grid-template-columns: 1fr; }
  .checks li { grid-template-columns: 1fr; gap: 4px; }
  .facts { grid-template-columns: 1fr; gap: 0; } .facts dd { margin-bottom: 6px; }
  .hide-narrow { display: none; }
}
"""


def e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def rich(value: object) -> str:
    """An agent's text as HTML: escaped, with the code in it (backticks, file paths) set as code (literals.py)."""
    return "".join(e(p) if kind == "text" else f'<code class="lit">{e(p)}</code>'
                   for p, kind in spans(str(value or "")))


def _when(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return ""
    try:
        at = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
    except ValueError:
        return iso
    return f"{at:%a %d %b %Y, %H:%M}"


def _took(start: str | None, end: str | None) -> str:
    try:
        a = datetime.fromisoformat((start or "").replace("Z", "+00:00"))
        b = datetime.fromisoformat((end or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    minutes = max(0, round((b - a).total_seconds() / 60))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


def head_of(outcome: str | None, findings: int) -> str:
    if outcome == "found":
        return f"{findings} problem{'s' if findings != 1 else ''} found"
    return HEADS.get(outcome or "", None) or "In progress"


def _page(title: str, body: str, style: str = "") -> str:
    """A whole page: its styles inline (the reports' own, then `style`), nothing loaded from elsewhere."""
    return (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<meta name="robots" content="noindex">\n<title>{e(title)}</title>\n<style>{STYLE}{style}</style>\n'
            f'</head>\n<body>\n<main>\n{body}\n</main>\n</body>\n</html>\n')


def _shot(s: dict) -> str:
    src = f"media/{e(s['file'])}"
    caption = f"<figcaption>{rich(s['caption'])}</figcaption>" if s.get("caption") else ""
    if s.get("kind") == "video":
        thing = f'<video src="{src}" controls preload="metadata"></video>'
    else:
        thing = f'<a href="{src}"><img src="{src}" alt="{e(s.get("caption") or "Screenshot")}"></a>'
    return f"<figure>{thing}{caption}</figure>"


def _finding(i: int, f: dict) -> str:
    sev = f.get("severity") or "major"
    tries = ""
    if f.get("attempts"):
        tries = f"Happened {f.get('reproduced') or 0} of {f['attempts']} times"
        if (f.get("reproduced") or 0) < 2:
            tries += ", may be intermittent"
    meta = [x for x in (tries, f'<a href="{e(f["url"])}">{e(f["url"])}</a>' if f.get("url") else "") if x]
    parts = [f'<article class="finding" id="f{i}">',
             f'<header><h3>{rich(f["title"])}</h3><span class="tag {e(sev)}">{SEVERITY_WORDS.get(sev, e(sev))}</span>'
             f'</header>']
    if meta:
        parts.append(f'<p class="meta">{"<br>".join(meta)}</p>')
    by_step: dict[int, list[dict]] = {}
    loose = []
    for s in f.get("evidence") or []:
        (by_step.setdefault(s["step"], []) if s.get("step") else loose).append(s)
    if f.get("steps"):
        parts.append('<ol class="steps">')
        for n, step in enumerate(f["steps"], 1):
            cls = ' class="failed"' if f.get("failed_step") == n else ""
            shots = by_step.get(n) or []
            parts.append(f'<li{cls}><span class="text">{rich(step)}</span>'
                         + (f'<div class="shots">{"".join(_shot(s) for s in shots)}</div>' if shots else "")
                         + "</li>")
        parts.append("</ol>")
    if f.get("expected") or f.get("actual"):
        parts.append('<div class="compare">'
                     f'<div><h4>Expected</h4><p>{rich(f.get("expected")) or "Not said"}</p></div>'
                     f'<div class="actual"><h4>Actual</h4><p>{rich(f.get("actual")) or "Not said"}</p></div></div>')
    if loose:
        parts.append(f'<div class="shots">{"".join(_shot(s) for s in loose)}</div>')
    if f.get("cause") or f.get("code"):
        code = []
        for c in f.get("code") or []:
            where = f"{c['repo']}/{c['file']}" if c.get("repo") else c["file"]
            where += f":{c['line']}" if c.get("line") else ""
            label = f"<code>{e(where)}</code>" + (f" at <code>{e(c['sha'][:12])}</code>" if c.get("sha") else "")
            code.append(f'<li><a href="{e(c["url"])}">{label}</a></li>' if c.get("url") else f"<li>{label}</li>")
        parts.append('<div class="cause"><h4>Suspected cause in the code, not confirmed</h4>'
                     + (f"<p>{rich(f['cause'])}</p>" if f.get("cause") else "")
                     + (f"<ul>{''.join(code)}</ul>" if code else "") + "</div>")
    parts.append("</article>")
    return "\n".join(parts)


def render_run(view: dict, project: str, tz: ZoneInfo) -> str:
    """One run's report as a page of its own (report.html in the run's folder)."""
    return _page(*run_body(view, project, tz))


def run_body(view: dict, project: str, tz: ZoneInfo) -> tuple[str, str]:
    """One run's report, its title and body: the verdict and the facts, the agent's summary, what it checked, then
    each problem with its steps, its screenshots at the step they show, expected against actual, and the suspected
    cause. The web console shows the body inside its own page."""
    findings = view.get("findings") or []
    outcome = view.get("outcome")
    # Who typed the command on the server is the runner's own user: only the schedule says something.
    by = ", on schedule" if view.get("source") == "schedule" else ""
    facts = [("Started", f"{_when(view.get('started_at') or view.get('created_at'), tz)}{by}"),
             ("Finished", _when(view.get("finished_at"), tz)),
             ("Took", _took(view.get("started_at"), view.get("finished_at")))]
    ins = view.get("instructions") or {}
    if ins:
        facts.append(("Instructions", f"{ins.get('path')}, version {str(ins.get('sha256'))[:12]}"))
    body = [f'<p class="crumb"><a href="../">All runs of {e(project)}</a></p>',
            f"<h1>Platform test, run {e(view['n'])}</h1>",
            f'<p class="verdict {e(outcome)}">{e(head_of(outcome, len(findings)))}</p>',
            '<dl class="facts">' + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in facts if v) + "</dl>"]
    if view.get("summary"):
        body.append(f'<p class="summary">{rich(view["summary"])}</p>')
    if view.get("blocked"):
        body.append(f'<div class="alert"><p><strong>Could not test:</strong> {rich(view["blocked"])}</p></div>')
    fatal = view.get("fatal") or []
    if fatal:
        body.append('<div class="alert"><p><strong>The run did not finish as it should.</strong> What is below is '
                    'what it reported before that.</p><ul>' + "".join(f"<li>{e(x)}</li>" for x in fatal)
                    + "</ul></div>")
    doubts = view.get("doubts") or []
    if doubts:
        body.append('<div class="alert note"><p><strong>Not a clean result:</strong></p><ul>'
                    + "".join(f"<li>{e(x)}</li>" for x in doubts) + "</ul></div>")
    tested = view.get("tested") or []
    body.append("<h2>What was checked</h2>")
    if tested:
        body.append('<ul class="checks">' + "".join(
            f'<li><span class="tag {e(t["result"])}">{RESULT_WORDS.get(t["result"], e(t["result"]))}</span>'
            f'<span>{rich(t["what"])}' + (f'<span class="note">{rich(t["note"])}</span>' if t.get("note") else "")
            + "</span></li>" for t in tested) + "</ul>")
    else:
        body.append('<p class="empty">The agent did not say what it checked.</p>')
    body.append("<h2>Problems</h2>")
    if findings:
        body += [_finding(i, f) for i, f in enumerate(findings, 1)]
    else:
        body.append('<p class="empty">None reported.</p>')
    problems = view.get("problems") or []
    if problems:
        body.append('<div class="alert note"><p><strong>Left out of this report</strong> (the agent\'s report did not '
                    'hold up there):</p><ul>' + "".join(f"<li>{e(p)}</li>" for p in problems) + "</ul></div>")
    repos = view.get("repos") or []
    if repos:
        lines = []
        for r in repos:
            if not r.get("sha"):
                lines.append(f"<li><code>{e(r['repo'])}</code>: no {e(r['default'])} on the code host</li>")
            else:
                note = "" if r.get("switch") else ", read with git show: other work was checked out"
                lines.append(f"<li><code>{e(r['repo'])}</code> {e(r['default'])} at <code>{e(r['sha'][:12])}</code>"
                             f"{note}</li>")
        body.append("<footer><p>Code the agent read to explain problems:</p><ul>" + "".join(lines) + "</ul></footer>")
    return f"Run {view['n']}: {head_of(outcome, len(findings))}", "\n".join(body)


def render_index(rows: list[dict], project: str, tz: ZoneInfo) -> str:
    """Every run, newest first."""
    body = [f"<h1>Platform tests of {e(project)}</h1>"]
    if not rows:
        body.append('<p class="empty">No runs yet. Start one on the server with <code>arceus-runner qa run</code>.</p>')
        return _page(f"Platform tests of {project}", "\n".join(body))
    trs = []
    for r in sorted(rows, key=lambda r: r["n"], reverse=True):
        if r["state"] != "done":
            result = "Queued" if r["state"] == "pending" else "Testing now" if r["state"] in ("preparing", "running") \
                else "Writing the report"
            result += ", stop asked" if r.get("stop_requested") else ""
        else:
            result = head_of(r["outcome"], r["findings"])
        run = f'<a href="{e(r["n"])}/">Run {e(r["n"])}</a>' if r.get("has_report") else f"Run {e(r['n'])}"
        summary = f'<span class="run-summary">{rich(r["summary"])}</span>' if r.get("summary") else ""
        by = "Schedule" if r["source"] == "schedule" else "Command"
        cls = e(r["outcome"]) if r["state"] == "done" else ""
        trs.append(f'<tr><td class="nowrap">{run}</td><td><span class="{cls}">{e(result)}</span>{summary}</td>'
                   f"<td class=\"hide-narrow nowrap\">{e(_when(r.get('started_at') or r.get('created_at'), tz))}</td>"
                   f"<td class=\"hide-narrow\">{by}</td></tr>")
    body.append('<table><thead><tr><th>Run</th><th>Result</th><th class="hide-narrow">Started</th>'
                '<th class="hide-narrow">How</th></tr></thead><tbody>' + "".join(trs) + "</tbody></table>")
    return _page(f"Platform tests of {project}", "\n".join(body))


# -- the server -----------------------------------------------------------------------------------------------------

_RUN = re.compile(r"^/(\d{1,9})/$")
_MEDIA = re.compile(r"^/(\d{1,9})/media/([0-9a-f]{16}\.[a-z0-9]{2,5})$")


def resolve(root: Path, path: str) -> tuple[Path, str] | None:
    """The file a request path means and its type, or None: only the list, a run's report and its media."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    if path == "/":
        return root / "index.html", "text/html; charset=utf-8"
    if m := _RUN.match(path):
        return root / m.group(1) / "report.html", "text/html; charset=utf-8"
    if m := _MEDIA.match(path):
        ext = Path(m.group(2)).suffix
        if ext not in media.TYPES:
            return None
        return root / m.group(1) / "media" / m.group(2), media.TYPES[ext][0]
    if _RUN.match(path + "/"):
        return None  # /12 without the slash: the handler sends it to /12/
    return None


class _Handler(BaseHTTPRequestHandler):
    root: Path
    index: Callable[[], str] | None = None
    server_version = "arceus-qa"
    sys_version = ""
    csp = ("default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; "
           "form-action 'none'")

    def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
        bare = self.path.split("?", 1)[0]
        if re.match(r"^/\d{1,9}$", bare):
            self.send_response(301)
            self.send_header("Location", bare + "/")
            self.end_headers()
            return
        if bare == "/" and self.index is not None:
            self._send(self.index().encode(), "text/html; charset=utf-8")
            return
        found = resolve(self.root, self.path)
        if found is None or found[0].is_symlink() or not found[0].is_file():
            self.send_error(404, "Not found")
            return
        self._send(found[0].read_bytes(), found[1])

    def _send(self, data: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if kind.startswith("text/html") else "max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", self.csp)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return  # who looked at a report is not the runner's news


def serve(root: Path, address: str, port: int, index: Callable[[], str] | None = None):
    """Start the reports server in a thread of its own. Returns the port it listens on and a function that stops it.
    `index` builds the list for `/` on each visit; without it, `/` is the index.html beside the runs."""
    root.mkdir(parents=True, exist_ok=True)
    handler = type("Handler", (_Handler,), {"root": root.resolve(), "index": staticmethod(index) if index else None})
    server = ThreadingHTTPServer((address, port), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="qa-reports", daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return server.server_address[1], stop
