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
.bar-in { max-width: 1040px; margin: 0 auto; padding: 0 16px; display: flex; align-items: center; gap: 4px 16px;
          flex-wrap: wrap; min-height: 56px; }
.brand { display: flex; align-items: baseline; gap: 8px; color: var(--ink); text-decoration: none; font-weight: 700;
         letter-spacing: -0.01em; padding: 14px 0; }
.brand span { font-weight: 500; color: var(--muted); font-size: 0.9rem; min-width: 0; overflow: hidden;
              text-overflow: ellipsis; white-space: nowrap; }
.sections { flex: 0 1 auto; }
.nav { display: flex; flex-wrap: wrap; gap: 2px; margin: 0; padding: 0; list-style: none; }
.nav a { display: block; padding: 6px 8px; border-radius: 6px; color: var(--muted); text-decoration: none;
         font-size: 0.925rem; font-weight: 500; }
.nav a:hover { color: var(--ink); background: var(--bg); }
.nav a[aria-current="page"] { color: var(--ink); background: var(--bg); }
.external { margin-left: 3px; font-size: 0.8em; opacity: 0.75; }
.nav .count { display: inline-block; min-width: 1.3rem; margin-left: 4px; padding: 0 6px; border-radius: 999px;
              background: var(--blue); color: #fff; font-size: 0.75rem; line-height: 1.3rem; text-align: center; }
/* The name and Sign out keep to the right of the bar. When the sections do not fit beside them (an admin's full
   list, a long name), the script marks the bar .stack: the name stays on the brand's line and the sections take a
   line of their own, as on a narrow screen. On that first line the project and the name give way (cut with an
   ellipsis) so that "Arceus" and Sign out both stay whole, side by side, at any width. */
.who { display: flex; align-items: center; gap: 12px; font-size: 0.9rem; margin-left: auto; flex: 0 1 auto;
       min-width: 0; white-space: nowrap; }
.who a { color: var(--muted); text-decoration: none; max-width: 14rem; overflow: hidden; text-overflow: ellipsis; }
.who form { flex: none; }
.bar-in.stack { gap: 0 12px; }
.bar-in.stack .brand { flex: 1 1 0; min-width: 0; }
.bar-in.stack .who { max-width: calc(100% - 7rem); }
.bar-in.stack .sections { order: 3; flex-basis: 100%; }
.bar-in.stack .nav { margin: 0 -8px 6px; }
.who a:hover { color: var(--ink); }
.who form { margin: 0; }
.linkish { font: inherit; font-size: 0.9rem; color: var(--muted); background: none; border: 0; padding: 0;
           cursor: pointer; }
.linkish:hover { color: var(--ink); }
.meta { color: var(--muted); font-size: 0.925rem; }
.lede { color: var(--muted); margin: 4px 0 24px; max-width: 46rem; }
.notice { margin: 0 0 20px; padding: 10px 14px; border-radius: 8px; background: var(--good-soft); color: var(--ink);
          overflow-wrap: anywhere; }
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
.value-line { margin: 0; font-weight: 600; overflow-wrap: anywhere; }
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
.file-split .lit { overflow-wrap: anywhere; }
pre.diff { margin: 0 0 20px; padding: 12px 0; background: var(--surface); border: 1px solid var(--line);
           border-radius: 8px; overflow-x: auto; font: 0.85rem/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
pre.diff span { display: block; padding: 0 14px; white-space: pre; }
pre.diff .add { background: var(--good-soft); } pre.diff .del { background: var(--bad-soft); }
pre.diff .ctx { color: var(--muted); }

/* test scenarios: the list on Test runs and a scenario's form */
.section-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 16px;
                margin: 0 0 12px; }
.section-head h2 { margin: 0; }
.scenario-list { list-style: none; margin: 0; padding: 0; background: var(--surface); border: 1px solid var(--line);
                 border-radius: 8px; }
.scenario-list li { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
                    gap: 8px 16px; padding: 12px 16px; border-top: 1px solid var(--line); }
.scenario-list li:first-child { border-top: 0; }
.scenario-what { min-width: 0; flex: 1 1 16rem; }
.scenario-name { font-weight: 600; overflow-wrap: anywhere; }
.scenario-what .note { display: block; color: var(--muted); font-size: 0.875rem; }
.scenario-do { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.scenario-do form { margin: 0; }
.scenario-do a.tag { text-decoration: none; }
.scenario-tz { margin: 8px 0 0; font-size: 0.85rem; }
.runs-h { margin-top: 36px; }
.scenario-form { max-width: 46rem; }
.scenario-form fieldset.schedule { border: 0; border-top: 1px solid var(--line); margin: 6px 0 14px;
                                   padding: 14px 0 0; min-width: 0; }
.scenario-form fieldset.schedule legend { padding: 0 8px 0 0; }
.scenario-form .note { color: var(--muted); font-size: 0.875rem; margin: 10px 0 0; }
.day-picks { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0 14px; }
.scenario-form .schedule-lede { margin: 0 0 4px; }
.at-pick { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.schedule-says { margin: 12px 0 0; padding: 8px 12px; border-radius: 6px; background: var(--bg); font-size: 0.925rem;
                 font-weight: 600; }
.schedule-says.good { color: var(--good); background: var(--good-soft); }
.schedule-says.warn { color: var(--warn); background: var(--warn-soft); }
.day { position: relative; }
.day input { position: absolute; opacity: 0; inset: 0; margin: 0; cursor: pointer; }
.day span { display: inline-flex; align-items: center; justify-content: center; min-width: 3.25rem; min-height: 38px;
            padding: 0 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg);
            color: var(--muted); font-size: 0.925rem; font-weight: 500; user-select: none; }
.day input:checked + span { color: #fff; background: var(--blue); border-color: var(--blue); }
.day input:focus-visible + span { outline: 2px solid var(--blue); outline-offset: 2px; }
.at-row input[type=time] { display: block; width: 9rem; padding: 8px 10px; font: inherit; color: var(--ink);
                           background: var(--bg); border: 1px solid var(--line); border-radius: 6px; }
.at-row input[type=time]:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
@media (forced-colors: active) {
  .day input { position: static; opacity: 1; }
  .day span { border: 0; min-width: 0; padding: 0 0 0 4px; }
}

/* a task's page and a run's before its report */
.task-title { overflow-wrap: anywhere; }
.task-title span { font-weight: 500; color: var(--muted); }
.task-meta { margin: 0 0 20px; overflow-wrap: anywhere; }
.lede.why { margin-top: -12px; }
.task-head { margin-bottom: 4px; }
.task-facts { margin-top: 16px; }
.stale strong { font-weight: 600; }
.plan-counts { display: flex; flex-wrap: wrap; gap: 6px; margin: 14px 0 10px; }
.facts .sub { display: block; color: var(--muted); font-size: 0.85rem; }
.facts .value-line { font-weight: 600; }
.value-line.bad { color: var(--bad); }
.task-split { grid-template-columns: minmax(0, 1fr) minmax(0, 22rem); gap: 0 24px; }
.task-split .note, .card > .note { color: var(--muted); font-size: 0.875rem; }
.question-text { white-space: pre-wrap; overflow-wrap: anywhere; padding: 14px 16px; border-radius: 6px;
                 background: var(--bg); border: 1px solid var(--line); }
.question .options { margin: 12px 0 0; padding-left: 24px; overflow-wrap: anywhere; }
.question .answer-here { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; margin: 16px 0 0; }
.question .answer-here .note { color: var(--muted); font-size: 0.875rem; }
.mr-rows .tag.ok { color: var(--good); background: var(--good-soft); }
.branch { margin: 10px 0 0; color: var(--muted); font-size: 0.9rem; overflow-wrap: anywhere; }
.review h3 { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: baseline; margin: 0 0 12px; }
.review .stale { margin: 0 0 12px; }

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
/* The settings' choices draw their own arrow: the browser's sits against the border. It is a background, so a click
   on it is a click on the select. */
.setting select { appearance: none; -webkit-appearance: none; padding-right: 36px; text-overflow: ellipsis;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2.5 4.5 6 8l3.5-3.5' fill='none' stroke='%235d6576' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 10px center; background-size: 12px 12px; }
.setting select:disabled { opacity: 0.6; cursor: not-allowed; }
@media (prefers-color-scheme: dark) {
  .setting select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2.5 4.5 6 8l3.5-3.5' fill='none' stroke='%23a2a9b6' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  }
}
@media (forced-colors: active) {
  .setting select { appearance: auto; -webkit-appearance: auto; background-image: none; padding-right: 10px; }
}
.check { display: inline-flex; gap: 8px; align-items: center; }
textarea.code.small { min-height: 0; }
.form-end { padding: 14px 0 0; margin-top: 20px; border-top: 1px solid var(--line); align-items: center; }
pre.diff .hunk { color: var(--blue); }
.spaced-top { margin-top: 14px; }

/* operations */
.ops-tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.ops-links { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.muted-title { font-weight: 600; color: var(--quiet); }
.toolbar.spaced { margin-top: 24px; }
pre.oplog { margin: 0; padding: 12px 14px; max-height: 32rem; overflow: auto; background: var(--surface);
            border: 1px solid var(--line); border-radius: 8px; white-space: pre-wrap; overflow-wrap: anywhere;
            font: 0.825rem/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
/* the assistant */
.assistant-split { grid-template-columns: minmax(0, 1fr) minmax(0, 24rem); margin-top: 8px; }
.assistant-split h2:first-child { margin-top: 0; }
.assistant-split .card p:last-child { margin-bottom: 0; }
.assistant-split .card > :first-child { margin-top: 0; }
.applied { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; }
.applied p { margin: 0; flex: 1 1 18rem; }
.applied form { margin: 0; }
.keys { list-style: none; margin: 0 0 12px; padding: 0; display: grid; gap: 10px; }
.key { padding: 14px 16px; margin: 0; }
.key-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.key .note { margin: 4px 0 10px; color: var(--muted); font-size: 0.875rem; overflow-wrap: anywhere; }
.key .inline-form input { min-width: 0; }
.rows .label { margin: 0; color: var(--muted); }

@media (max-width: 760px) {
  .split, .task-split { grid-template-columns: 1fr; }
  .roles-help li { grid-template-columns: 1fr; gap: 2px; }
  .settings-groups, .ops-links { grid-template-columns: 1fr; }
  .bar-in { gap: 0 12px; }
  .brand { flex: 1 1 0; min-width: 0; }
  .who { max-width: calc(100% - 7rem); }
  .sections { order: 3; flex-basis: 100%; }
  .nav { margin: 0 -8px 6px; }
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
// The bar: the sections in one line with the name and Sign out at the right when they fit, otherwise the sections
// on a line of their own (.stack). Checked again whenever the bar changes width (window, zoom).
(function () {
  var bar = document.querySelector('.bar-in');
  var brand = bar && bar.querySelector('.brand');
  var who = bar && bar.querySelector('.who');
  var sections = bar && bar.querySelector('.sections');
  var link = sections && sections.querySelector('.nav a');
  if (!brand || !who || !link) return;
  var width = -1;
  var fit = function () {
    if (bar.clientWidth === width) return;  // only a change of width can change the answer
    width = bar.clientWidth;
    bar.classList.remove('stack');
    var line = brand.getBoundingClientRect().bottom - 1;
    if (who.getBoundingClientRect().top >= line || sections.getBoundingClientRect().top >= line ||
        sections.offsetHeight > link.offsetHeight * 1.5) bar.classList.add('stack');
  };
  fit();
  if (window.ResizeObserver) new ResizeObserver(fit).observe(bar);
  else window.addEventListener('resize', fit);
})();
// Buttons that ask first: the question is in data-confirm.
document.addEventListener('submit', function (ev) {
  var b = ev.submitter;
  var q = (b && b.getAttribute('data-confirm')) || ev.target.getAttribute('data-confirm');
  if (q && !window.confirm(q)) ev.preventDefault();
});
// A test scenario's form: the line under the schedule says what the days and the time picked will do, as the server
// will keep it (qa_scenarios.clean): both, it runs by itself; neither, only by hand; one without the other is said to
// be missing its pair. "Clear schedule" empties both. Without the script the server says the same after saving.
(function () {
  var form = document.querySelector('.scenario-form');
  var says = form && form.querySelector('[data-schedule-says]');
  if (!says) return;
  var at = form.querySelector('input[name="at"]');
  var names = {mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun'};
  var boxes = Array.prototype.slice.call(form.querySelectorAll('input[name="day"]'));
  function show() {
    var days = boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.value; });
    var time = at.value;
    var text, tone = '';
    if (days.length && time) {
      var all = days.join(',');
      var when = days.length === 7 ? 'Every day' : all === 'mon,tue,wed,thu,fri' ? 'Weekdays' :
        all === 'sat,sun' ? 'Weekends' : days.map(function (d) { return names[d]; }).join(', ');
      text = 'Runs by itself: ' + when + ' at ' + time + '.';
      tone = 'good';
    } else if (!days.length && !time) {
      text = 'Runs only when someone presses "Run now".';
    } else {
      text = days.length ? 'Set the time too, or clear the days.' : 'Pick the days too, or clear the time.';
      tone = 'warn';
    }
    says.textContent = text;
    says.className = 'schedule-says' + (tone ? ' ' + tone : '');
  }
  form.addEventListener('change', show);
  form.addEventListener('input', show);
  var clear = form.querySelector('[data-clear-schedule]');
  if (clear) {
    clear.hidden = false;
    clear.addEventListener('click', function () {
      boxes.forEach(function (b) { b.checked = false; });
      at.value = '';
      show();
    });
  }
  show();
})();
// Following the server: follow(url, every, apply) asks url every `every` ms and hands each answer to apply(html), which
// puts it on the page and says whether it was the last (data-final). Stops while the tab is hidden; back in a shown tab
// it asks at once. One request at a time: one that has not answered within a round is given up, so a connection that
// hangs never piles requests up and an older answer never lands after a newer one. When the page has a [data-lost]
// notice, it shows once nothing has been answered for two rounds, whether the requests failed or hang, with the time
// of the last answer in [data-lost-at], and hides on the next answer; back in a hidden tab the server gets two rounds
// again before the notice, which still names the time of the last real answer. After the last answer it asks no more.
(function () {
var follow = (function () {
  var lost = document.querySelector('[data-lost]');
  var lostAt = document.querySelector('[data-lost-at]');
  return function (url, every, apply) {
    var answered = Date.now();  // the last answer; only an answer moves it
    var resumed = 0;            // when the tab was shown again: the two rounds count from then too
    var pending = null, over = false, timer, watcher;
    var watch = function () {
      if (lost && !over && Date.now() - Math.max(answered, resumed) > 2 * every) {
        if (lostAt) lostAt.textContent = new Date(answered).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
        lost.hidden = false;
      }
    };
    var shown = function () {
      if (document.hidden) return;
      resumed = Date.now();
      tick();
    };
    var stop = function () {
      over = true;
      clearInterval(timer); clearInterval(watcher);
      document.removeEventListener('visibilitychange', shown);
    };
    var tick = function () {
      if (over || document.hidden) return;
      watch();
      if (pending) return;
      var ctl = window.AbortController ? new AbortController() : null;
      pending = setTimeout(function () { if (ctl) ctl.abort(); }, every);
      fetch(url, {credentials: 'same-origin', headers: {'Accept': 'text/html'}, signal: ctl ? ctl.signal : undefined})
        .then(function (r) { if (r.status === 401) { location.reload(); return null; } return r.ok ? r.text() : null; })
        .then(function (html) {
          if (html === null || over) return;
          answered = Date.now();
          if (lost) lost.hidden = true;
          if (apply(html)) stop();
        })
        .catch(function () {})
        .then(function () { clearTimeout(pending); pending = null; watch(); });
    };
    timer = setInterval(tick, every);
    // The notice does not wait for a request to end: a hanging one is noticed while it hangs.
    watcher = setInterval(function () { if (!document.hidden) watch(); }, 1000);
    document.addEventListener('visibilitychange', shown);
  };
})();
var roundOf = function (el) { return Math.max(3, parseInt(el.getAttribute('data-every') || '10', 10)) * 1000; };
// Parts of a page that follow the server: data-refresh="<url>" every data-every seconds (default 10), replaced
// whole by what the URL answers. When the page has a [data-announce] live region, a screen reader hears the part's
// headline ([data-reason]) only when it changes, never the part every round.
(function () {
  var announce = document.querySelector('[data-announce]');
  var reasonOf = function (el) { var h = el.querySelector('[data-reason]'); return h ? h.getAttribute('data-reason') : ''; };
  document.querySelectorAll('[data-refresh]').forEach(function (el) {
    follow(el.getAttribute('data-refresh'), roundOf(el), function (html) {
      var before = reasonOf(el);
      var keep = el.querySelector('[data-keep-scroll]');
      var top = keep ? keep.scrollTop : 0;
      var stuck = keep ? keep.scrollHeight - keep.scrollTop - keep.clientHeight < 24 : false;
      el.innerHTML = html;
      var again = el.querySelector('[data-keep-scroll]');
      if (again) again.scrollTop = stuck ? again.scrollHeight : top;
      if (announce && reasonOf(el) !== before) {
        var head = el.querySelector('[data-reason]');
        announce.textContent = head ? head.textContent.trim() : '';
      }
      return !!el.querySelector('[data-final]');
    });
  });
})();
// A page of parts that follow the server together: data-refresh-parts="<url>" every data-every seconds. The URL
// answers with every part ([data-part="name"]) in the page's order; a part is replaced only when its HTML changed, so
// one that did not (a question still being read) keeps its text, selection and scroll. A part new to the page goes
// where the server puts it; one that goes is left empty by the server itself. The last answer carries data-final.
(function () {
  document.querySelectorAll('[data-refresh-parts]').forEach(function (root) {
    follow(root.getAttribute('data-refresh-parts'), roundOf(root), function (html) {
      var fresh = document.createElement('template');
      fresh.innerHTML = html;
      var last = !!fresh.content.querySelector('[data-final]');  // before the parts move out of the answer
      var prev = null;
      Array.prototype.forEach.call(fresh.content.querySelectorAll('[data-part]'), function (part) {
        var here = root.querySelector('[data-part="' + part.getAttribute('data-part') + '"]');
        if (!here) {
          if (prev) prev.after(part); else root.prepend(part);
          here = part;
        } else if (here.innerHTML.trim() !== part.innerHTML.trim()) {
          here.replaceWith(part);
          here = part;
        }
        prev = here;
      });
      return last;
    });
  });
})();
})();
"""


def build() -> dict[str, tuple[bytes, str]]:
    css = qa_report.STYLE + arch_report.STYLE + CONSOLE_CSS
    return {"console.css": (css.encode(), "text/css; charset=utf-8"),
            "console.js": (CONSOLE_JS.encode(), "text/javascript; charset=utf-8")}
