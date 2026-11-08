"""The console's stylesheet and script, served from /static/ with a hash in the address.

The stylesheet starts from the report pages' own (qa_report.STYLE, arch_report.STYLE): the same colours, type and
cards, so a report inside the console looks as it does on its own. What follows are the console's parts: the bar at
the top, forms, the people list, status tiles.
"""

from __future__ import annotations

from arceus_runner import arch_report, qa_report

CONSOLE_CSS = """
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
@media (max-width: 760px) {
  .split { grid-template-columns: 1fr; }
  .roles-help li { grid-template-columns: 1fr; gap: 2px; }
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
