#!/bin/bash
# Everything that proves this repository works, in one command, from its root: the runner's tests and the
# setup script's test. The sandbox (sandbox/) and its live scenarios are not part of it: they are for work on
# the pipeline itself and run by hand, sandbox/sandbox.sh test and sandbox/sandbox.sh live.
# Exits non-zero when a part fails. No model is called.
# --offline leaves out the one step that needs the network (the engine's real upstream on GitHub): what CI runs
# on every pull request and merge, so a GitHub hiccup upstream never turns a change red.
set -uo pipefail
cd "$(dirname "$0")"

live=--live
case "${1:-}" in
  "") ;;
  --offline) live= ;;
  *) echo "usage: $0 [--offline]" >&2; exit 2 ;;
esac

fails=0
SKIP=77   # a step that had nothing to check: shown as skipped, never as ok
step() {   # $1=what, then the command
  local what="$1" rc; shift
  printf '\n== %s\n' "$what"
  "$@"; rc=$?
  case "$rc" in
    0) printf 'ok   %s\n' "$what" ;;
    "$SKIP") printf 'skip %s\n' "$what" ;;
    *) printf 'FAIL %s\n' "$what"; fails=$((fails + 1)) ;;
  esac
}

step "runner tests (Mac or wherever this runs)" bash -c 'cd runner && .venv/bin/pytest -q -n auto'
step "setup: running services are enabled for good, so they come back after a reboot" \
  bash setup/tests/test-autostart.sh
step "setup: claude and codex are only the pinned ones, the sign-ins stay" \
  bash setup/tests/test-agent-tools.sh
step "setup: the agent's Google MCP server is read-only and acts as the service account" \
  bash setup/tests/test-google-mcp.sh
step "setup: the console sees the data folder without its secrets; nginx trusts only the given proxies" \
  bash setup/tests/test-console.sh
if [ -n "$live" ]; then  # the base image and PyPI: the network, so not in --offline
  step "console: the image builds from console/Dockerfile and runs without root, sudo or secrets" \
    bash setup/tests/test-console-image.sh
fi
step "setup: the engine's source survives a rewritten upstream${live:+, the pinned commit installs}" \
  bash setup/tests/test-bulava-source.sh $live

printf '\n'
[ "$fails" = 0 ] && { echo "verify: all passed"; exit 0; }
echo "verify: $fails failed"; exit 1
