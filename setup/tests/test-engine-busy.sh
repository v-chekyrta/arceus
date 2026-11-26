#!/bin/bash
# The engine is swapped, and restarted, only between tasks: setup waits for a `running` task whose attempt is live,
# and not for one whose session is gone (a reboot, a QA run stopped with `arceus-runner qa stop`), which would
# otherwise keep the engine from ever being updated. night-shift and runuser are stand-ins; no root needed.
#   bash setup/tests/test-engine-busy.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }

eval "$(sed -n '/^running_bulava_tasks() {/,/^}/p' "$HERE/../setup.sh")"
type running_bulava_tasks >/dev/null || { echo "FAIL running_bulava_tasks is not in setup.sh"; exit 1; }

RUN_USER=arceus RUN_HOME="$T/home"
mkdir -p "$RUN_HOME/.local/bin"
# runuser -u <user> -- <command…>: the command as it is, its environment kept for the stand-in to check.
runuser() { shift 3; "$@"; }
# night-shift task list / task show, from $T/tasks.json ({id: {state, attempt_live}}); a task in $T/broken cannot be shown.
cat > "$RUN_HOME/.local/bin/night-shift" <<'SH'
#!/bin/bash
[ "${SUPERVISOR_PROFILE:-}" = server ] || { echo "asked without the server profile" >&2; exit 9; }
case "$2" in
  list) jq -c '[to_entries[] | {task_id: .key, state: .value.state}]' "$T/tasks.json" ;;
  show) grep -qx "$4" "$T/broken" 2>/dev/null && exit 1
        jq -c --arg t "$4" '.[$t] | {task_id: $t, state, attempt_live}' "$T/tasks.json" ;;
esac
SH
chmod +x "$RUN_HOME/.local/bin/night-shift"; export T

echo '{"qa-4": {"state": "running", "attempt_live": false}, "AL-2": {"state": "waiting_review", "attempt_live": false}}' > "$T/tasks.json"
check "a running record whose session is gone does not hold the engine" '[ -z "$(running_bulava_tasks)" ]'

echo '{"qa-4": {"state": "running", "attempt_live": false}, "AL-7": {"state": "running", "attempt_live": true}}' > "$T/tasks.json"
check "a live attempt does" '[ "$(running_bulava_tasks)" = AL-7 ]'

echo '{"AL-8": {"state": "running", "attempt_live": false}}' > "$T/tasks.json"; echo AL-8 > "$T/broken"
check "and so does a running task that cannot be shown" '[ "$(running_bulava_tasks)" = AL-8 ]'

rm -f "$RUN_HOME/.local/bin/night-shift"
check "no engine installed yet: nothing runs" '[ -z "$(running_bulava_tasks)" ]'

[ "$fails" = 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
