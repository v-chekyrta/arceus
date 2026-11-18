#!/bin/bash
# The assistant phase: its units, its tokens' file, the old bot it refuses to start beside, and its route in nginx.
# The functions are taken from setup.sh as they are; systemd and config.yaml's side (arceus-runner console env)
# are stand-ins that record what was asked of them. No root needed.
#   bash setup/tests/test-assistant.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d /tmp/arceus-assistant-test.XXXXXX)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }

eval "$(sed -n '/^put_file() {/,/^}/p; /^same_file() {/,/^}/p' "$HERE/../setup.sh")"
eval "$(sed -n '/^ASSISTANT_OPT=/,/^# Grafana, Prometheus/p' "$HERE/../setup.sh")"
eval "$(sed -n '/^console_listen() {/,/^}/p' "$HERE/../setup.sh")"
eval "$(sed -n '/^console_nginx_conf() {/,/^CONF$/{p;/^CONF$/{n;p;};}' "$HERE/../setup.sh")"
type check_assistant apply_assistant assistant_unit_text legacy_assistant_units console_nginx_conf >/dev/null \
  || { echo "FAIL the functions are not in setup.sh"; exit 1; }

# GNU stat's -c on a Mac too (put_file and the checks ask for '%a %U' and '%a %U:%G').
if ! stat -c %a / >/dev/null 2>&1; then
  stat() {
    local fmt="$2"; shift 2
    fmt="${fmt//%a/%Lp}"; fmt="${fmt//%U/%Su}"; fmt="${fmt//%G/%Sg}"; fmt="${fmt//%Y/%m}"
    command stat -f "$fmt" "$@"
  }
fi
say() { printf '%s\n' "$*" >> "$T/said"; }
die() { printf 'DIE %s\n' "$*" >> "$T/said"; exit 1; }   # apply_assistant runs in a subshell here
chown() { :; }
install() { mkdir -p "${@: -1}"; }   # the only install left is the folder of a unit file
put_file() {  # setup's own, with root:root written as the tester's owner (no chown here)
  local path="$1" mode="$2" content="$4"
  [ -f "$path" ] && [ "$(cat "$path")" = "$content" ] && return 1
  mkdir -p "$(dirname "$path")"; printf '%s\n' "$content" > "$path"; chmod "$mode" "$path"
}
same_file() { [ -f "$1" ] && [ "$(cat "$1")" = "$2" ]; }
# systemd: what is active or enabled is a file in $T/sd; every call is written down.
mkdir -p "$T/sd"
systemctl() {
  printf '%s\n' "$*" >> "$T/systemctl"
  local verb="" unit="" a quiet_now=0
  for a in "$@"; do
    case "$a" in --quiet|--no-block) ;; --now) quiet_now=1 ;; -*) ;; *) [ -z "$verb" ] && verb="$a" || unit="${unit:-$a}" ;; esac
  done
  case "$verb" in
    is-active) [ -f "$T/sd/$unit.active" ] ;;
    is-enabled) [ -f "$T/sd/$unit.enabled" ] ;;
    enable) for a in "$@"; do case "$a" in -*|enable) ;; *) touch "$T/sd/$a.enabled"; [ "$quiet_now" = 0 ] || touch "$T/sd/$a.active" ;; esac; done ;;
    disable) for a in "$@"; do case "$a" in -*|disable) ;; *) rm -f "$T/sd/$a.enabled" "$T/sd/$a.active" ;; esac; done ;;
    restart|start) touch "$T/sd/$unit.active" ;;
    *) return 0 ;;
  esac
}
assistant_installed() { [ -f "$T/installed" ]; }
started_after_install() { return 0; }

SYSTEMD_DIR="$T/systemd"; mkdir -p "$SYSTEMD_DIR"
ASSISTANT_UNIT=$SYSTEMD_DIR/arceus-assistant@.service
MIRROR_UNIT=$SYSTEMD_DIR/arceus-assistant-mirror@.service
MIRROR_TIMER=$SYSTEMD_DIR/arceus-assistant-mirror@.timer
ME="$(id -un)"   # both projects' users are the tester: the files they own are the tester's
RUN_USER="$ME"; DATA_DIR="$T/factory/arceus/data"; RUN_HOME="$T/factory"; ARCEUS_HOME="$T/factory/arceus"
CONSOLE_PROJECT_DIR="$T/contoso/arceus"; CONSOLE_USER=contoso; CONSOLE_DATA="$T/contoso/arceus/data"
CONSOLE_HOME="$T/contoso"; CONSOLE_APP=127.0.0.1:8180; RUNNER_OPT="$T/runner"
check "a machine with the console on another project has two assistants to look after" \
  "[ \"\$(assistant_projects | cut -d' ' -f1 | tr '\n' ' ')\" = '$ME contoso ' ]"
