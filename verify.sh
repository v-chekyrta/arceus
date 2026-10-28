#!/bin/bash
# Everything that proves this repository works, in one command, from its root: the runner's tests and the
# setup script's test. The sandbox (sandbox/) and its live scenarios are not part of it: they are for work on
# the pipeline itself and run by hand, sandbox/sandbox.sh test and sandbox/sandbox.sh live.
# Exits non-zero when a part fails. No model is called.
set -uo pipefail
cd "$(dirname "$0")"

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
step "setup: the engine's source survives a rewritten upstream, the pinned commit installs" \
  bash setup/tests/test-bulava-source.sh --live

printf '\n'
[ "$fails" = 0 ] && { echo "verify: all passed"; exit 0; }
echo "verify: $fails failed"; exit 1
