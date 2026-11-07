#!/bin/bash
# claude and codex are only the pinned ones in /opt/agent-tools: setup finds the copies Claude's native
# installer and `npm i -g` under nvm leave in the user's home, removes them, and leaves the rest of the
# home (the sign-ins in ~/.claude and ~/.codex, Bulava's commands in ~/.local/bin) as it was.
#   bash setup/tests/test-agent-tools.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }

# The functions under test, taken from setup.sh as they are (sourcing it would run its phases).
eval "$(sed -n '/^stray_agent_tools() {/,/^}/p; /^remove_stray_agent_tools() {/,/^}/p' "$HERE/../setup.sh")"
type stray_agent_tools remove_stray_agent_tools >/dev/null || { echo "FAIL the functions are not in setup.sh"; exit 1; }
say() { printf '%s\n' "$*" >> "$T/said"; }
RUN_HOME="$T/home" TOOLS=/opt/agent-tools

mkdir -p "$RUN_HOME/.local/bin" "$RUN_HOME/.claude" "$RUN_HOME/.codex"
echo creds > "$RUN_HOME/.claude/.credentials.json"; echo auth > "$RUN_HOME/.codex/auth.json"
ln -s /opt/bulava/engine/bin/night-shift.sh "$RUN_HOME/.local/bin/night-shift"
check "a home with only the sign-ins and Bulava's commands has no copies" '[ -z "$(stray_agent_tools)" ]'

mkdir -p "$RUN_HOME/.local/share/claude/versions" "$RUN_HOME/.nvm/versions/node/v24.22.1/bin" \
  "$RUN_HOME/.nvm/versions/node/v24.22.1/lib/node_modules/@openai/codex/bin" \
  "$RUN_HOME/.nvm/versions/node/v24.22.1/lib/node_modules/npm"
touch "$RUN_HOME/.local/share/claude/versions/2.1.319"
ln -s "$RUN_HOME/.local/share/claude/versions/2.1.319" "$RUN_HOME/.local/bin/claude"
ln -s ../lib/node_modules/@openai/codex/bin/codex.js "$RUN_HOME/.nvm/versions/node/v24.22.1/bin/codex"
ln -s /nowhere "$RUN_HOME/.local/bin/codex"   # a link whose target is gone is a copy too
check "the native claude, the nvm codex and a dangling link are all found" \
  '[ "$(stray_agent_tools | wc -l)" -eq 5 ]'

remove_stray_agent_tools
check "they are removed" '[ -z "$(stray_agent_tools)" ] && grep -q "removed .*/.local/share/claude" "$T/said"'
check "the sign-ins are untouched" \
  '[ "$(cat "$RUN_HOME/.claude/.credentials.json")" = creds ] && [ "$(cat "$RUN_HOME/.codex/auth.json")" = auth ]'
check "Bulava's commands and nvm's own npm stay" \
  '[ -L "$RUN_HOME/.local/bin/night-shift" ] && [ -d "$RUN_HOME/.nvm/versions/node/v24.22.1/lib/node_modules/npm" ]'

[ "$fails" = 0 ] && { echo "all passed"; exit 0; }
exit 1
