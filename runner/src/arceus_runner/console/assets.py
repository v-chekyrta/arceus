"""The console's stylesheet and script, served from /static/ with a hash in the address.

The stylesheet starts from the report pages' own (qa_report.STYLE, arch_report.STYLE): the same colours, type and
cards, so a report inside the console looks as it does on its own. What follows are the console's parts: the bar at
the top, forms, the people list, status tiles.
"""

from __future__ import annotations

from arceus_runner import arch_report, qa_report

CONSOLE_CSS = """
[hidden] { display: none !important; }
body { min-height: 100vh; display: flex; flex-direction: column; }
main { width: 100%; max-width: 1040px; flex: 1 0 auto; }
.bar { background: var(--surface); border-bottom: 1px solid var(--line); }
.bar-in { max-width: 1040px; margin: 0 auto; padding: 0 16px; display: flex; align-items: center; gap: 4px 20px;
          flex-wrap: wrap; min-height: 56px; }
.brand { display: flex; align-items: baseline; gap: 8px; color: var(--ink); text-decoration: none; font-weight: 700;
         letter-spacing: -0.01em; padding: 14px 0; }
.brand span { font-weight: 500; color: var(--muted); font-size: 0.9rem; }
.sections { flex: 1 1 auto; }
.nav { display: flex; flex-wrap: wrap; gap: 2px; margin: 0; padding: 0; list-style: none; }
.nav a { display: block; padding: 6px 10px; border-radius: 6px; color: var(--muted); text-decoration: none;
         font-size: 0.925rem; font-weight: 500; }
.nav a:hover { color: var(--ink); background: var(--bg); }
.nav a[aria-current="page"] { color: var(--ink); background: var(--bg); }
.nav .count { display: inline-block; min-width: 1.3rem; margin-left: 4px; padding: 0 6px; border-radius: 999px;
              background: var(--blue); color: #fff; font-size: 0.75rem; line-height: 1.3rem; text-align: center; }
.who { display: flex; align-items: center; gap: 12px; font-size: 0.9rem; }
.who a { color: var(--muted); text-decoration: none; }
.who a:hover { color: var(--ink); }
.who form { margin: 0; }
.linkish { font: inherit; font-size: 0.9rem; color: var(--muted); background: none; border: 0; padding: 0;
           cursor: pointer; }
.linkish:hover { color: var(--ink); }
.meta { color: var(--muted); font-size: 0.925rem; }
.lede { color: var(--muted); margin: 4px 0 24px; max-width: 46rem; }
.notice { margin: 0 0 20px; padding: 10px 14px; border-radius: 8px; background: var(--good-soft); color: var(--ink); }
.notice.err { background: var(--bad-soft); }
.notice.warn { background: var(--warn-soft); }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 20px;
        margin: 0 0 16px; }
.card > :is(h2, h3):first-of-type { margin-top: 0; }
.narrow { max-width: 420px; margin: 8vh auto 0; }
.narrow h1 { margin-bottom: 4px; }
.form-row { margin: 0 0 14px; }
.form-row label, .label { display: block; font-size: 0.9rem; font-weight: 600; margin: 0 0 6px; }
.form-row .hint { display: block; color: var(--muted); font-size: 0.85rem; font-weight: 400; margin-top: 4px; }
input[type=text], input[type=email], input[type=password], input[type=number], input[type=url], select, textarea {
  display: block; width: 100%; padding: 8px 10px; font: inherit; color: var(--ink); background: var(--bg);
  border: 1px solid var(--line); border-radius: 6px; }
textarea { min-height: 6rem; resize: vertical; }
textarea.code { font: 0.875rem/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; min-height: 24rem;
                tab-size: 2; white-space: pre; overflow-wrap: normal; overflow-x: auto; }
input:focus-visible, select:focus-visible, textarea:focus-visible, button:focus-visible, .btn:focus-visible {
  outline: 2px solid var(--blue); outline-offset: 2px; }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; text-decoration: none;
       min-height: 38px; }
.btn.primary { color: #fff; background: var(--blue); border-color: var(--blue); }
.btn.primary:hover { filter: brightness(1.08); }
.btn.danger { color: var(--bad); }
.btn.danger:hover { border-color: var(--bad); }
.btn[disabled] { opacity: 0.55; cursor: not-allowed; }
.btn.block { width: 100%; }
.after { margin: 16px 0 0; font-size: 0.925rem; color: var(--muted); }
.checks-row { display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 0 0 12px; padding: 0; border: 0; }
.checks-row legend { font-size: 0.9rem; font-weight: 600; margin: 0 0 6px; padding: 0; }
.checks-row label { display: inline-flex; align-items: center; gap: 6px; font-size: 0.95rem; }
.role { display: inline-block; font-size: 0.8rem; font-weight: 600; padding: 1px 8px; border-radius: 999px;
        color: var(--blue); background: var(--blue-soft); white-space: nowrap; }
.role.admin { color: var(--bad); background: var(--bad-soft); }
.role.none { color: var(--quiet); background: var(--bg); }
.status-pending { color: var(--warn); font-weight: 600; }
.status-disabled { color: var(--quiet); font-weight: 600; }
.people td .note { display: block; color: var(--muted); font-size: 0.875rem; overflow-wrap: anywhere; }
.people td, .people th { vertical-align: middle; }
.roles-help { margin: 0; padding: 0; list-style: none; display: grid; gap: 10px; }
.roles-help li { display: grid; grid-template-columns: 7rem 1fr; gap: 12px; font-size: 0.925rem; }
.roles-help .everyone { color: var(--muted); }
.made { margin: 0 0 20px; padding: 14px 16px; border-radius: 8px; background: var(--blue-soft); }
.made p { margin: 0 0 8px; }
.made input { font: 0.85rem ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.spaced { margin-top: 32px; }
.split { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 20rem); gap: 24px; align-items: start; }
.inline-form { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; }
.inline-form .grow { flex: 1 1 14rem; }
.journal { list-style: none; margin: 0; padding: 0; font-size: 0.9rem; }
.journal li { padding: 8px 0; border-top: 1px solid var(--line); overflow-wrap: anywhere; }
.journal li:first-child { border-top: 0; }
.journal .at { color: var(--muted); display: block; font-size: 0.825rem; }
.foot { max-width: 1040px; margin: 0 auto; padding: 24px 16px 32px; color: var(--quiet); font-size: 0.825rem; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.table-wrap { overflow-x: auto; border-radius: 8px; }

/* home */
.headline { display: flex; gap: 10px; align-items: baseline; font-size: 1.35rem; font-weight: 650; line-height: 1.3;
            letter-spacing: -0.01em; margin: 4px 0 20px; overflow-wrap: anywhere; }
.headline .glyph { flex: none; width: 1.6rem; height: 1.6rem; border-radius: 50%; display: inline-grid;
                   place-items: center; font-size: 0.95rem; transform: translateY(0.1rem); }
.headline.good .glyph { color: var(--good); background: var(--good-soft); }
.headline.busy .glyph { color: var(--blue); background: var(--blue-soft); }
.headline.wait .glyph { color: var(--warn); background: var(--warn-soft); }
.headline.bad .glyph { color: var(--bad); background: var(--bad-soft); }
.headline.unknown .glyph { color: var(--quiet); background: var(--surface); border: 1px solid var(--line); }
.tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 0 0 8px; }
.tile { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; }
.tile h2 { font-size: 0.85rem; font-weight: 600; color: var(--muted); margin: 0 0 4px; }
.tile .value { font-size: 1.6rem; font-weight: 650; margin: 0; line-height: 1.25; }
.tile .note { margin: 4px 0 0; font-size: 0.85rem; color: var(--muted); overflow-wrap: anywhere; }
.value.good, .value-line.good { color: var(--good); } .value.bad { color: var(--bad); }
.value.unknown, .value-line.unknown { color: var(--quiet); }
.tile .value.good, .tile .value.bad, .tile .value.unknown { font-size: 1.15rem; padding-top: 6px; }
.value-line { margin: 0; font-weight: 600; }
.home-split { grid-template-columns: minmax(0, 1fr) minmax(0, 22rem); margin-top: 28px; }
.home-split > div > h2:first-child { margin-top: 0; }
.rows { list-style: none; margin: 0; padding: 0; background: var(--surface); border: 1px solid var(--line);
        border-radius: 8px; }
.rows li { display: grid; grid-template-columns: 11rem minmax(0, 1fr); gap: 12px; padding: 10px 16px;
           border-top: 1px solid var(--line); align-items: start; }
.rows li:first-child { border-top: 0; }
.rows .what { overflow-wrap: anywhere; }
.rows .note, .checklist .note, .links .note, .meters .note { display: block; color: var(--muted); font-size: 0.875rem; }
.tag.busy { color: var(--blue); background: var(--blue-soft); }
.tag.wait { color: var(--warn); background: var(--warn-soft); }
.tag.bad { color: var(--bad); background: var(--bad-soft); }
.tag.quiet { color: var(--quiet); background: var(--bg); }
.checklist { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.checklist li { overflow-wrap: anywhere; }
.meters { list-style: none; margin: 0; padding: 0; display: grid; gap: 14px; }
.meters li { display: grid; grid-template-columns: 1fr auto; gap: 4px 12px; align-items: baseline; }
.meter-name { font-size: 0.925rem; }
.meter-num { font-weight: 600; font-variant-numeric: tabular-nums; }
.meter { grid-column: 1 / -1; display: block; height: 8px; border-radius: 999px; background: var(--blue-soft);
         overflow: hidden; }
.meter .fill { display: block; height: 100%; border-radius: 999px; background: var(--blue); }
.meter.warn { background: var(--warn-soft); } .meter.warn .fill { background: var(--warn); }
.meter.bad { background: var(--bad-soft); } .meter.bad .fill { background: var(--bad); }
.meters .note { grid-column: 1 / -1; margin-top: -2px; }
.meter .w0 { width: 0%; }
.meter .w5 { width: 5%; }
.meter .w10 { width: 10%; }
.meter .w15 { width: 15%; }
.meter .w20 { width: 20%; }
.meter .w25 { width: 25%; }
.meter .w30 { width: 30%; }
.meter .w35 { width: 35%; }
.meter .w40 { width: 40%; }
.meter .w45 { width: 45%; }
.meter .w50 { width: 50%; }
.meter .w55 { width: 55%; }
.meter .w60 { width: 60%; }
.meter .w65 { width: 65%; }
.meter .w70 { width: 70%; }
.meter .w75 { width: 75%; }
.meter .w80 { width: 80%; }
.meter .w85 { width: 85%; }
.meter .w90 { width: 90%; }
.meter .w95 { width: 95%; }
.meter .w100 { width: 100%; }
.links { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
         gap: 12px; }
.links li { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 12px 16px; }
.links a { font-weight: 600; }
.dashboards { margin-top: 40px; }
.dashboards .lede { margin-bottom: 12px; }
.refreshed { margin: 16px 0 0; color: var(--quiet); font-size: 0.8rem; }
.problems { list-style: none; } .problems li { padding: 4px 0; }
@media (max-width: 900px) {
  .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .home-split { grid-template-columns: 1fr; }
  .links { grid-template-columns: 1fr; }
}
@media (max-width: 640px) {
  .rows li { grid-template-columns: 1fr; gap: 4px; }
  .headline { font-size: 1.15rem; }
}

/* runs, reports, files */
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 20px; }
.toolbar form { margin: 0; }
.inline-stop { display: inline; margin: 0 0 0 6px; }
.btn.small { min-height: 28px; padding: 2px 10px; font-size: 0.85rem; }
.report > .crumb:first-child { margin-top: 0; }
.file-split { grid-template-columns: minmax(0, 1fr) minmax(0, 20rem); }
pre.diff { margin: 0 0 20px; padding: 12px 0; background: var(--surface); border: 1px solid var(--line);
           border-radius: 8px; overflow-x: auto; font: 0.85rem/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
pre.diff span { display: block; padding: 0 14px; white-space: pre; }
pre.diff .add { background: var(--good-soft); } pre.diff .del { background: var(--bad-soft); }
pre.diff .ctx { color: var(--muted); }

/* settings */
.settings-groups { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.settings-groups .count-note { color: var(--quiet); font-size: 0.8rem; margin-top: 4px; }
.settings-form .group { border: 0; border-top: 1px solid var(--line); margin: 18px 0 0; padding: 14px 0 0; min-width: 0; }
.settings-form .group legend { padding: 0 8px 0 0; font-weight: 600; }
.settings-form .group .group { margin-left: 12px; }
.settings-form > .setting:first-of-type { margin-top: 0; }
.setting { margin: 14px 0 0; min-width: 0; }
.setting > label { display: block; font-size: 0.9rem; margin: 0 0 4px; }
.setting .kind { color: var(--quiet); font-size: 0.8rem; margin-left: 6px; }
.setting .hint, .group > .hint { display: block; color: var(--muted); font-size: 0.85rem; margin: 4px 0 0; overflow-wrap: anywhere; }
.setting.default input, .setting.default select, .setting.default textarea { color: var(--muted); }
.check { display: inline-flex; gap: 8px; align-items: center; }
textarea.code.small { min-height: 0; }
.form-end { padding: 14px 0 0; margin-top: 20px; border-top: 1px solid var(--line); align-items: center; }
pre.diff .hunk { color: var(--blue); }
.spaced-top { margin-top: 14px; }
@media (max-width: 760px) {
  .split { grid-template-columns: 1fr; }
  .roles-help li { grid-template-columns: 1fr; gap: 2px; }
  .settings-groups { grid-template-columns: 1fr; }
  .bar-in { gap: 0 12px; }
  .sections { order: 3; flex-basis: 100%; }
  .nav { margin: 0 -10px 6px; }
  .who { margin-left: auto; }
  .narrow { margin-top: 2vh; }
}
"""

