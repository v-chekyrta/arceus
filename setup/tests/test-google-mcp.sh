#!/bin/bash
# The agent's Google MCP server: setup puts it into mcp.json only once a service account's key is in place, read-only,
# with no way for the agent to start a sign-in of its own, and with the key itself left out of the file every worker
# reads.
#   bash setup/tests/test-google-mcp.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }

# The function under test, taken from setup.sh as it is (sourcing it would run its phases).
eval "$(sed -n '/^mcp_json() {/,/^}/p' "$HERE/../setup.sh")"
type mcp_json >/dev/null || { echo "FAIL mcp_json is not in setup.sh"; exit 1; }

TOOLS=/opt/agent-tools GOOGLE_MCP=/opt/agent-tools/workspace-mcp GOOGLE_DIR="$T/google" WORKSPACE_MCP_VERSION=2.3.0
mkdir -p "$GOOGLE_DIR"

mcp_json > "$T/without.json"
check "no key yet: only the browser" '[ "$(jq -c ".mcpServers | keys" "$T/without.json")" = "[\"browser\"]" ]'

echo '{"type":"service_account","client_email":"agent@proj.iam.gserviceaccount.com","private_key":"PRIVATE"}' \
  > "$GOOGLE_DIR/service-account.json"
mcp_json > "$T/with.json"
g() { jq -c "$1" "$T/with.json"; }
check "with the key: the google server next to the browser" '[ "$(g ".mcpServers | keys")" = "[\"browser\",\"google\"]" ]'
check "it is the pinned install" '[ "$(g ".mcpServers.google.command")" = "\"/opt/agent-tools/workspace-mcp/bin/workspace-mcp\"" ]'
check "read-only, only Sheets, Docs and Drive" \
  '[ "$(g "[.mcpServers.google.args[] | select(. == \"--read-only\")] | length")" = 1 ] \
   && [ "$(g "[.mcpServers.google.args | index(\"--tools\") as \$i | .[\$i+1:\$i+4][]]")" = "[\"sheets\",\"docs\",\"drive\"]" ]'
check "the agent cannot start a sign-in itself" \
  '[ "$(g ".mcpServers.google.args | index(\"--disabled-tools\") as \$i | .[\$i+1]")" = "\"start_google_auth\"" ]'
check "it acts as the service account itself, with the key from the data folder" \
  '[ "$(g ".mcpServers.google.env.USER_GOOGLE_EMAIL")" = "\"agent@proj.iam.gserviceaccount.com\"" ] \
   && [ "$(g ".mcpServers.google.env.GOOGLE_SERVICE_ACCOUNT_KEY_FILE")" = "\"$GOOGLE_DIR/service-account.json\"" ]'
check "the key itself stays in its file" '! grep -q PRIVATE "$T/with.json"'

echo '{"type":"authorized_user"}' > "$GOOGLE_DIR/service-account.json"
mcp_json > "$T/notkey.json" 2>"$T/notkey.err"
check "a file that is not a service account's key: no Google, said, and still a whole mcp.json" \
  '[ "$(jq -c ".mcpServers | keys" "$T/notkey.json")" = "[\"browser\"]" ] && grep -q "not a service account key" "$T/notkey.err"'

echo '{"type":"service_account","client_email":"agent@proj.iam.gserviceaccount.com"}' > "$GOOGLE_DIR/service-account.json"
WORKSPACE_MCP_VERSION="" mcp_json > "$T/off.json"
check "no version in setup.conf: no Google, key or not" '[ "$(jq -c ".mcpServers | keys" "$T/off.json")" = "[\"browser\"]" ]'

printf '\n'
[ "$fails" = 0 ] && { echo "google mcp: all passed"; exit 0; }
echo "google mcp: $fails failed"; exit 1
