#!/bin/bash
# nginx in front of the console, as setup.sh writes it, and the real Grafana from observability/compose.yaml with
# the settings setup.sh gives it while the console is on. The three share one network namespace, as
# network_mode: host gives them on the VM, so 127.0.0.1 is the same for all of them. Proves: /grafana/ needs a
# console session; Grafana signs in the person the console names, as Viewer (Admin for admins), whatever
# X-WEBAUTH-* the browser sent; Grafana lets nobody in anonymously; everything else goes to the console.
# Needs Docker and the network (the images). Exits 77 (skipped) without Docker.
#   bash setup/tests/test-console-proxy.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
command -v docker >/dev/null && docker info >/dev/null 2>&1 || { echo "skip: no docker here"; exit 77; }
T="$(mktemp -d)"; P="arceus-proxy-test-$$"; TAG="arceus-console:proxy-test-$$"; PORT=18$((RANDOM % 900 + 100))
OBS=(docker compose -p "$P-obs" --project-directory "$ROOT/observability" -f "$ROOT/observability/compose.yaml"
     -f "$T/grafana-here.yaml")
cleanup() { "${OBS[@]}" down -v >/dev/null 2>&1; docker rm -f "$P-console" "$P-nginx" >/dev/null 2>&1
            docker image rm -f "$TAG" >/dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT
# KEEP=1 leaves it running to look at; the last line says where and how to remove it.
[ -n "${KEEP:-}" ] && trap 'echo "kept: http://127.0.0.1:$PORT  (admin@example.com / correct horse battery); remove: docker rm -f $P-console $P-nginx; ${OBS[*]} down -v"' EXIT
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }

# The nginx.conf setup.sh writes, from its own function.
eval "$(sed -n '/^console_nginx_conf() {/,/^CONF$/{p;/^CONF$/{n;p;};}' "$ROOT/setup/setup.sh")"
CONSOLE_APP=127.0.0.1:8180
console_get() {
  case "$1" in
    CONSOLE_TRUSTED_PROXIES) echo "10.0.0.0/8" ;;
    CONSOLE_PORT) echo 80 ;;
  esac
}
console_nginx_conf > "$T/nginx.conf"

mkdir -p "$T/build/runner" "$T/data"
rsync -a --exclude __pycache__ --exclude '*.egg-info' --exclude build \
  "$ROOT/runner/pyproject.toml" "$ROOT/runner/src" "$T/build/runner/"
docker build --quiet -t "$TAG" -f "$ROOT/console/Dockerfile" "$T/build" >/dev/null || { echo "FAIL image build"; exit 1; }
chmod -R a+rwX "$T/data"


# The console publishes the namespace's port 80 (nginx) to this machine.
docker run -d --name "$P-console" -p "127.0.0.1:$PORT:80" --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp --user 1000:1000 -e HOME=/tmp \
  -e ARCEUS_RUNNER_CONFIG="$T/data/config.yaml" -e ARCEUS_CONSOLE_PUBLIC_URL="http://127.0.0.1:$PORT" \
  -v "$T/data:$T/data" "$TAG" console serve --host 127.0.0.1 --port 8180 >/dev/null
# Grafana as observability/compose.yaml has it, with what setup.sh's obs_env writes while the console is on;
# only its network is the console's namespace here instead of the host's.
printf 'services:\n  grafana:\n    network_mode: "container:%s"\n' "$P-console" > "$T/grafana-here.yaml"
echo "grafana-admin-$$" > "$T/grafana-admin"; chmod 644 "$T/grafana-admin"
mkdir -p "$T/state"
GRAFANA_ADMIN_FILE="$T/grafana-admin" ARCEUS_DATA="$T/data" BULAVA_STATE="$T/state" GRAFANA_ANONYMOUS=false \
  GRAFANA_AUTH_PROXY=true GRAFANA_SUB_PATH=true GRAFANA_ROOT_URL="http://127.0.0.1:$PORT/grafana/" \
  "${OBS[@]}" up -d --quiet-pull grafana >/dev/null 2>&1 || { echo "FAIL grafana did not start"; exit 1; }
inside() {  # a GET from inside the namespace, past nginx: what Grafana itself answers
  docker exec "$P-console" /opt/console/bin/python -c "
import sys, urllib.request, urllib.error
try:
    r = urllib.request.urlopen(sys.argv[1], timeout=5); print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)" "$1"
}
for _ in $(seq 1 90); do [ "$(inside http://127.0.0.1:3000/grafana/api/health)" = 200 ] && break; sleep 2; done
docker run -d --name "$P-nginx" --network "container:$P-console" --read-only --cap-drop ALL \
  --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add NET_BIND_SERVICE \
  --security-opt no-new-privileges:true --tmpfs /var/cache/nginx --tmpfs /var/run --tmpfs /tmp \
  -v "$T/nginx.conf:/etc/nginx/nginx.conf:ro" nginx:1.30.2-alpine >/dev/null