CONSOLE_JS = """
'use strict';
// Copy buttons next to one-time links: the link is in a read-only field beside it.
document.addEventListener('click', function (ev) {
  var b = ev.target.closest('[data-copy]');
  if (!b) return;
  var field = document.getElementById(b.getAttribute('data-copy'));
  if (!field) return;
  field.select();
  var done = function () { b.textContent = 'Copied'; setTimeout(function () { b.textContent = 'Copy'; }, 1600); };
  if (navigator.clipboard) navigator.clipboard.writeText(field.value).then(done, function () {});
  else { document.execCommand('copy'); done(); }
});
// Buttons that ask first: the question is in data-confirm.
document.addEventListener('submit', function (ev) {
  var b = ev.submitter;
  var q = (b && b.getAttribute('data-confirm')) || ev.target.getAttribute('data-confirm');
  if (q && !window.confirm(q)) ev.preventDefault();
});
// Parts of a page that follow the server: data-refresh="<url>" every data-every seconds (default 10), replaced
// whole by what the URL answers. Stops while the tab is hidden.
(function () {
  var parts = document.querySelectorAll('[data-refresh]');
  parts.forEach(function (el) {
    var every = Math.max(3, parseInt(el.getAttribute('data-every') || '10', 10)) * 1000;
    var tick = function () {
      if (document.hidden) return;
      fetch(el.getAttribute('data-refresh'), {credentials: 'same-origin', headers: {'Accept': 'text/html'}})
        .then(function (r) { if (r.status === 401) { location.reload(); return null; } return r.ok ? r.text() : null; })
        .then(function (html) {
          if (html === null) return;
          var keep = el.querySelector('[data-keep-scroll]');
          var top = keep ? keep.scrollTop : 0;
          var stuck = keep ? keep.scrollHeight - keep.scrollTop - keep.clientHeight < 24 : false;
          el.innerHTML = html;
          var again = el.querySelector('[data-keep-scroll]');
          if (again) again.scrollTop = stuck ? again.scrollHeight : top;
          if (el.querySelector('[data-final]')) { clearInterval(timer); }
        })
        .catch(function () {});
    };
    var timer = setInterval(tick, every);
  });
})();
"""


def build() -> dict[str, tuple[bytes, str]]:
    css = qa_report.STYLE + arch_report.STYLE + CONSOLE_CSS
    return {"console.css": (css.encode(), "text/css; charset=utf-8"),
            "console.js": (CONSOLE_JS.encode(), "text/javascript; charset=utf-8")}
