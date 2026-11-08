#!/bin/bash
# nginx in front of the console, as setup.sh writes it, with a stand-in for Grafana that answers with the headers it
# got. The three share one network namespace, as network_mode: host gives them on the VM, so 127.0.0.1 is the same
# for all of them. Proves: /grafana/ needs a console session; Grafana is told who it is (Viewer, Admin for admins);
# an X-WEBAUTH-USER sent from outside never reaches it; everything else goes to the console.
# Needs Docker and the network. Exits 77 (skipped) without Docker.
#   bash setup/tests/test-console-proxy.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
command -v docker >/dev/null && docker info >/dev/null 2>&1 || { echo "skip: no docker here"; exit 77; }
T="$(mktemp -d)"; P="arceus-proxy-test-$$"; TAG="arceus-console:proxy-test-$$"; PORT=18$((RANDOM % 900 + 100))
cleanup() { docker rm -f "$P-console" "$P-grafana" "$P-nginx" >/dev/null 2>&1; docker image rm -f "$TAG" >/dev/null 2>&1
            rm -rf "$T"; }
trap cleanup EXIT
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
cat > "$T/grafana.py" <<'PY'
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"path": self.path, **{k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("127.0.0.1", 3000), H).serve_forever()
PY
chmod a+r "$T/grafana.py"

# The console publishes the namespace's port 80 (nginx) to this machine.
docker run -d --name "$P-console" -p "127.0.0.1:$PORT:80" --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp --user 1000:1000 -e HOME=/tmp \
  -e ARCEUS_RUNNER_CONFIG="$T/data/config.yaml" -e ARCEUS_CONSOLE_PUBLIC_URL="http://127.0.0.1:$PORT" \
  -v "$T/data:$T/data" "$TAG" console serve --host 127.0.0.1 --port 8180 >/dev/null
docker run -d --name "$P-grafana" --network "container:$P-console" --user 1000:1000 -v "$T/grafana.py:/g.py:ro" \
  --entrypoint /opt/console/bin/python "$TAG" /g.py >/dev/null
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

got="$(curl -s -b "$T/viewer.jar" -H 'X-WEBAUTH-USER: admin@example.com' -H 'X-WEBAUTH-ROLE: Admin' "$URL/grafana/d/x")"
check "signed in, Grafana gets the person the console names" "grep -q '\"x-webauth-user\": \"viewer@example.com\"' <<< '$got'"
check "with no role, as a Viewer: the browser's Admin is overwritten" "grep -q '\"x-webauth-role\": \"Viewer\"' <<< '$got'"
check "Grafana sees the path under /grafana/" "grep -q '\"path\": \"/grafana/d/x\"' <<< '$got'"
got="$(curl -s -b "$T/admin.jar" "$URL/grafana/")"
check "an admin is Grafana's Admin" "grep -q '\"x-webauth-user\": \"admin@example.com\"' <<< '$got' && grep -q '\"x-webauth-role\": \"Admin\"' <<< '$got'"
code="$(curl -s -o /dev/null -w '%{http_code}' -b "$T/viewer.jar" "$URL/")"
check "the console's own pages go to the console" "[ '$code' = 200 ]"

[ "$fails" = 0 ] && { echo "all passed"; exit 0; }
docker logs "$P-nginx" 2>&1 | tail -5
echo "$fails failed"; exit 1
