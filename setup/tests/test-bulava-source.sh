#!/bin/bash
# The engine's source survives a rewrite of Bulava's history: a commit setup installed once stays
# installable from the mirror after upstream force-pushes it away and collects the garbage.
#   bash setup/tests/test-bulava-source.sh            local repositories only
#   bash setup/tests/test-bulava-source.sh --live     also the pinned commit from the real BULAVA_SOURCE
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../bulava-source.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }
g() { git -c user.name=t -c user.email=t@t -c init.defaultBranch=factory "$@"; }

# Upstream with an engine at commit OLD.
g init -q --bare "$T/upstream.git"
g init -q "$T/work"
mkdir -p "$T/work/engine/bin"
printf '#!/bin/sh\n' > "$T/work/engine/bin/dispatch.sh"; chmod 755 "$T/work/engine/bin/dispatch.sh"
g -C "$T/work" add -A && g -C "$T/work" commit -q -m old && g -C "$T/work" push -q "$T/upstream.git" HEAD:factory
OLD="$(git -C "$T/work" rev-parse HEAD)"

check "the first install fetches the commit into the mirror" 'bulava_mirror_fetch "$T/mirror.git" "$T/upstream.git" "$OLD"'
check "and checks it out whole, scripts executable" \
  'bulava_mirror_checkout "$T/mirror.git" "$OLD" "$T/one" && [ -x "$T/one/engine/bin/dispatch.sh" ]'

# Upstream rewrites its history: one new root commit, the old one unreachable and collected.
g init -q "$T/rewrite"
mkdir -p "$T/rewrite/engine/bin"; printf '#!/bin/sh\n# new\n' > "$T/rewrite/engine/bin/dispatch.sh"
g -C "$T/rewrite" add -A && g -C "$T/rewrite" commit -q -m squashed
g -C "$T/rewrite" push -q -f "$T/upstream.git" HEAD:factory
NEW="$(git -C "$T/rewrite" rev-parse HEAD)"
git -C "$T/upstream.git" reflog expire --expire=now --all && git -C "$T/upstream.git" gc -q --prune=now
check "upstream no longer has the old commit" '! git -C "$T/upstream.git" cat-file -e "$OLD^{commit}" 2>/dev/null'
check "a fresh clone of upstream cannot install it" \
  '! { git clone -q "$T/upstream.git" "$T/clone" 2>/dev/null && git -C "$T/clone" checkout -q "$OLD" 2>/dev/null; }'

git -C "$T/mirror.git" fetch -q "$T/upstream.git" "+refs/heads/*:refs/upstream/heads/*"   # the next setup run
git -C "$T/mirror.git" reflog expire --expire=now --all; git -C "$T/mirror.git" gc -q --prune=now
check "the mirror still installs the old commit after a fetch and a gc" \
  'bulava_mirror_fetch "$T/mirror.git" "$T/upstream.git" "$OLD" && bulava_mirror_checkout "$T/mirror.git" "$OLD" "$T/two" \
   && [ "$(git -C "$T/two" rev-parse HEAD)" = "$OLD" ] && ! grep -q new "$T/two/engine/bin/dispatch.sh"'
check "the new commit installs too" \
  'bulava_mirror_fetch "$T/mirror.git" "$T/upstream.git" "$NEW" && bulava_mirror_checkout "$T/mirror.git" "$NEW" "$T/three"'
check "a commit neither has is refused" \
  '! bulava_mirror_fetch "$T/mirror.git" "$T/upstream.git" 0123456789abcdef0123456789abcdef01234567'

if [ "${1:-}" = --live ]; then
  conf="$HERE/../setup.conf.example"
  src="$(sed -n 's/^BULAVA_SOURCE=//p' "$conf")"; ref="$(sed -n 's/^BULAVA_REF=//p' "$conf")"
  check "BULAVA_REF in setup.conf.example is a full commit" '[[ "$ref" =~ ^[0-9a-f]{40}$ ]]'
  check "that commit installs from $src, with the independent checks in its review gate" \
    'bulava_mirror_fetch "$T/live.git" "$src" "$ref" && bulava_mirror_checkout "$T/live.git" "$ref" "$T/live" \
     && [ -x "$T/live/engine/bin/dispatch.sh" ] && grep -q "claude_checks_enabled; then" "$T/live/engine/hooks/review-gate.sh"'
fi

[ "$fails" = 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
