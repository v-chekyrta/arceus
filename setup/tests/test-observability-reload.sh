#!/bin/bash
# Prometheus, Loki and Alloy read their config file only when they start. An update must hand them a new file
# (so a running container is seen to hold the old one and is recreated), and a container whose file was
# changed in place after it started must be seen as stale too: it shows the new text but never read it.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d /tmp/arceus-obs-reload.XXXXXX)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }

# The functions as setup.sh has them; the processes' roots are under $T/proc instead of /proc.
eval "$(sed -n '/^OBS_FILE_MOUNTS=(/,/)$/p; /^obs_project() {/p; /^obs_metrics_port() {/p;
  /^obs_render() {/,/^}/p; /^obs_install_files() {/,/^}/p; /^obs_stale_services() {/,/^}/p' "$HERE/../setup.sh" \
  | sed "s#\"/proc/#\"$T/proc/#g")"
type obs_install_files obs_stale_services >/dev/null || { echo "FAIL the functions are not in setup.sh"; exit 1; }

# GNU stat and date on the VM; on a Mac the same answers from Python.
stat -c %Y / >/dev/null 2>&1 || stat() { python3 -c 'import os,sys; print(int(os.stat(sys.argv[1]).st_mtime))' "$3"; }
date -d @0 >/dev/null 2>&1 || date() {
  python3 -c 'import datetime,re,sys
s = re.sub(r"\.\d+", "", sys.argv[1]).replace("Z", "+00:00")
print(int(datetime.datetime.fromisoformat(s).timestamp()))' "$2"
}
inode() { python3 -c 'import os,sys; print(os.stat(sys.argv[1]).st_ino)' "$1"; }
mtime() { python3 -c 'import os,sys; print(int(os.stat(sys.argv[1]).st_mtime))' "$1"; }
set_mtime() { python3 -c 'import os,sys; os.utime(sys.argv[1], (int(sys.argv[2]), int(sys.argv[2])))' "$1" "$2"; }

mkdir -p "$T/venv/bin"; ln -s "$(command -v python3)" "$T/venv/bin/python"
RUNNER_OPT="$T"; REPO_DIR="$HERE/../.."
PROJECT=""; PORT=9464
console_get() { case "$1" in CONSOLE_PROJECT) echo "$PROJECT" ;; CONSOLE_METRICS_PORT) echo "$PORT" ;; esac; }

# --- the update hands over new files, and leaves unchanged ones alone ---
OBS_DIR="$T/obs"; mkdir -p "$OBS_DIR/secrets"
echo keep > "$OBS_DIR/.env"; echo keep > "$OBS_DIR/secrets/grafana-admin"
obs_install_files
check "installed before the console names a project: the repository's own prometheus.yml" \
  "cmp -s '$REPO_DIR/observability/prometheus.yml' '$OBS_DIR/prometheus.yml'"
prom_before="$(inode "$OBS_DIR/prometheus.yml")"
loki_before="$(inode "$OBS_DIR/loki.yaml")"; set_mtime "$OBS_DIR/loki.yaml" 1000000000

PROJECT=contoso
obs_install_files
check "once it does, prometheus.yml carries the project's label" \
  "grep -qF 'labels: {project: \"contoso\"}' '$OBS_DIR/prometheus.yml'"
check "as a new file: the running Prometheus keeps the old one and is seen to hold it" \
  "[ '$(inode "$OBS_DIR/prometheus.yml")' != '$prom_before' ]"
check "an unchanged file is not touched: neither replaced nor its time changed" \
  "[ '$(inode "$OBS_DIR/loki.yaml")' = '$loki_before' ] && [ '$(mtime "$OBS_DIR/loki.yaml")' = 1000000000 ]"
prom_now="$(inode "$OBS_DIR/prometheus.yml")"
obs_install_files
check "a second run with nothing new replaces nothing" "[ '$(inode "$OBS_DIR/prometheus.yml")' = '$prom_now' ]"
check "the install's own .env and secrets stay" \
  "[ \"\$(cat '$OBS_DIR/.env')\" = keep ] && [ \"\$(cat '$OBS_DIR/secrets/grafana-admin')\" = keep ]"

# --- which running service has not read its current file ---
STARTED=2026-11-16T13:46:04.097012372Z; started_s="$(date -d "$STARTED" +%s)"
PIDS="prometheus=4242 loki=4243 alloy=0"   # alloy: not running
docker() {
  local a="$*" s
  case "$a" in
    *" ps -q "*) echo "id-${a##* }" ;;
    "inspect -f {{.State.Pid}} id-"*) s="${a##*id-}"; s="${PIDS#*"$s"=}"; echo "${s%% *}" ;;
    "inspect -f {{.State.StartedAt}} id-"*) echo "$STARTED" ;;
  esac
}
for m in "${OBS_FILE_MOUNTS[@]}"; do
  inside="${m#*:}"; inside="${inside%:*}"; file="${m##*:}"
  for pid in 4242 4243; do
    mkdir -p "$(dirname "$T/proc/$pid/root$inside")"; cp "$OBS_DIR/$file" "$T/proc/$pid/root$inside"
    set_mtime "$T/proc/$pid/root$inside" $((started_s - 3600))
  done
done
check "every running service read the file it holds: none is stale" "[ -z \"\$(obs_stale_services)\" ]"

echo old > "$T/proc/4242/root/etc/prometheus/prometheus.yml"; set_mtime "$T/proc/4242/root/etc/prometheus/prometheus.yml" $((started_s - 3600))
check "a container that holds an older file is stale" "[ \"\$(obs_stale_services)\" = prometheus ]"

cp "$OBS_DIR/prometheus.yml" "$T/proc/4242/root/etc/prometheus/prometheus.yml"
set_mtime "$T/proc/4242/root/etc/prometheus/prometheus.yml" $((started_s + 2 * 86400))
check "the same text, written in place after the service started, is stale: it was never read" \
  "[ \"\$(obs_stale_services)\" = prometheus ]"

set_mtime "$T/proc/4242/root/etc/prometheus/prometheus.yml" "$started_s"
check "written the second it started, it is taken as read" "[ -z \"\$(obs_stale_services)\" ]"

[ "$fails" = 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