CONSOLE_PROJECT_DIR="$ARCEUS_HOME"   # from here on, one project: the tester owns its files
mkdir -p "$DATA_DIR" "$CONSOLE_DATA"; REPO_DIR="$T"
id() { echo staff; }
# config.yaml's side: one file of ASSISTANT_* lines per project, as `arceus-runner console env` prints them.
assistant_env() { cat "$2/env" 2>/dev/null || true; }
project_env() {  # data enabled web port
  printf '%s\n' "ASSISTANT_ENABLED=$2" "ASSISTANT_WEB=$3" "ASSISTANT_PORT=$4" "ASSISTANT_HOME=$(dirname "$(dirname "$1")")/assistant" \
    "ASSISTANT_SECRETS=$1/assistant.env" "ASSISTANT_WORKSPACE=$(dirname "$1")/workspace" > "$1/env"
}
project_env "$DATA_DIR" 1 1 8190

# -- units ---------------------------------------------------------------------------------------------------------
unit="$(assistant_unit_text)"
check "one template unit, run as the project's own user" "grep -qx 'User=%i' <<< \"\$unit\""
check "it starts the installed assistant, which reads its config.yaml from the drop-in" \
  "grep -qx 'ExecStart=/opt/arceus-assistant/venv/bin/arceus-assistant' <<< \"\$unit\""
check "its tokens are never put into its environment" "! grep -q '^EnvironmentFile' <<< \"\$unit\""
check "a refused config.yaml is not restarted over and over" "grep -qx 'RestartPreventExitStatus=2' <<< \"\$unit\""
check "the drop-in names the project's config.yaml" \
  "grep -qx 'Environment=ARCEUS_RUNNER_CONFIG=$DATA_DIR/config.yaml' <<< \"\$(assistant_dropin_text '$DATA_DIR')\""
check "the mirror is refreshed from the factory's clones into the assistant's own folder" \
  "grep -qx 'Environment=ASSISTANT_MIRROR=$T/factory/assistant/mirror' <<< \"\$(mirror_dropin_text \"\$(cat $DATA_DIR/env)\")\" \
   && grep -q 'mirror.sh \${ASSISTANT_WORKSPACE} \${ASSISTANT_MIRROR}' <<< \"\$(mirror_unit_text)\""

# -- the old bot ---------------------------------------------------------------------------------------------------
cat > "$SYSTEMD_DIR/factory-assistant.service" <<U
[Service]
User=$ME
ExecStart=/home/factory/assistant/venv/bin/python -m assistant.server
U
cat > "$SYSTEMD_DIR/factory-assistant-contoso.service" <<'U'
[Service]
User=contoso
ExecStart=/home/contoso/assistant/venv/bin/python -m assistant.server
U
touch "$T/sd/factory-assistant.active" "$T/sd/factory-assistant-contoso.active"
check "the old bot of the same user is found" "[ \"\$(legacy_assistant_units $ME)\" = factory-assistant ]"
check "another user's old bot is not this one's" "[ \"\$(legacy_assistant_units contoso)\" = factory-assistant-contoso ]"
touch "$T/installed"
( apply_assistant ); rc=$?
check "setup refuses while the old bot of an enabled project still runs" \
  "[ $rc != 0 ] && grep -q 'DIE.*factory-assistant.*arceus-runner assistant import' '$T/said'"
check "and starts nothing of the new one" "! grep -q 'arceus-assistant@' '$T/systemctl' && [ ! -f '$T/sd/arceus-assistant@$ME.active' ]"
check "the phase is not done meanwhile" "! check_assistant"

