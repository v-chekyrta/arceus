#!/bin/bash
# Grafana follows the console's project: setup.sh's obs_render makes the repository's prometheus.yml scrape that
# project's runner with its `project` label, and every dashboard query of the runner's metrics and events ask for
# that label, so another project's history in the same Prometheus and Loki is never shown as this one's.
# The functions are taken from setup.sh as they are. No root, no Docker.
#   bash setup/tests/test-observability-project.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d /tmp/arceus-obs-test.XXXXXX)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }
die() { echo "die: $*" >&2; exit 1; }

eval "$(sed -n '/^obs_project() {/p; /^obs_metrics_port() {/p; /^obs_render() {/,/^}/p; /^obs_rendered_ok() {/,/^}/p' \
  "$HERE/../setup.sh")"
type obs_render obs_rendered_ok >/dev/null || { echo "FAIL the functions are not in setup.sh"; exit 1; }

mkdir -p "$T/venv/bin"; ln -s "$(command -v python3)" "$T/venv/bin/python"
RUNNER_OPT="$T"; REPO_DIR="$HERE/../.."; CONSOLE_DATA="$T/data"
PROJECT=contoso; PORT=9465
console_get() { case "$1" in CONSOLE_PROJECT) echo "$PROJECT" ;; CONSOLE_METRICS_PORT) echo "$PORT" ;; esac; }

OBS_DIR="$T/obs"; mkdir -p "$OBS_DIR"
rsync -a --exclude build_dashboards.py "$REPO_DIR/observability/" "$OBS_DIR/"
obs_render "$OBS_DIR"

check "the runner's scrape target is the project's metrics port, with its label" \
  "grep -qF 'static_configs: [{targets: [\"127.0.0.1:9465\"], labels: {project: \"contoso\"}}]' '$OBS_DIR/prometheus.yml'"
check "the machine's own metrics stay unlabelled" "grep -qF 'targets: [\"127.0.0.1:9100\"]}]' '$OBS_DIR/prometheus.yml'"

unlabelled="$(python3 - "$OBS_DIR/grafana/dashboards" <<'PY'
import json, pathlib, re, sys
out = []
def walk(n):
    if isinstance(n, dict):
        for k, v in n.items():
            if k == "expr" and isinstance(v, str):
                for m in re.finditer(r'\b(runner_[a-z0-9_]+)(\{[^}]*\})?', v):
                    if 'project="contoso"' not in (m.group(2) or ""):
                        out.append(v[:80])
                for m in re.finditer(r'\{source=', v):
                    out.append(v[:80])
            else:
                walk(v)
    elif isinstance(n, list):
        for v in n:
            walk(v)
for p in pathlib.Path(sys.argv[1]).glob("*.json"):
    walk(json.loads(p.read_text()))
print("\n".join(out))
PY
)"
check "every query of the runner's metrics and events asks for the project" "[ -z \"\$unlabelled\" ]"
check "a labelled query keeps its own labels" \
  "grep -qF 'runner_service_up{project=\\\"contoso\\\",role=' $OBS_DIR/grafana/dashboards/*.json"
check "Loki streams ask for the project before their own labels" \
  "grep -qF '{project=\\\"contoso\\\", source=\\\"runner\\\", event=' $OBS_DIR/grafana/dashboards/*.json"
check "node queries are not touched" "! grep -q 'node_[a-z_]*{project=' $OBS_DIR/grafana/dashboards/*.json"
check "the check sees the installed copy as current" "obs_rendered_ok"

PORT=9464
check "and as stale once the project's port changes" "! obs_rendered_ok"
PORT=9465
cp "$REPO_DIR/observability/prometheus.yml" "$OBS_DIR/prometheus.yml"
check "and as stale when the repository's unrendered file is back" "! obs_rendered_ok"

PROJECT=""
mkdir -p "$T/raw"; rsync -a --exclude build_dashboards.py "$REPO_DIR/observability/" "$T/raw/"
obs_render "$T/raw"
check "without a readable config.yaml the repository's files stay as they are" \
  "diff -rq '$REPO_DIR/observability/grafana/dashboards' '$T/raw/grafana/dashboards' >/dev/null && cmp -s '$REPO_DIR/observability/prometheus.yml' '$T/raw/prometheus.yml'"

[ "$fails" = 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
