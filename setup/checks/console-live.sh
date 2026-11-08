#!/bin/bash
# A console as it runs on a VM, checked from outside and over SSH. Exits 0 only if everything holds.
#   bash setup/checks/console-live.sh https://console.example.com user@vm -p 22 [--owner EMAIL] [--since 2026-11-08T23:20]
# From outside: HTTPS answers the sign-in, http goes to https, the session cookie is Secure and HttpOnly, Grafana
# needs a session and ignores an identity header from the browser, the inner ports are closed. On the VM: the
# services and the host's executor are active, setup --check is clean, the console reads the VM's own runner.db and
# config.yaml, the operations it ran ended as the host recorded them, nothing failed since --since, and --owner has
# an unused admin invitation (its token is never printed).
set -uo pipefail

URL="${1:?usage: $0 <public url> <ssh target> [ssh options] [--owner EMAIL] [--since ISO]}"; shift
TARGET="${1:?ssh target}"; shift
SSH_OPTS=(); OWNER=""; SINCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --owner) OWNER="$2"; shift 2 ;;
    --since) SINCE="$2"; shift 2 ;;
    *) SSH_OPTS+=("$1"); shift ;;
  esac
done
HOST="$(printf '%s' "$URL" | sed -E 's#^https?://([^/:]+).*#\1#')"
VM="${TARGET#*@}"
on_vm() { ssh -o BatchMode=yes -o ConnectTimeout=15 "${SSH_OPTS[@]}" "$TARGET" "$@"; }
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }

# -- from outside ------------------------------------------------------------------------------------------------
check "https://$HOST/login answers the console's sign-in" \
  "curl -fsS -m 20 '$URL/login' | grep -q '<h1>Sign in</h1>'"
check "http goes to https" \
  "[ \"\$(curl -sS -m 20 -o /dev/null -w '%{http_code} %{redirect_url}' 'http://$HOST/login')\" = '308 https://$HOST/login' ] || curl -sS -m 20 -o /dev/null -w '%{http_code} %{redirect_url}' 'http://$HOST/login' | grep -qE '^30[178] https://'"
cookie="$(curl -sS -m 20 -D - -o /dev/null "$URL/login" | tr -d '\r' | grep -i '^set-cookie:')"
check "the cookie is a __Host- cookie, Secure, HttpOnly, SameSite" \
  "grep -q '__Host-arceus' <<< \"\$cookie\" && grep -qi 'secure' <<< \"\$cookie\" && grep -qi 'httponly' <<< \"\$cookie\" && grep -qi 'samesite=lax' <<< \"\$cookie\""
check "pages need a session" \
  "[ \"\$(curl -sS -m 20 -o /dev/null -w '%{http_code}' '$URL/settings/')\" = 303 ]"
check "Grafana needs a session and sends to the sign-in" \
  "curl -sS -m 20 -o /dev/null -D - '$URL/grafana/d/arceus-now' | tr -d '\r' | grep -qx 'location: /login?next=/grafana/'"
check "an identity header from the browser does not open Grafana" \
  "[ \"\$(curl -sS -m 20 -o /dev/null -w '%{http_code}' -H 'X-WEBAUTH-USER: admin' -H 'X-WEBAUTH-ROLE: Admin' '$URL/grafana/api/user')\" = 302 ]"
for port in 3000 8180 9090 9464 3100; do
  check "port $port is closed from outside ($VM)" "! nc -z -w 5 '$VM' $port 2>/dev/null"
done

# -- on the VM ---------------------------------------------------------------------------------------------------
check "runner, engine and the host's executor are active" \
  "[ \"\$(on_vm 'systemctl is-active runner bulava-engine arceus-ops.path arceus-ops-tick.timer' | sort -u)\" = active ]"
check "the console's container is healthy and nginx runs" \
  "on_vm 'docker ps --format \"{{.Names}} {{.Status}}\"' | grep -q 'arceus-console-console-1 .*(healthy)' && on_vm 'docker ps --format {{.Names}}' | grep -qx arceus-console-nginx-1"
check "Grafana lets nobody in anonymously on the VM itself" \
  "[ \"\$(on_vm 'curl -s -o /dev/null -w %{http_code} http://127.0.0.1:3000/grafana/api/dashboards/uid/arceus-now')\" = 401 ]"
check "setup.sh --check is clean" "on_vm 'cd ~/arceus/setup && sudo -n ./setup.sh --check' >/dev/null 2>&1"