# -- carried over: the old one stopped -----------------------------------------------------------------------------
rm -f "$T/sd/factory-assistant.active" "$T/said" "$T/systemctl"
( apply_assistant ); rc=$?
check "with the old one stopped, setup goes through" "[ $rc = 0 ] && ! grep -q DIE '$T/said'"
check "assistant.env is made, 600, before anything is put in it" \
  "[ \"\$(stat -c %a '$DATA_DIR/assistant.env')\" = 600 ] && ! grep -qv '^#' '$DATA_DIR/assistant.env'"
check "the enabled project's assistant is enabled and started, with its mirror's timer" \
  "[ -f '$T/sd/arceus-assistant@$ME.enabled' ] && [ -f '$T/sd/arceus-assistant@$ME.active' ] \
   && [ -f '$T/sd/arceus-assistant-mirror@$ME.timer.enabled' ]"
check "the first mirror is made at once" "grep -q 'start --no-block arceus-assistant-mirror@$ME.service' '$T/systemctl'"
check "the phase is done" "check_assistant"
echo 'SLACK_BOT_TOKEN=xoxb-kept' >> "$DATA_DIR/assistant.env"
rm -f "$T/systemctl"
( apply_assistant ) >/dev/null
check "a second run keeps the tokens and restarts nothing" \
  "grep -q xoxb-kept '$DATA_DIR/assistant.env' && ! grep -q restart '$T/systemctl'"
chmod 644 "$DATA_DIR/assistant.env"
check "assistant.env readable by others is not done" "! check_assistant"
( apply_assistant ) >/dev/null
check "and setup puts it back to 600" "[ \"\$(stat -c %a '$DATA_DIR/assistant.env')\" = 600 ] && check_assistant"

# -- new code: restarted --------------------------------------------------------------------------------------------
rm -f "$T/installed" "$T/systemctl"
pip_ran=0
rsync() { :; }
python3() { :; }
mkdir -p "$T/opt"; ASSISTANT_OPT="$T/opt"; mkdir -p "$ASSISTANT_OPT/venv/bin"
printf '#!/bin/sh\n' > "$ASSISTANT_OPT/venv/bin/pip"; chmod +x "$ASSISTANT_OPT/venv/bin/pip"
assistant_source_hash() { echo new; }
assistant_installed() { [ "$(cat "$ASSISTANT_OPT/.source-sha256" 2>/dev/null)" = new ]; }
( apply_assistant ) >/dev/null
check "a new version is installed and the running assistant restarted with it" \
  "assistant_installed && grep -q 'restart arceus-assistant@$ME' '$T/systemctl'"

# -- turned off ------------------------------------------------------------------------------------------------------
project_env "$DATA_DIR" 0 0 8190
check "turned off in config.yaml, a running assistant is not done" "! check_assistant"
( apply_assistant ) >/dev/null
check "and setup stops and disables it" \
  "[ ! -f '$T/sd/arceus-assistant@$ME.active' ] && [ ! -f '$T/sd/arceus-assistant@$ME.enabled' ] && check_assistant"

rm -f "$DATA_DIR/assistant.env"
( apply_assistant ) >/dev/null
check "assistant.env is made even while the assistant is off: the console's container must never see it" \
  "[ \"\$(stat -c %a '$DATA_DIR/assistant.env')\" = 600 ]"

# -- nginx -----------------------------------------------------------------------------------------------------------
console_get() { case "$1" in ASSISTANT_WEB) echo "$WEB" ;; ASSISTANT_PORT) echo 8191 ;; CONSOLE_PORT) echo 80 ;; esac; }
WEB=1; conf="$(console_nginx_conf)"
check "with the web chat on, /assistant/ goes to the console project's assistant, for signed-in people only" \
  "grep -q 'location /assistant/ {' <<< \"\$conf\" && grep -q 'proxy_pass http://127.0.0.1:8191;' <<< \"\$conf\" \
   && grep -A2 'location /assistant/ {' <<< \"\$conf\" | grep -q 'auth_request /_console_auth;'"
check "who asks is the console's answer, written over anything the browser sent" \
  "grep -q 'proxy_set_header X-Assistant-User \$arceus_user;' <<< \"\$conf\""
WEB=0
check "with it off, there is no /assistant/" "! grep -q '/assistant/' <<< \"\$(console_nginx_conf)\""

printf '\n'
[ "$fails" = 0 ] && { echo "all passed"; exit 0; }
echo "$fails failed"; exit 1
