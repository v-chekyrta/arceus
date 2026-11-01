#!/bin/bash
# The services come back after a reboot: setup's systemd phase enables for good a unit the operator
# started but did not enable, or enabled only with --runtime, and leaves a unit nobody started alone.
# systemctl is a stand-in that keeps each unit's state in files; no root, no systemd needed.
#   bash setup/tests/test-autostart.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }

# The functions under test, taken from setup.sh as they are (sourcing it would run its phases).
eval "$(sed -n '/^not_autostarted() {/,/^}/p; /^check_systemd() {/,/^}/p; /^apply_systemd() {/,/^}/p' "$HERE/../setup.sh")"
type not_autostarted check_systemd apply_systemd >/dev/null || { echo "FAIL the functions are not in setup.sh"; exit 1; }

# What apply_systemd and check_systemd lean on, minus the unit files themselves.
say() { printf '%s\n' "$*" >> "$T/said"; }
put_file() { return 1; }
same_file() { return 0; }
unit_text() { :; }
engine_unit_text() { :; }
running_bulava_tasks() { :; }
UNIT=/dev/null ENGINE_UNIT=/dev/null

# systemctl: $T/<unit>.active (yes|no), $T/<unit>.etc and $T/<unit>.run (enable links, present or not).
systemctl() {
  local quiet=0 runtime=0 a args=()
  for a in "$@"; do case "$a" in --quiet) quiet=1 ;; --runtime) runtime=1 ;; *) args+=("$a") ;; esac; done
  local verb="${args[0]}" u="${args[1]:-}" word rc
  case "$verb" in
    daemon-reload) return 0 ;;
    is-active) [ "$(cat "$T/$u.active")" = yes ] ;;
    is-enabled)
      if [ -e "$T/$u.run" ]; then word=enabled-runtime; rc=0
      elif [ -e "$T/$u.etc" ]; then word=enabled; rc=0
      else word=disabled; rc=1; fi
      [ "$quiet" = 1 ] || printf '%s\n' "$word"; return "$rc" ;;
    enable) if [ "$runtime" = 1 ]; then touch "$T/$u.run"; else touch "$T/$u.etc"; fi ;;
    disable) if [ "$runtime" = 1 ]; then rm -f "$T/$u.run"; else rm -f "$T/$u.etc"; fi ;;
  esac
}
state() {   # $1=unit $2=active yes|no $3=none|etc|runtime
  rm -f "$T/$1.etc" "$T/$1.run"; printf '%s\n' "$2" > "$T/$1.active"
  case "$3" in etc) touch "$T/$1.etc" ;; runtime) touch "$T/$1.run" ;; esac
}

state bulava-engine yes etc; state runner yes etc
check "both running and enabled: nothing to do" '[ -z "$(not_autostarted)" ] && check_systemd'

state runner yes none
check "a running runner that is not enabled is named" '[ "$(not_autostarted)" = runner ] && ! check_systemd'
apply_systemd
check "and enabled for good by the phase" '[ -e "$T/runner.etc" ] && check_systemd && grep -q "runner was running" "$T/said"'

state bulava-engine yes runtime
check "an engine enabled with --runtime is named: it does not outlive the boot" \
  '[ "$(not_autostarted)" = bulava-engine ] && ! check_systemd'
apply_systemd
check "and enabled for good, the runtime link gone" \
  '[ -e "$T/bulava-engine.etc" ] && [ ! -e "$T/bulava-engine.run" ] && check_systemd'

state bulava-engine no none; state runner no none
check "a fresh install nobody started is left to the operator" '[ -z "$(not_autostarted)" ] && check_systemd'
apply_systemd
check "and the phase does not enable it" '[ ! -e "$T/bulava-engine.etc" ] && [ ! -e "$T/runner.etc" ]'

[ "$fails" = 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
