# Where setup takes the Bulava engine from: a local mirror of the public repository that never
# forgets a commit it once installed. Sourced by setup.sh; tests/test-bulava-source.sh runs it alone.
#
# Bulava's history has been rewritten upstream (factory and main squashed into one commit), and a
# commit that no branch reaches any more is not in a fresh clone. A server pinned to it then could not
# be rebuilt. The mirror keeps every commit it fetched under refs/arceus/<commit>, so `git gc` keeps
# it too, and an install of that commit works whatever happened upstream since.

# Make the mirror hold <commit>, fetching from <source> when it does not yet. Fails when neither has it.
bulava_mirror_fetch() {   # $1=mirror (bare repository) $2=source URL $3=full commit
  local m="$1" src="$2" c="$3"
  [ -d "$m" ] || git init -q --bare "$m" || return 1
  if ! git -C "$m" cat-file -e "$c^{commit}" 2>/dev/null; then
    # Branches and tags under refs/upstream/: forced, since upstream rewrites them; refs/arceus/ is ours.
    git -C "$m" fetch -q --no-tags "$src" "+refs/heads/*:refs/upstream/heads/*" \
      "+refs/tags/*:refs/upstream/tags/*" 2>/dev/null || true
  fi
  if ! git -C "$m" cat-file -e "$c^{commit}" 2>/dev/null; then
    git -C "$m" fetch -q --no-tags "$src" "$c" 2>/dev/null || true   # a commit no branch names any more
  fi
  git -C "$m" cat-file -e "$c^{commit}" 2>/dev/null || return 1
  git -C "$m" update-ref "refs/arceus/$c" "$c"
}

# The full checkout of <commit> from the mirror into <dest>, which must not exist yet.
bulava_mirror_checkout() {   # $1=mirror $2=full commit $3=destination
  local m="$1" c="$2" dest="$3"
  git init -q "$dest" \
    && git -C "$dest" fetch -q --no-tags "$m" "refs/arceus/$c" \
    && git -C "$dest" -c advice.detachedHead=false checkout -q --detach FETCH_HEAD \
    && [ "$(git -C "$dest" rev-parse HEAD)" = "$c" ]
}
