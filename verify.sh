#!/bin/bash
# Everything that proves this repository works, in one command, from its root:
#   the runner's tests, the setup and sandbox script tests, and — when the sandbox container is up —
#   the runner's tests on Linux x86_64, the runner against the real Bulava engine, and the invariants of
#   the latest live run of each scenario (artifacts/sandbox, written by runner/tests/test_live_pipeline.py).
# Exits non-zero on the first part that fails. No model is called: the live runs are read, not repeated.
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
# sandbox/ is local and git-ignored: a clone without it skips its part and says so.
if [ -d sandbox ]; then
  step "sandbox.sh live keeps pytest's status and brings reports back" bash sandbox/tests/test-live-report.sh
else
  printf '\n(skipped: there is no sandbox/ in this checkout, so neither its scripts nor the container are tested)\n'
fi

box() { docker exec -u ai -w /home/ai/arceus/runner "$@"; }
if [ -d sandbox ] && [ "$(docker inspect -f '{{.State.Running}}' arceus-sandbox 2>/dev/null)" = true ]; then
  step "sandbox: keeper.py cold and repeated starts" bash sandbox/tests/test-keeper.sh
  step "sandbox: runner tests on Ubuntu 26.04 x86_64" box arceus-sandbox .venv/bin/pytest -q -n auto
  step "sandbox: the runner against the real Bulava engine" \
    box -e ARCEUS_REAL_ENGINE=/opt/bulava/engine arceus-sandbox .venv/bin/pytest -q tests/test_real_engine.py
elif [ -d sandbox ]; then
  printf '\n(the sandbox container is not running: sandbox/sandbox.sh up runs its part too)\n'
fi

# The latest report of each live scenario must still say what the scenario is for. Runs are read only.
latest() { ls -1d artifacts/sandbox/*/"$1".json 2>/dev/null | sort | tail -1; }
live_report() {   # $1=scenario $2=jq condition
  local f; f="$(latest "$1")"
  [ -n "$f" ] || { echo "no live report of $1 here (sandbox/sandbox.sh live writes one)"; return "$SKIP"; }
  echo "$f"; jq -e "$2" "$f" >/dev/null
}
gate_checks='[.timeline[] | select(.stage == "gate.check") | .state]'
step "live: planted-problem ended in code_review with one MR" \
  live_report planted-problem '.runner_state == "code_review" and (.mrs | length) == 1'
step "live: follow-ups (review comment, red CI) ended in code_review with one MR" \
  live_report follow-ups '.runner_state == "code_review" and (.mrs | length) == 1'
step "live: a required check that timed out stopped the task before Codex, no MR, the ticket says why" \
  live_report required-check-timeout "(.runner_state == \"failed\") and ((.mrs | length) == 0)
    and ($gate_checks == [\"running\", \"failed\"]) and ([.timeline[] | select(.stage == \"gate.review\")] | length) == 0
    and ([.ticket_comments[] | select(test(\"independent Claude check \\\\(code or security review\\\\) did not run\"))] | length) >= 1"
step "live: findings went back to the agent once, it answered, the checks did not run again, one MR" \
  live_report findings-returned "(.checks.rounds_used == \"1\") and ((.checks.returned | length) >= 1)
    and ((.checks.replies | length) >= 1) and ($gate_checks == [\"running\", \"findings\", \"skipped\", \"skipped\"])
    and .runner_state == \"code_review\" and (.mrs | length) == 1"

printf '\n'
[ "$fails" = 0 ] && { echo "verify: all passed"; exit 0; }
echo "verify: $fails failed"; exit 1
