#!/bin/bash
# The console phase's files: what the container sees and what it must not, and nginx's settings.
# The functions are taken from setup.sh as they are; config.yaml's side (arceus-runner console env) is a
# stand-in. With Docker at hand, compose also reads the result as valid. No root needed.
#   bash setup/tests/test-console.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }

# console_nginx_conf is a here-document whose own lines start with }: it ends at CONF and the line after.
eval "$(sed -n '/^yaml_str() {/p; /^console_secret_paths() {/,/^}/p; /^console_mounts() {/,/^}/p;
                /^console_env() {/,/^}/p' "$HERE/../setup.sh")"
eval "$(sed -n '/^console_nginx_conf() {/,/^CONF$/{p;/^CONF$/{n;p;};}' "$HERE/../setup.sh")"
type yaml_str console_secret_paths console_mounts console_nginx_conf >/dev/null \
  || { echo "FAIL the functions are not in setup.sh"; exit 1; }

RUN_USER="$(id -un)"; RUN_GROUP="$(id -gn)"; RUN_HOME="$T/home"
DATA_DIR="$T/home/arceus/data"; CONSOLE_APP=127.0.0.1:8180
mkdir -p "$DATA_DIR/google" "$DATA_DIR/qa/runs" "$T/srv/arch runs" "$T/home/.config/claude"
for f in config.yaml runner.db runner.db-wal secrets.env secrets.vso_korean.env grafana-admin-password \
         config.linker.yaml events.jsonl my.pem; do echo x > "$DATA_DIR/$f"; done
echo token > "$T/home/.config/claude/oauth.env"
ln -s "$T/home/.config/claude/oauth.env" "$DATA_DIR/bulava.env"
install() { mkdir -p "${@: -1}"; }   # the read-only folders setup makes when missing; no chown here

console_get() {
  case "$1" in
    CONSOLE_READ_DIR) printf '%s\n' "$DATA_DIR" "$T/srv/arch runs" ;;
    CONSOLE_SECRET_FILE) printf '%s\n' "$DATA_DIR/secrets.env" "$DATA_DIR/bulava.env" ;;
    CONSOLE_TRUSTED_PROXIES) echo "10.0.0.0/8 192.168.0.0/16" ;;
    CONSOLE_PORT) echo 8081 ;;
    CONSOLE_PUBLIC_URL) echo https://console.example.com ;;
    CONSOLE_SESSION_IDLE) echo 86400s ;;
    CONSOLE_SESSION_MAX) echo 1209600s ;;
  esac
}
console_source_hash() { echo 0123456789abcdef; }

mounts="$(console_mounts)"; [ -n "${SHOW:-}" ] && printf "%s\n" "$mounts"
printf '%s\n' "$mounts" > "$T/mounts.yaml"
masked() { grep -qF "target: \"$1\"" <<< "$mounts" && ! grep -qF "source: \"$1\"" <<< "$mounts"; }
check "the runner's secrets are replaced by empty files" "masked '$DATA_DIR/secrets.env' && masked '$DATA_DIR/secrets.vso_korean.env'"
check "so is Grafana's admin password and a key file" "masked '$DATA_DIR/grafana-admin-password' && masked '$DATA_DIR/my.pem'"
check "the Google key's folder is an empty one in memory" "grep -qF 'type: tmpfs, target: \"$DATA_DIR/google\"' <<< \"\$mounts\""
check "a link to a file outside is left alone: its target is not there to read" "! grep -qF '$DATA_DIR/bulava.env' <<< \"\$mounts\""
check "runner.db, config.yaml and the reports stay as they are" \
  "! grep -qE 'data/(runner\\.db|config\\.yaml|events\\.jsonl|qa)' <<< \"\$mounts\""
check "a reports folder outside the data folder is mounted read-only at its own path" \
  "grep -qF '{type: bind, source: \"$T/srv/arch runs\", target: \"$T/srv/arch runs\", read_only: true}' <<< \"\$mounts\""
check "the data folder itself is not mounted twice" "! grep -qF 'source: \"$DATA_DIR\"' <<< \"\$mounts\""

conf="$(console_nginx_conf)"
check "nginx listens on console.port" "grep -q 'listen 8081 default_server;' <<< \"\$conf\""
check "and believes X-Forwarded-For only from the trusted proxies" \
  "grep -q 'set_real_ip_from 10.0.0.0/8;' <<< \"\$conf\" && grep -q 'set_real_ip_from 192.168.0.0/16;' <<< \"\$conf\" && grep -q 'real_ip_recursive on;' <<< \"\$conf\""
check "and passes everything to the console on localhost" "grep -q 'proxy_pass http://127.0.0.1:8180;' <<< \"\$conf\""

eval "$(sed -n '/^OPS_UNITS=/p; /^ops_command() {/,/^}/p; /^ops_unit_text() {/,/^}/p' "$HERE/../setup.sh")"
RUNNER_OPT=/opt/arceus-runner; ARCEUS_HOME="$T/home/arceus"
check "the executor's units are the four it needs" "[ \"\${OPS_UNITS[*]}\" = 'arceus-ops.path arceus-ops.service arceus-ops-tick.service arceus-ops-tick.timer' ]"
check "the path unit wakes on a request in the data folder" \
  "ops_unit_text arceus-ops.path | grep -qx 'DirectoryNotEmpty=$DATA_DIR/console/ops/requests'"
check "the service takes requests with the runner's own code, as root, from the fixed list" \
  "ops_unit_text arceus-ops.service | grep -qx 'ExecStart=/opt/arceus-runner/venv/bin/arceus-runner console hostops --data $DATA_DIR --setup $T/home/arceus/setup/setup.sh dispatch' && ! ops_unit_text arceus-ops.service | grep -q '^User='"
check "the timer passes every 20 seconds, from boot on" \
  "ops_unit_text arceus-ops-tick.timer | grep -qx 'OnUnitActiveSec=20s' && ops_unit_text arceus-ops-tick.timer | grep -qx 'OnBootSec=15s' && ops_unit_text arceus-ops-tick.service | grep -q 'hostops .* tick$'"

env_text="$(console_env)"
check "the container runs as the run user" "grep -qx \"RUN_UID=$(id -u)\" <<< \"\$env_text\" && grep -qx 'CONSOLE_IMAGE_TAG=0123456789abcdef' <<< \"\$env_text\""

if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  printf '%s\n' "$env_text" > "$T/.env"
  cp "$HERE/../../console/compose.yaml" "$T/compose.yaml"
  out="$(docker compose --project-directory "$T" -f "$T/compose.yaml" -f "$T/mounts.yaml" config 2>&1)"
  check "compose reads compose.yaml with mounts.yaml as valid" "[ \$? = 0 ] && grep -q 'read_only: true' <<< \"\$out\""
  check "the console has no capabilities and no new privileges" \
    "grep -A1 'cap_drop:' <<< \"\$out\" | grep -q ALL && grep -q 'no-new-privileges:true' <<< \"\$out\""
else
  echo "skip compose config: no docker compose here"
fi

[ "$fails" = 0 ] && { echo "all passed"; exit 0; }
echo "$fails failed"; exit 1