URL="http://127.0.0.1:$PORT"
for _ in $(seq 1 30); do curl -fs -o /dev/null "$URL/healthz" && break; sleep 1; done
check "nginx serves the console" "curl -fs '$URL/healthz' | grep -qx ok"

# Two people: an admin and one with no role, made by the server's command, signed in through nginx.
people() { docker exec "$P-console" /opt/console/bin/arceus-runner --config "$T/data/config.yaml" console "$@"; }
sign_in() {  # $1 jar, $2 email: invite, set the password through the link, sign in
  local jar="$1" email="$2" link csrf
  link="$(people invite "$email" ${3:-} --url "$URL" | sed -n 's#.*\(/link/[^ ]*\)$#\1#p')"
  csrf="$(curl -s -c "$jar" -b "$jar" "$URL$link" | sed -n 's/.*name="csrf" value="\([^"]*\)".*/\1/p' | head -1)"
  curl -s -o /dev/null -c "$jar" -b "$jar" -H "Origin: $URL" --data-urlencode "csrf=$csrf" \
    --data-urlencode "name=Test Person" --data-urlencode "password=correct horse battery" \
    --data-urlencode "password2=correct horse battery" "$URL$link"
  csrf="$(curl -s -c "$jar" -b "$jar" "$URL/login" | sed -n 's/.*name="csrf" value="\([^"]*\)".*/\1/p' | head -1)"
  curl -s -o /dev/null -c "$jar" -b "$jar" -H "Origin: $URL" --data-urlencode "csrf=$csrf" \
    --data-urlencode "email=$email" --data-urlencode "password=correct horse battery" "$URL/login"
}
sign_in "$T/admin.jar" admin@example.com --admin
sign_in "$T/viewer.jar" viewer@example.com

code="$(curl -s -o /dev/null -w '%{http_code}' "$URL/grafana/d/arceus-now")"
where="$(curl -s -o /dev/null -D - "$URL/grafana/d/arceus-now" | tr -d '\r' | sed -n 's/^[Ll]ocation: //p')"
check "without a session /grafana/ sends to the sign-in, by path (behind TLS the scheme is not nginx's)" \
  "[ '$code' = 302 ] && [ '$where' = '/login?next=/grafana/' ]"
code="$(curl -s -o /dev/null -w '%{http_code}' -H 'X-WEBAUTH-USER: admin@example.com' -H 'X-WEBAUTH-ROLE: Admin' \
  "$URL/grafana/")"
check "a Grafana user named by the browser itself does not get in" "[ '$code' = 302 ]"

check "Grafana is up, serving under /grafana/" "[ \"\$(inside http://127.0.0.1:3000/grafana/api/health)\" = 200 ]"
check "Grafana itself lets nobody in anonymously" \
  "[ \"\$(inside http://127.0.0.1:3000/grafana/api/dashboards/uid/arceus-now)\" = 401 ]"
spoof=(-H 'X-WEBAUTH-USER: admin@example.com' -H 'X-WEBAUTH-ROLE: Admin' -H 'X-WEBAUTH-NAME: Mallory')
user="$(curl -s -b "$T/viewer.jar" "${spoof[@]}" "$URL/grafana/api/user")"
orgs="$(curl -s -b "$T/viewer.jar" "${spoof[@]}" "$URL/grafana/api/user/orgs")"
check "signed in, Grafana's user is the person the console names, not the browser's header" \
  "grep -q '\"login\":\"viewer@example.com\"' <<< '$user'"
check "with no role in the console, a Grafana Viewer (the browser's Admin is overwritten)" \
  "grep -q '\"role\":\"Viewer\"' <<< '$orgs' && ! grep -q '\"role\":\"Admin\"' <<< '$orgs'"
code="$(curl -s -o /dev/null -w '%{http_code}' -b "$T/viewer.jar" "$URL/grafana/api/dashboards/uid/arceus-now")"
check "a Viewer sees the provisioned dashboards" "[ '$code' = 200 ]"
code="$(curl -s -o /dev/null -w '%{http_code}' -b "$T/viewer.jar" "$URL/grafana/d/arceus-now")"
check "and Grafana's own page opens for them" "[ '$code' = 200 ]"
orgs="$(curl -s -b "$T/admin.jar" "$URL/grafana/api/user/orgs")"
user="$(curl -s -b "$T/admin.jar" "$URL/grafana/api/user")"
check "a console admin is Grafana's Admin" \
  "grep -q '\"login\":\"admin@example.com\"' <<< '$user' && grep -q '\"role\":\"Admin\"' <<< '$orgs'"
code="$(curl -s -o /dev/null -w '%{http_code}' -b "$T/viewer.jar" "$URL/")"
check "the console's own pages go to the console" "[ '$code' = 200 ]"

[ "$fails" = 0 ] && { echo "all passed"; exit 0; }
docker logs "$P-nginx" 2>&1 | tail -5; "${OBS[@]}" logs grafana 2>&1 | tail -15
echo "$fails failed"; exit 1