# The console's own reading of the VM, from inside its container, against runner.db and config.yaml read directly.
read -r -d '' PROBE <<'PY'
import hashlib, json, os, sqlite3
from pathlib import Path
from arceus_runner.config import load_config
from arceus_runner.console import status
cfg = load_config(Path(os.environ["ARCEUS_RUNNER_CONFIG"]))
snap = status.snapshot(cfg)
db = sqlite3.connect(f"file:{cfg.paths.db}?mode=ro", uri=True)
direct = {
    "schema": db.execute("pragma user_version").fetchone()[0],
    "active": db.execute("select count(*) from tasks where state in ('claimed','dispatched','publishing')").fetchone()[0],
    "waiting": db.execute("select count(*) from tasks where state in ('needs_input','security_hold')").fetchone()[0],
    "review": db.execute("select count(*) from tasks where state = 'code_review'").fetchone()[0],
    "loaded": json.loads(db.execute("select value from runner_state where key = 'config'").fetchone()[0])["sha256"],
}
print(json.dumps({"console": {"error": snap["db"].get("error"), "active": len(snap["db"].get("active", [])),
                              "waiting": len(snap["db"].get("waiting", [])), "review": len(snap["db"].get("review", [])),
                              "runner": snap["runner"]["words"], "mode": snap["mode"]},
                  "direct": direct, "file_sha": hashlib.sha256(Path(os.environ["ARCEUS_RUNNER_CONFIG"]).read_bytes()).hexdigest()}))
PY
probe="$(on_vm "docker exec -i arceus-console-console-1 /opt/console/bin/python -" <<< "$PROBE" 2>&1)"
pj() { python3 -c "import json,sys; d=json.loads(sys.argv[1]); print($1)" "$probe" 2>/dev/null; }
check "the console reads the VM's runner.db (schema 22, no error)" "[ \"\$(pj 'd[\"direct\"][\"schema\"]')\" = 22 ] && [ \"\$(pj 'd[\"console\"][\"error\"]')\" = None ]"
check "its counts are runner.db's own (in work, waiting on people, in code review)" \
  "[ \"\$(pj '[d[\"console\"][k] for k in (\"active\",\"waiting\",\"review\")] == [d[\"direct\"][k] for k in (\"active\",\"waiting\",\"review\")]')\" = True ]"
check "it sees the runner answering in its configured mode" \
  "[ \"\$(pj 'd[\"console\"][\"runner\"]')\" = Running ] && [ -n \"\$(pj 'd[\"console\"][\"mode\"]')\" ]"
check "the runner runs the config.yaml that is in the file" "[ \"\$(pj 'd[\"direct\"][\"loaded\"] == d[\"file_sha\"]')\" = True ]"

# What the host recorded of the operations started from the console (its own ledger, root's).
ledger="$(on_vm 'sudo -n sh -c "cat /var/lib/arceus/ops/*.json 2>/dev/null"' | python3 -c '
import json, sys
text = sys.stdin.read().replace("}{", "}\n{")
recs = []
dec = json.JSONDecoder(); i = 0
while i < len(text):
    while i < len(text) and text[i].isspace(): i += 1
    if i >= len(text): break
    obj, i = dec.raw_decode(text, i); recs.append(obj)
for r in recs: print(r.get("op"), r.get("state"), (r.get("health") or "").replace(" ", "_"))')"
check "the setup check run from the console succeeded on the host" "grep -q '^setup-check succeeded' <<< \"\$ledger\""
check "the runner restart run from the console succeeded, the runner back and loading config.yaml" \
  "grep -E '^restart-runner succeeded runner.service_active_since_.*loaded_config.yaml' <<< \"\$ledger\" | grep -q ."
check "no operation is left running or lost" "! grep -qE ' (accepted|running|waiting|interrupted) ' <<< \"\$ledger\""

if [ -n "$SINCE" ]; then
  failed="$(on_vm "python3 - '$SINCE'" <<'PY'
import json, os, sys
since, n = sys.argv[1], 0
for line in open(os.path.expanduser("~/arceus/data/events.jsonl"), encoding="utf-8", errors="replace"):
    if '"ts": "' not in line:
        continue
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if e.get("ts", "") >= since and (e.get("event", "").endswith("_failed") or e.get("event") == "task_stopped"):
        n += 1
print(n)
PY
)"
  check "nothing failed in the runner since $SINCE" "[ '$failed' = 0 ]"
fi

if [ -n "$OWNER" ]; then
  invite="$(on_vm "sqlite3 ~/arceus/data/console/console.db \"select count(*) from links where kind = 'invite' and email = '$OWNER' and used_at is null and expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now') and roles like '%admin%'\"" 2>/dev/null)"
  active_admin="$(on_vm "sqlite3 ~/arceus/data/console/console.db \"select count(*) from users where email = '$OWNER' and status = 'active' and roles like '%admin%'\"" 2>/dev/null)"
  check "$OWNER has an unused admin invitation, or is an active admin already" "[ \"\${invite:-0}\" -ge 1 ] || [ \"\${active_admin:-0}\" -ge 1 ]"
fi

[ "$fails" = 0 ] && { echo "all passed"; exit 0; }
echo "$fails failed"; exit 1
