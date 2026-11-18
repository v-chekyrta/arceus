#!/bin/bash
# Keep read-only clones of the factory's repositories for the assistant: the assistant reads code here and
# nowhere else. Source: the factory's workspace clones (their origin is GitLab); each mirror tracks the default
# branch and is reset to it, so nothing the agents leave in the workspace shows up here.
#   deploy/mirror.sh <workspace dir> <mirror dir>
set -euo pipefail
SRC=${1:?workspace dir}; DST=${2:?mirror dir}
mkdir -p "$DST"
# A repository may sit one level down (a group's subgroup, e.g. mobile/shop-android); the workspace folder's own
# repository (the runner's notes) is not one of the project's.
for repo in "$SRC"/*/ "$SRC"/*/*/; do
  [ -d "$repo/.git" ] || continue
  name=${repo#"$SRC"/}; name=${name%/}
  mkdir -p "$DST/$(dirname "$name")"
  url=$(git -C "$repo" remote get-url origin 2>/dev/null) || continue
  if [ ! -d "$DST/$name/.git" ]; then
    git clone --quiet --reference-if-able "$repo" "$url" "$DST/$name" || { echo "skip $name: clone failed" >&2; continue; }
  fi
  branch=$(git -C "$DST/$name" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
  branch=${branch:-$(git -C "$repo" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')}
  branch=${branch:-master}
  git -C "$DST/$name" fetch --quiet origin "$branch" || { echo "skip $name: fetch failed" >&2; continue; }
  git -C "$DST/$name" checkout --quiet -B "$branch" "origin/$branch"
  git -C "$DST/$name" reset --quiet --hard "origin/$branch"
  git -C "$DST/$name" clean --quiet -fdx
done
