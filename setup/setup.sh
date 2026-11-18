#!/usr/bin/env bash
# Prepare a VM for the Arceus runner and the Bulava engine.
#
#   sudo ./setup.sh                 run every phase that is not done yet
#   sudo ./setup.sh --check         only check, change nothing; exit 1 if something is not done
#   sudo ./setup.sh --phase docker  run one phase
#   ./setup.sh --list               list the phases
#
# Every phase checks the machine first, acts only when the check fails, then checks again.
# Running it twice changes nothing the second time. It never overwrites config.yaml,
# secrets.env, bulava.env, the SQLite database or anything in the workspace.
set -Eeuo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/.." && pwd)"
CONF="${ARCEUS_SETUP_CONF:-$SELF_DIR/setup.conf}"
FACTS_DIR=/var/lib/arceus
PHASES=(packages legacy docker chrome agent_tools mcp_config dirs bulava bulava_install agent_reports agent_notes runner systemd assistant observability console)

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_fail=$'\e[31m'; c_off=$'\e[0m'
[ -t 1 ] || { c_ok=; c_warn=; c_fail=; c_off=; }
say()  { printf '%s\n' "$*"; }
die()  { printf '%ssetup: %s%s\n' "$c_fail" "$*" "$c_off" >&2; exit 1; }
trap 'die "failed at line $LINENO: $BASH_COMMAND"' ERR

usage() { sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

MODE=apply; ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check ;;
    --phase) ONLY="${2:?--phase needs a name}"; shift ;;
    --list)  printf '%s\n' "${PHASES[@]}"; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument $1 (see --help)" ;;
  esac
  shift
done

# --- configuration -----------------------------------------------------------------------------

[ -f "$CONF" ] || die "no $CONF. Copy setup.conf.example to setup.conf and fill it in."
# shellcheck source=setup.conf.example
. "$CONF"

# Who everything runs as is not configured: it is the user who owns this folder and runs this
# script with sudo. A setup.conf from an earlier layout may still name RUN_USER; it must agree.
CONF_RUN_USER="${RUN_USER:-}"
REPO_OWNER="$(stat -c %U "$REPO_DIR")"
RUN_USER="${SUDO_USER:-$REPO_OWNER}"
: "${CLAUDE_CODE_VERSION:?set CLAUDE_CODE_VERSION in setup.conf}"
: "${CODEX_VERSION:?set CODEX_VERSION in setup.conf}"
: "${CHROME_DEVTOOLS_MCP_VERSION:?set CHROME_DEVTOOLS_MCP_VERSION in setup.conf}"
: "${WORKSPACE_MCP_VERSION:=}"
: "${BULAVA_SOURCE:=}"
: "${BULAVA_REF:=}"
: "${RUNNER_EXTRA_PACKAGES:=}"

RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null || true)"
# One folder for everything Arceus on this machine, the one this script lies in (normally ~/arceus):
#   setup/ runner/ config/  this repository (synced from a checkout, replaced on update)
#   data/                   the operator's settings and the runner's state, never replaced
#   workspace/              clones of the project's repositories
ARCEUS_HOME="$REPO_DIR"
DATA_DIR="$ARCEUS_HOME/data"
WORKSPACE="$ARCEUS_HOME/workspace"
OAUTH_ENV="$RUN_HOME/.config/claude/oauth.env"
# A /login sign-in: refreshed by Claude itself and allowed to read the usage meter, which a
# `setup-token` token is not (the meter answers 403, scope user:profile). When it exists it wins.
CLAUDE_LOGIN="$RUN_HOME/.claude/.credentials.json"
TOOLS=/opt/agent-tools
RUNNER_OPT=/opt/arceus-runner
# The wheels of RUNNER_EXTRA_PACKAGES: built once, installed into the runner's virtualenv and into the console's
# image from the same files. EXTRA_INSTALLED lists the distributions setup put there, to take out dropped ones.
EXTRA_DIR="$RUNNER_OPT/extra"
EXTRA_INSTALLED="$RUNNER_OPT/.extra-installed"
BULAVA_DIR=/opt/bulava
MCP_CONFIG=/opt/bulava-local/mcp.json
# Google Sheets, Docs and Drive for the agent, read-only (workspace-mcp): its own venv under TOOLS, and the
# service account's key, put there by hand, in GOOGLE_DIR.
GOOGLE_MCP="$TOOLS/workspace-mcp"
GOOGLE_DIR="$DATA_DIR/google"
CLAUDE_POLICY=/etc/claude-code/managed-settings.json
UNIT=/etc/systemd/system/runner.service
ENGINE_UNIT=/etc/systemd/system/bulava-engine.service
ENGINE="$BULAVA_DIR/engine"
OBS_DIR=/opt/arceus-observability
OBS_SERVICES=(grafana prometheus loki alloy node-exporter)
CONSOLE_DIR=/opt/arceus-console
CONSOLE_APP=127.0.0.1:8180   # the console's own server; nginx in front of it is what people reach
# The project the console and Grafana show. By default this one; a machine that runs a second project by hand
# (its own user, an Arceus folder with data/, its own runner and engine units) may show that one instead:
# CONSOLE_PROJECT_DIR, CONSOLE_RUNNER_UNIT and CONSOLE_ENGINE_UNIT in setup.conf. Its user is the folder's owner.
CONSOLE_PROJECT_DIR="${CONSOLE_PROJECT_DIR:-$ARCEUS_HOME}"
CONSOLE_PROJECT_DIR="${CONSOLE_PROJECT_DIR%/}"
CONSOLE_RUNNER_UNIT="${CONSOLE_RUNNER_UNIT:-runner}"
CONSOLE_ENGINE_UNIT="${CONSOLE_ENGINE_UNIT:-bulava-engine}"
# Other services behind the console's nginx and its sign-in (the factory assistant's /assistant/, for one):
# CONSOLE_EXTRA_LOCATIONS in setup.conf names a file of nginx location blocks. They go into the server block,
# before `location /`, every time setup writes nginx.conf, so an update no longer drops a route someone added
# by hand. The file is read as root and only when setup runs: edit it, then run setup again.
CONSOLE_EXTRA_LOCATIONS="${CONSOLE_EXTRA_LOCATIONS:-}"
CONSOLE_DATA="$CONSOLE_PROJECT_DIR/data"
CONSOLE_USER="$(stat -c %U "$CONSOLE_PROJECT_DIR" 2>/dev/null || true)"
CONSOLE_HOME="$(getent passwd "$CONSOLE_USER" | cut -d: -f6)"
CONSOLE_GROUP="$(id -gn "$CONSOLE_USER" 2>/dev/null || true)"
# Config files mounted into a container one by one, as service:path-inside:file-in-OBS_DIR. An update
# replaces the file on disk, but the container keeps reading the one it started with.
OBS_FILE_MOUNTS=(prometheus:/etc/prometheus/prometheus.yml:prometheus.yml loki:/etc/loki/loki.yaml:loki.yaml
                 alloy:/etc/alloy/config.alloy:config.alloy)
DOCKER_KEY_FPR=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
# poppler-utils: Claude Code renders the pages of a PDF read in parts (Read's `pages`) with its pdftoppm.
APT_PKGS=(git tmux jq curl ca-certificates gnupg rsync unzip sqlite3 python3-venv python3-pip nodejs npm ffmpeg poppler-utils)

preflight() {
  [ "$(id -u)" = 0 ] || die "run it with sudo"
  . /etc/os-release
  [ "${ID:-}" = ubuntu ] || die "Ubuntu only, this is ${ID:-unknown}"
  case "${VERSION_ID:-}" in 24.04|26.04) ;; *) die "Ubuntu 24.04 or 26.04 only, this is ${VERSION_ID:-?}" ;; esac
  [ "$(uname -m)" = x86_64 ] || die "x86_64 only: Chrome ships no .deb for $(uname -m)"
  # Everything runs as the admin who runs this script (or RUN_USER in setup.conf). Setup creates no
  # users and never changes anyone's sudo.
  [ "$RUN_USER" != root ] || die "the runner and Claude must not run as root: put this folder in a normal user's home and run 'sudo ./setup.sh' as that user"
  if [ "$RUN_USER" != "$REPO_OWNER" ]; then
    [ "$REPO_OWNER" = root ] \
      && die "$REPO_DIR belongs to root. Put the folder in your own home (normally ~/arceus) and run 'sudo ./setup.sh' there."
    die "$REPO_DIR belongs to '$REPO_OWNER' but setup was run by '$RUN_USER'. Run it as '$REPO_OWNER', or keep a copy in your own home."
  fi
  [ -z "$CONF_RUN_USER" ] || [ "$CONF_RUN_USER" = "$RUN_USER" ] \
    || die "setup.conf says RUN_USER=$CONF_RUN_USER, but everything runs as the user who runs setup ('$RUN_USER'). Remove RUN_USER from setup.conf."
  if [ "$CONSOLE_PROJECT_DIR" != "$ARCEUS_HOME" ]; then
    [ -n "$CONSOLE_USER" ] && [ "$CONSOLE_USER" != root ] && [ -d "$CONSOLE_DATA" ] && [ -f "$CONSOLE_DATA/config.yaml" ] \
      || die "CONSOLE_PROJECT_DIR=$CONSOLE_PROJECT_DIR in setup.conf: it must be a normal user's Arceus folder with data/config.yaml"
    local u
    for u in "$CONSOLE_RUNNER_UNIT" "$CONSOLE_ENGINE_UNIT"; do
      [ "$(systemctl show "$u" -p LoadState --value 2>/dev/null)" = loaded ] \
        || die "setup.conf names the unit $u for the console's project, but systemd has no such unit"
    done
  fi
  # Named and missing is refused rather than skipped: skipped, the route would vanish from nginx without a word.
  [ -z "$CONSOLE_EXTRA_LOCATIONS" ] || { [ -f "$CONSOLE_EXTRA_LOCATIONS" ] && [ -r "$CONSOLE_EXTRA_LOCATIONS" ]; } \
    || die "CONSOLE_EXTRA_LOCATIONS=$CONSOLE_EXTRA_LOCATIONS in setup.conf: no such readable file"
  [ -n "$RUN_HOME" ] && [ -d "$RUN_HOME" ] || die "user '$RUN_USER' has no home folder"
  # The services are machine-wide. Setup never moves them from one user to another behind his back.
  local unit owner
  for unit in "$UNIT" "$ENGINE_UNIT"; do
    [ -f "$unit" ] || continue   # a machine setup has not run on yet
    owner="$(sed -n 's/^User=//p' "$unit" | head -1)"
    if [ -n "$owner" ] && [ "$owner" != "$RUN_USER" ] && id "$owner" >/dev/null 2>&1; then
      die "$(basename "$unit") runs as '$owner'. Stop and remove it there first (sudo systemctl disable --now $(basename "$unit" .service); sudo rm $unit), then run setup as '$RUN_USER'."
    fi
  done
}


# Write a file only when its content differs. Returns 0 if it changed.
put_file() {  # path mode owner content
  local path="$1" mode="$2" owner="$3" content="$4" tmp
  if [ -f "$path" ] && [ "$(cat "$path")" = "$content" ] \
     && [ "$(stat -c '%a %U:%G' "$path")" = "$mode $owner" ]; then
    return 1
  fi
  install -d -m 755 "$(dirname "$path")"
  tmp="$(mktemp "$path.XXXXXX")"
  printf '%s\n' "$content" > "$tmp"
  chmod "$mode" "$tmp"; chown "$owner" "$tmp"
  mv -f "$tmp" "$path"
}

same_file() {  # path content mode owner
  [ -f "$1" ] && [ "$(cat "$1")" = "$2" ] && [ "$(stat -c '%a %U:%G' "$1")" = "$3 $4" ]
}

# --- phases: check_<name> answers "done?", apply_<name> makes it so -----------------------------

check_packages() {
  local p
  for p in "${APT_PKGS[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -qx 'install ok installed' || return 1
  done
}
apply_packages() {
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${APT_PKGS[@]}"
}

# An earlier setup wrote Claude Code managed settings (deny git push, deny the runner's files) for a separate
# agent user. They are machine-wide, so with the runner on the admin's own account they would
# also bind the admin's interactive Claude. Bulava's server profile gives the workers these denies
# itself (SUPERVISOR_SERVER_EXTRA_SECRET_PATHS covers ~/arceus/data). Only that file of ours is removed.
ours_policy() { [ -f "$CLAUDE_POLICY" ] && grep -q '"Bash(git push)"' "$CLAUDE_POLICY" && grep -q DISABLE_AUTOUPDATER "$CLAUDE_POLICY"; }
# Earlier layouts kept the runner's settings in ~/runner and the clones in ~/workspace. They move into
# ~/arceus/data and ~/arceus/workspace; a file already at the new place is never overwritten.
OLD_DATA="$RUN_HOME/runner"
OLD_WORKSPACE="$RUN_HOME/workspace"
DATA_FILES=(config.yaml secrets.env bulava.env runner.db runner.db-wal runner.db-shm runner.lock events.jsonl)
old_data_left() {
  local f
  for f in "${DATA_FILES[@]}"; do
    { [ -e "$OLD_DATA/$f" ] || [ -L "$OLD_DATA/$f" ]; } && [ ! -e "$DATA_DIR/$f" ] && [ ! -L "$DATA_DIR/$f" ] && return 0
  done
  return 1
}
config_points_at_old_paths() {
  [ -f "$DATA_DIR/config.yaml" ] && grep -qE "(~|$RUN_HOME)/runner/|^[[:space:]]*workspace:[[:space:]]*(~|$RUN_HOME)/workspace([[:space:]]|$)" "$DATA_DIR/config.yaml"
}
old_workspace_empty() { [ -d "$OLD_WORKSPACE" ] && [ -z "$(ls -A "$OLD_WORKSPACE")" ]; }
check_legacy() { ! ours_policy && ! old_data_left && ! old_workspace_empty && ! config_points_at_old_paths; }
apply_legacy() {
  if ours_policy; then
    rm -f "$CLAUDE_POLICY"; rmdir --ignore-fail-on-non-empty "$(dirname "$CLAUDE_POLICY")" 2>/dev/null || true
  fi
  if old_data_left; then
    systemctl is-active -q runner && die "stop the runner first: sudo systemctl stop runner"
    install -d -m 755 -o "$RUN_USER" -g "$RUN_GROUP" "$ARCEUS_HOME"
    install -d -m 700 -o "$RUN_USER" -g "$RUN_GROUP" "$DATA_DIR"
    local f
    for f in "${DATA_FILES[@]}"; do
      if { [ -e "$OLD_DATA/$f" ] || [ -L "$OLD_DATA/$f" ]; } && [ ! -e "$DATA_DIR/$f" ] && [ ! -L "$DATA_DIR/$f" ]; then
        mv "$OLD_DATA/$f" "$DATA_DIR/$f"; say "  moved ~/runner/$f -> ~/arceus/data/$f"
      fi
    done
    rmdir "$OLD_DATA" 2>/dev/null && say "  removed the empty ~/runner" || say "  ~/runner still has files of its own: left in place"
  fi
  if config_points_at_old_paths; then
    # The files moved, so the paths in config.yaml that name them move with them. Nothing else changes.
    sed -i -E -e "s#(~|$RUN_HOME)/runner/#~/arceus/data/#g" \
              -e "s#^([[:space:]]*workspace:[[:space:]]*)(~|$RUN_HOME)/workspace([[:space:]]|\$)#\1~/arceus/workspace\3#" \
              "$DATA_DIR/config.yaml"
    say "  config.yaml: paths now point at ~/arceus/data and ~/arceus/workspace"
  fi
  if old_workspace_empty; then
    rmdir "$OLD_WORKSPACE"; say "  removed the empty ~/workspace (clones now go to ~/arceus/workspace)"
  fi
}

check_docker() {
  command -v docker >/dev/null && docker compose version >/dev/null 2>&1 \
    && systemctl is-active -q docker && id -nG "$RUN_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker
}
apply_docker() {
  local key=/etc/apt/keyrings/docker.asc codename tmp
  codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
  if [ ! -s "$key" ]; then
    tmp="$(mktemp)"
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$tmp"
    gpg --show-keys --with-colons "$tmp" | grep -q "^fpr:::::::::$DOCKER_KEY_FPR:" \
      || die "Docker signing key fingerprint is not $DOCKER_KEY_FPR"
    install -D -m 644 "$tmp" "$key"; rm -f "$tmp"
  fi
  put_file /etc/apt/sources.list.d/docker.list 644 root:root \
    "deb [arch=amd64 signed-by=$key] https://download.docker.com/linux/ubuntu $codename stable" || true
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
  usermod -aG docker "$RUN_USER"
}

check_chrome() { command -v google-chrome-stable >/dev/null && google-chrome-stable --version >/dev/null 2>&1; }
apply_chrome() {
  local dir; dir="$(mktemp -d)"
  curl -fsSL -o "$dir/chrome.deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  dpkg-deb -f "$dir/chrome.deb" Package | grep -qx google-chrome-stable || die "downloaded file is not the Chrome package"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$dir/chrome.deb"
  rm -rf "$dir"
}

npm_pkgs() {
  printf '%s\n' "@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION" "@openai/codex@$CODEX_VERSION" \
    "chrome-devtools-mcp@$CHROME_DEVTOOLS_MCP_VERSION"
}
installed_npm_version() { jq -r .version "$TOOLS/lib/node_modules/$1/package.json" 2>/dev/null || true; }
# Copies of claude and codex outside $TOOLS: Claude's native installer (it updates itself), and `npm i -g`
# or `codex update` under the user's nvm. A shell that finds them first runs a version nobody pinned,
# and `claude update` / `codex update` update them instead of the pinned ones.
stray_agent_tools() {
  local p
  for p in "$RUN_HOME/.local/bin/claude" "$RUN_HOME/.local/bin/codex" "$RUN_HOME/.local/share/claude" \
           "$RUN_HOME"/.nvm/versions/node/*/bin/claude "$RUN_HOME"/.nvm/versions/node/*/bin/codex \
           "$RUN_HOME"/.nvm/versions/node/*/lib/node_modules/@anthropic-ai/claude-code \
           "$RUN_HOME"/.nvm/versions/node/*/lib/node_modules/@openai/codex; do
    if [ -e "$p" ] || [ -L "$p" ]; then printf '%s\n' "$p"; fi
  done
}
remove_stray_agent_tools() {
  local p
  while IFS= read -r p; do
    [ -n "$p" ] && rm -rf -- "$p" && say "  removed $p: claude and codex are the pinned ones in $TOOLS"
  done < <(stray_agent_tools)
}
check_agent_tools() {
  [ -z "$(stray_agent_tools)" ] || return 1
  [ "$(installed_npm_version @anthropic-ai/claude-code)" = "$CLAUDE_CODE_VERSION" ] || return 1
  [ "$(installed_npm_version @openai/codex)" = "$CODEX_VERSION" ] || return 1
  [ "$(installed_npm_version chrome-devtools-mcp)" = "$CHROME_DEVTOOLS_MCP_VERSION" ] || return 1
  local b; for b in claude codex chrome-devtools-mcp; do
    [ "$(readlink "/usr/local/bin/$b")" = "$TOOLS/bin/$b" ] || return 1
  done
  [ -z "$WORKSPACE_MCP_VERSION" ] || [ "$(installed_workspace_mcp)" = "$WORKSPACE_MCP_VERSION" ] || return 1
  [ -z "$(find "$TOOLS" -perm -o+w -not -type l -print -quit)" ] && [ "$(stat -c %U "$TOOLS")" = root ]
}
installed_workspace_mcp() {
  "$GOOGLE_MCP/bin/python" -I -c 'import importlib.metadata as m; print(m.version("workspace-mcp"))' 2>/dev/null || true
}
apply_agent_tools() {
  # Root-owned prefix: the workers can run these tools but not replace them by accident.
  install -d -m 755 -o root -g root "$TOOLS"
  mapfile -t pkgs < <(npm_pkgs)
  npm install --global --prefix "$TOOLS" --no-fund --no-audit "${pkgs[@]}"
  chown -R root:root "$TOOLS"; chmod -R go-w "$TOOLS"
  local b; for b in claude codex chrome-devtools-mcp; do ln -sfn "$TOOLS/bin/$b" "/usr/local/bin/$b"; done
  remove_stray_agent_tools
  if [ -n "$WORKSPACE_MCP_VERSION" ]; then
    rm -rf "$GOOGLE_MCP"   # a clean venv: pip would otherwise keep whatever an older version pulled in
    python3 -m venv "$GOOGLE_MCP"
    "$GOOGLE_MCP/bin/pip" install --quiet --disable-pip-version-check "workspace-mcp==$WORKSPACE_MCP_VERSION"
    chown -R root:root "$GOOGLE_MCP"; chmod -R go-w "$GOOGLE_MCP"
  fi
}

# The agent gets Google once the server is installed and a service account's key is in GOOGLE_DIR. The account reads
# what is shared with its address, the key does not expire, and the server acts as the account itself
# (USER_GOOGLE_EMAIL is its own address; no domain-wide delegation).
mcp_json() {
  local google=null key="$GOOGLE_DIR/service-account.json" who
  if [ -n "$WORKSPACE_MCP_VERSION" ] && [ -f "$key" ]; then
    who="$(jq -r 'select(.type == "service_account") | .client_email // empty' "$key" 2>/dev/null)"
    # Called inside $(...): a die here would only end the subshell, and an empty mcp.json would be written.
    [ -n "$who" ] && google="$(jq -n --arg cmd "$GOOGLE_MCP/bin/workspace-mcp" --arg key "$key" --arg who "$who" \
      '{command: $cmd, args: ["--read-only", "--tools", "sheets", "docs", "drive", "--disabled-tools", "start_google_auth"],
        env: {GOOGLE_SERVICE_ACCOUNT_KEY_FILE: $key, USER_GOOGLE_EMAIL: $who}}')"
    [ -n "$who" ] || printf '%s is not a service account key (type service_account, client_email): no Google\n' "$key" >&2
  fi
  jq -n --arg cmd "$TOOLS/bin/chrome-devtools-mcp" --argjson google "$google" \
    '{mcpServers: ({browser: {command: $cmd, args: ["--headless", "--isolated", "--no-usage-statistics", "--executablePath", "/usr/bin/google-chrome-stable"]}}
                   + (if $google then {google: $google} else {} end))}'
}
check_mcp_config() { same_file "$MCP_CONFIG" "$(mcp_json)" 644 root:root; }
apply_mcp_config() { put_file "$MCP_CONFIG" 644 root:root "$(mcp_json)" || true; }

check_dirs() {
  [ "$(stat -c '%U' "$ARCEUS_HOME" 2>/dev/null)" = "$RUN_USER" ] \
    && [ "$(stat -c '%U' "$WORKSPACE" 2>/dev/null)" = "$RUN_USER" ] \
    && [ "$(stat -c '%U %a' "$DATA_DIR" 2>/dev/null)" = "$RUN_USER 700" ] \
    && [ "$(stat -c '%U %a' "$GOOGLE_DIR" 2>/dev/null)" = "$RUN_USER 700" ]
}
apply_dirs() {
  [ -d "$ARCEUS_HOME" ] || install -d -m 755 -o "$RUN_USER" -g "$RUN_GROUP" "$ARCEUS_HOME"
  chown "$RUN_USER:$RUN_GROUP" "$ARCEUS_HOME"
  [ -d "$WORKSPACE" ] || install -d -m 755 -o "$RUN_USER" -g "$RUN_GROUP" "$WORKSPACE"
  install -d -m 700 -o "$RUN_USER" -g "$RUN_GROUP" "$DATA_DIR"
  install -d -m 700 -o "$RUN_USER" -g "$RUN_GROUP" "$GOOGLE_DIR"
}

bulava_wanted() { [ -n "$BULAVA_SOURCE" ]; }
# shellcheck source=bulava-source.sh
. "$SELF_DIR/bulava-source.sh"
BULAVA_MIRROR="$FACTS_DIR/bulava.git"   # every engine commit installed here stays installable
# The engine is a commit of the public repository (engine/SERVER.md), root-owned and read-only for
# the run user. It runs from $BULAVA_DIR/engine. BULAVA_REF is a full commit, a tag, or a branch the
# server follows: a branch is read on every run, and a new commit on it is what makes this phase act.
bulava_stamp() { printf '%s@%s\n' "$BULAVA_SOURCE" "$BULAVA_REF"; }
installed_bulava_commit() { sed -n 's/^commit //p' "$ENGINE/.arceus-source" 2>/dev/null; }
BULAVA_TARGET=""
bulava_target() {   # the commit BULAVA_REF names now; asked once per run
  if [ -z "$BULAVA_TARGET" ]; then
    if [[ "$BULAVA_REF" =~ ^[0-9a-f]{40}$ ]]; then
      BULAVA_TARGET="$BULAVA_REF"
    else
      BULAVA_TARGET="$(git ls-remote "$BULAVA_SOURCE" "refs/heads/$BULAVA_REF" "refs/tags/$BULAVA_REF^{}" \
        "refs/tags/$BULAVA_REF" 2>/dev/null | awk 'NR == 1 {print $1}')"
    fi
  fi
  printf '%s\n' "$BULAVA_TARGET"
}
check_bulava() {
  bulava_wanted || return 0
  [ -x "$ENGINE/bin/dispatch.sh" ] || return 1
  [ "$(head -1 "$ENGINE/.arceus-source" 2>/dev/null)" = "$(bulava_stamp)" ] || return 1
  if [ ! -d "$BULAVA_SOURCE" ]; then
    local want; want="$(bulava_target)"
    [ -n "$want" ] || { say "  cannot read $BULAVA_REF from $BULAVA_SOURCE: keeping the installed engine"; return 0; }
    [ "$(installed_bulava_commit)" = "$want" ] || return 1
  fi
  [ "$(stat -c %U "$BULAVA_DIR")" = root ] && [ -z "$(find "$BULAVA_DIR" -perm /022 -not -type l -print -quit)" ]
}
# The engine is only ever swapped between tasks (engine/SERVER.md): never under a running attempt.
running_bulava_tasks() {
  [ -x "$RUN_HOME/.local/bin/night-shift" ] || return 0
  runuser -u "$RUN_USER" -- env HOME="$RUN_HOME" "$RUN_HOME/.local/bin/night-shift" task list --json 2>/dev/null \
    | jq -r '(if type == "array" then . else (.tasks // []) end)[] | select(.state == "running") | .task_id' 2>/dev/null
}
apply_bulava() {
  bulava_wanted || return 0
  local stage commit busy; stage="$(mktemp -d)"
  busy="$(running_bulava_tasks | tr '\n' ' ')"
  [ -z "${busy// /}" ] || die "Bulava has a task running ($busy): update the engine between tasks"
  if [ -d "$BULAVA_SOURCE" ]; then
    rsync -a --delete --exclude .git "$BULAVA_SOURCE/" "$stage/repo/"
    commit="local copy"
  else
    [ -n "$BULAVA_REF" ] || die "BULAVA_REF must name a commit, a tag or a branch when BULAVA_SOURCE is a git URL"
    commit="$(bulava_target)"
    [ -n "$commit" ] || die "$BULAVA_SOURCE has no branch or tag $BULAVA_REF"
    install -d -m 755 "$FACTS_DIR"
    bulava_mirror_fetch "$BULAVA_MIRROR" "$BULAVA_SOURCE" "$commit" \
      || die "neither $BULAVA_SOURCE nor the mirror $BULAVA_MIRROR has commit $commit"
    bulava_mirror_checkout "$BULAVA_MIRROR" "$commit" "$stage/repo" || die "cannot check out $commit from $BULAVA_MIRROR"
  fi
  [ -x "$stage/repo/engine/bin/dispatch.sh" ] || die "$BULAVA_SOURCE does not look like Bulava (no engine/bin/dispatch.sh)"
  # Only the engine is needed on a server; the app, the phone app and the site stay out.
  install -d "$stage/opt"; mv "$stage/repo/engine" "$stage/opt/engine"
  { bulava_stamp; printf 'commit %s\n' "$commit"; } > "$stage/opt/engine/.arceus-source"
  chown -R root:root "$stage/opt"; chmod -R u+rwX,go+rX,go-w "$stage/opt"; chmod 755 "$stage/opt"
  rm -rf "$BULAVA_DIR.old"; [ -e "$BULAVA_DIR" ] && mv "$BULAVA_DIR" "$BULAVA_DIR.old"
  mv "$stage/opt" "$BULAVA_DIR"; rm -rf "$stage" "$BULAVA_DIR.old"
}

# The engine's own install.sh, run as the run user: hooks into ~/.claude, night-shift into ~/.local/bin.
# Run again whenever the pinned engine changes.
INSTALLED_STAMP=/var/lib/arceus/bulava-installed
engine_stamp() { head -2 "$ENGINE/.arceus-source" 2>/dev/null | tr '\n' ' '; }
# Bulava's own tools (web-shot, capture) save screenshots in ~/.claude/supervisor/reports/<run>/, but
# the engine opens only the run's instance folder to the agent, so Claude asks before reading them, and
# on a server nobody answers ("Nobody is at the computer"). Until the engine opens that folder itself
# the agent may read it: read only, nothing else.
REPORTS_READ_RULE() { printf 'Read(/%s/.claude/supervisor/reports/**)' "$RUN_HOME"; }
reports_readable() {
  jq -e --arg r "$(REPORTS_READ_RULE)" '(.permissions.allow // []) | index($r)' "$RUN_HOME/.claude/settings.json" >/dev/null 2>&1
}
check_agent_reports() { ! bulava_wanted || reports_readable; }
apply_agent_reports() {
  bulava_wanted || return 0
  local f="$RUN_HOME/.claude/settings.json" tmp
  tmp="$(mktemp)"
  { [ -s "$f" ] && cat "$f" || echo '{}'; } \
    | jq --arg r "$(REPORTS_READ_RULE)" '.permissions.allow = (((.permissions.allow // []) + [$r]) | unique)' > "$tmp"
  install -m 600 -o "$RUN_USER" -g "$RUN_GROUP" "$tmp" "$f"; rm -f "$tmp"
  say "  the agent may read Bulava's screenshots: $(REPORTS_READ_RULE)"
}
# Notes for the agent from the team that runs the server (config/agent-notes.md): Claude Code reads
# the user's ~/.claude/CLAUDE.md in every session, the engine's workers included. Only the block
# between the markers is ours; anything else in the file is left as it is.
NOTES_BEGIN="<!-- arceus:agent-notes:begin -->"
NOTES_END="<!-- arceus:agent-notes:end -->"
notes_block() { printf '%s\n%s\n%s\n' "$NOTES_BEGIN" "$(cat "$REPO_DIR/config/agent-notes.md")" "$NOTES_END"; }
current_notes() {
  sed -n "/^${NOTES_BEGIN}\$/,/^${NOTES_END}\$/p" "$RUN_HOME/.claude/CLAUDE.md" 2>/dev/null
}
check_agent_notes() { [ "$(current_notes)" = "$(notes_block)" ]; }
apply_agent_notes() {
  local f="$RUN_HOME/.claude/CLAUDE.md" tmp
  install -d -m 700 -o "$RUN_USER" -g "$RUN_GROUP" "$RUN_HOME/.claude"
  tmp="$(mktemp)"
  if [ -s "$f" ]; then
    sed "/^${NOTES_BEGIN}\$/,/^${NOTES_END}\$/d" "$f" > "$tmp"
    [ -z "$(tail -c1 "$tmp")" ] || printf '\n' >> "$tmp"
  fi
  notes_block >> "$tmp"
  install -m 644 -o "$RUN_USER" -g "$RUN_GROUP" "$tmp" "$f"; rm -f "$tmp"
}

check_bulava_install() {
  bulava_wanted || return 0
  [ -n "$(engine_stamp)" ] && [ "$(cat "$INSTALLED_STAMP" 2>/dev/null)" = "$(engine_stamp)" ] || return 1
  [ "$(readlink "$RUN_HOME/.local/bin/night-shift")" = "$ENGINE/bin/night-shift.sh" ] || return 1
  grep -q "$ENGINE/hooks/" "$RUN_HOME/.claude/supervisor/worker-settings.json" 2>/dev/null
}
apply_bulava_install() {
  bulava_wanted || return 0
  (cd /tmp && runuser -u "$RUN_USER" -- env HOME="$RUN_HOME" \
    PATH="/usr/local/bin:$RUN_HOME/.local/bin:/usr/bin:/bin" bash "$ENGINE/install.sh") | sed 's/^/  install.sh: /'
  install -d -m 755 "$(dirname "$INSTALLED_STAMP")"
  engine_stamp > "$INSTALLED_STAMP"
}

# Python packages of this install's own beside the runner (setup.conf RUNNER_EXTRA_PACKAGES): a tracker, a code
# host or an advisory register kept outside this repository (docs/integrations.md), one pip requirement each.
extra_packages() {
  local list
  read -ra list <<< "${RUNNER_EXTRA_PACKAGES//$'\n'/ }"   # words: spaces and new lines alike
  [ "${#list[@]}" = 0 ] || printf '%s\n' "${list[@]}"
}
# What the extra packages are, for the hashes below: each requirement, and for a folder on this machine the
# contents of its files too, so an edit there installs again as a changed list does (a new commit in a pinned URL).
extra_fingerprint() {
  local req
  extra_packages | while IFS= read -r req; do
    printf '%s\n' "$req"
    [ -d "$req" ] || continue
    (cd "$req" && find . -type f ! -path './.git/*' ! -path '*/__pycache__/*' ! -name '*.pyc' \
      ! -path './build/*' ! -path './dist/*' ! -path '*.egg-info*' -print0 | sort -z | xargs -0 sha256sum)
  done
}
# A hash of what goes into an install, with the extra packages when there are any. Without them it is the hash of
# the sources alone, as before the setting existed.
with_extra() {  # $1: the sources' hash
  if [ -z "$(extra_packages)" ]; then printf '%s\n' "$1"
  else { printf '%s\n' "$1"; extra_fingerprint; } | sha256sum | cut -d' ' -f1
  fi
}
extra_names() {  # the distributions of the wheels in EXTRA_DIR, one per line
  local w
  for w in "$EXTRA_DIR"/*.whl; do [ -e "$w" ] && basename "$w" | cut -d- -f1; done
  return 0
}
extra_installed_ok() {
  local name
  [ "$(extra_names)" = "$(cat "$EXTRA_INSTALLED" 2>/dev/null || true)" ] || return 1
  for name in $(extra_names); do
    "$RUNNER_OPT/venv/bin/pip" show --quiet "$name" >/dev/null 2>&1 || return 1
  done
}
# Builds the wheels with the runner's pip, without their dependencies (a module uses what the runner has:
# httpx, pydantic, PyYAML), installs them, and uninstalls the ones setup put there before that left the list.
# A folder is built from a copy without what an earlier build left in it (build/, dist/, *.egg-info): a backend
# that reuses build/lib could put a module since removed back into the wheel, and root would write into the folder.
apply_extra_packages() {
  local pip="$RUNNER_OPT/venv/bin/pip" name packages=() reqs=() req copy work now
  rm -rf "$EXTRA_DIR"
  install -d -m 755 -o root -g root "$EXTRA_DIR"
  read -ra packages <<< "$(extra_packages | tr '\n' ' ')"
  if [ "${#packages[@]}" -gt 0 ]; then
    say "  extra packages (RUNNER_EXTRA_PACKAGES): ${packages[*]}"
    work="$(mktemp -d)"
    for req in "${packages[@]}"; do
      if [ -d "$req" ]; then
        copy="$work/${#reqs[@]}-$(basename "$req")"
        rsync -a --exclude /build/ --exclude /dist/ --exclude '*.egg-info' --exclude __pycache__/ "$req/" "$copy/"
        reqs+=("$copy")
      else
        reqs+=("$req")
      fi
    done
    "$pip" wheel --quiet --disable-pip-version-check --no-deps --wheel-dir "$EXTRA_DIR" "${reqs[@]}" \
      || { rm -rf "$work"; die "RUNNER_EXTRA_PACKAGES: pip could not fetch or build ${packages[*]}"; }
    rm -rf "$work"
    "$pip" install --quiet --disable-pip-version-check --no-deps --force-reinstall "$EXTRA_DIR"/*.whl \
      || die "RUNNER_EXTRA_PACKAGES: pip could not install the wheels in $EXTRA_DIR"
  fi
  now="$(extra_names)"
  for name in $(cat "$EXTRA_INSTALLED" 2>/dev/null || true); do
    grep -qxF "$name" <<< "$now" && continue
    "$pip" uninstall --quiet --yes "$name" >/dev/null 2>&1 || true
    say "  removed $name: no longer in RUNNER_EXTRA_PACKAGES"
  done
  printf '%s' "$now" > "$EXTRA_INSTALLED"
}

runner_source_hash() {
  with_extra "$(cd "$REPO_DIR/runner" && find pyproject.toml src -type f \( -name '*.py' -o -name '*.toml' \) -print0 \
    | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"
}
# The marker only says what was last handed to pip; this compares what pip actually installed.
runner_installed_matches() {
  local pkg
  pkg="$("$RUNNER_OPT/venv/bin/python" -c 'import arceus_runner, os; print(os.path.dirname(arceus_runner.__file__))' 2>/dev/null)" \
    || return 1
  diff -rq -x __pycache__ "$REPO_DIR/runner/src/arceus_runner" "$pkg" >/dev/null
}
check_runner() {
  [ -x "$RUNNER_OPT/venv/bin/arceus-runner" ] || return 1
  [ "$(cat "$RUNNER_OPT/.source-sha256" 2>/dev/null)" = "$(runner_source_hash)" ] || return 1
  runner_installed_matches || return 1
  extra_installed_ok || return 1
  [ -f "$DATA_DIR/config.yaml" ] && [ -f "$DATA_DIR/secrets.env" ] && [ -f "$DATA_DIR/bulava.env" ] \
    && [ -f "$DATA_DIR/project-notes.md" ] || return 1
  ! grep -q '^CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-replace-me$' "$DATA_DIR/bulava.env" || return 1
  [ "$(stat -c '%a %U' "$DATA_DIR/secrets.env")" = "600 $RUN_USER" ] \
    && [ "$(stat -L -c '%a %U' "$DATA_DIR/bulava.env")" = "600 $RUN_USER" ] || return 1
  # With a /login sign-in, no link to the setup-token file: its token would override the sign-in.
  if [ -s "$CLAUDE_LOGIN" ]; then
    [ "$(readlink "$DATA_DIR/bulava.env")" != "$OAUTH_ENV" ]
  else
    # Otherwise linked to the shell's token file whenever that exists and bulava.env holds nothing of its own.
    [ ! -s "$OAUTH_ENV" ] || [ -L "$DATA_DIR/bulava.env" ] || grep -qvE '^[[:space:]]*(#|$)' "$DATA_DIR/bulava.env"
  fi
}
apply_runner() {
  install -d -m 755 -o root -g root "$RUNNER_OPT"
  rsync -a --delete --exclude '__pycache__' --exclude '*.egg-info' \
    "$REPO_DIR/runner/pyproject.toml" "$REPO_DIR/runner/src" "$RUNNER_OPT/src/"
  [ -x "$RUNNER_OPT/venv/bin/python" ] || python3 -m venv "$RUNNER_OPT/venv"
  # rsync keeps the source's mtimes, so a build/ left by the previous install can look newer than an
  # updated module, and setuptools would then install the old copy. Every install builds from scratch.
  rm -rf "$RUNNER_OPT/src/build" "$RUNNER_OPT/src/src/"*.egg-info
  "$RUNNER_OPT/venv/bin/pip" install --quiet --disable-pip-version-check --upgrade "$RUNNER_OPT/src"
  apply_extra_packages
  runner_source_hash > "$RUNNER_OPT/.source-sha256"
  chown -R root:root "$RUNNER_OPT"; chmod -R go-w "$RUNNER_OPT"
  # Examples are copied once. Whatever is there already is the operator's and stays untouched.
  local target example
  for target in config.yaml:config.example.yaml secrets.env:secrets.env.example bulava.env:bulava.env.example \
      project-notes.md:project-notes.example.md qa/instructions.md:qa-instructions.example.md \
      architect/instructions.md:architect-instructions.example.md \
      secops/instructions.md:secops-instructions.example.md; do
    example="${target#*:}"; target="${target%%:*}"
    if [ ! -e "$DATA_DIR/$target" ]; then
      install -d -m 700 -o "$RUN_USER" -g "$RUN_GROUP" "$(dirname "$DATA_DIR/$target")"
      install -m 600 -o "$RUN_USER" -g "$RUN_GROUP" "$REPO_DIR/config/$example" "$DATA_DIR/$target"
      [ "$target" != config.yaml ] || [ "$ARCEUS_HOME" = "$RUN_HOME/arceus" ] \
        || sed -i "s#~/arceus/#$ARCEUS_HOME/#g" "$DATA_DIR/$target"
      if [ "$target" = bulava.env ]; then say "  created $DATA_DIR/$target (optional, stays empty with a /login sign-in)"
      elif [ "$target" = project-notes.md ]; then say "  created $DATA_DIR/$target: the project's notes for the agent, comments only until written"
      elif [ "$target" = qa/instructions.md ]; then say "  created $DATA_DIR/$target: instructions for test runs of the platform (qa:), comments only until written"
      elif [ "$target" = architect/instructions.md ]; then say "  created $DATA_DIR/$target: the team's instructions for architecture reviews (architect:), optional, comments only"
      elif [ "$target" = secops/instructions.md ]; then say "  created $DATA_DIR/$target: the team's notes on its stack for advisory triage (secops:), optional, comments only"
      else say "  created $DATA_DIR/$target from $example: fill it in"; fi
    fi
  done
  # An untouched example token from an earlier setup would override the user's real sign-in.
  if grep -q '^CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-replace-me$' "$DATA_DIR/bulava.env"; then
    install -m 600 -o "$RUN_USER" -g "$RUN_GROUP" "$REPO_DIR/config/bulava.env.example" "$DATA_DIR/bulava.env"
    say "  bulava.env still had the example token: replaced with the new example (sign-in via /login)"
  fi
  # The user's shell already gets its Claude token from ~/.config/claude/oauth.env (a `setup-token`
  # token). Services do not read ~/.bashrc, so the engine is pointed at the same file instead of a
  # copy of the token; only while bulava.env is still the untouched example.
  if [ -s "$CLAUDE_LOGIN" ] && [ "$(readlink "$DATA_DIR/bulava.env")" = "$OAUTH_ENV" ]; then
    rm -f "$DATA_DIR/bulava.env"
    install -m 600 -o "$RUN_USER" -g "$RUN_GROUP" "$REPO_DIR/config/bulava.env.example" "$DATA_DIR/bulava.env"
    say "  Claude is signed in with /login: bulava.env no longer points at the setup-token file"
  fi
  if [ ! -s "$CLAUDE_LOGIN" ] && [ -s "$OAUTH_ENV" ] && [ ! -L "$DATA_DIR/bulava.env" ] \
     && ! grep -qvE '^[[:space:]]*(#|$)' "$DATA_DIR/bulava.env"; then
    ln -sfn "$OAUTH_ENV" "$DATA_DIR/bulava.env"; chown -h "$RUN_USER:$RUN_GROUP" "$DATA_DIR/bulava.env"
    say "  bulava.env -> $OAUTH_ENV (the token your shell already uses)"
  fi
  chmod 600 "$DATA_DIR/secrets.env" "$(readlink -f "$DATA_DIR/bulava.env")"
  chown "$RUN_USER:$RUN_GROUP" "$DATA_DIR/secrets.env"
}

# Settings both units give the engine (engine/SERVER.md). The runner hands the
# same ones to the engine commands it runs.
engine_env_lines() {
  printf '%s\n' \
    "Environment=SUPERVISOR_PROFILE=server" \
    "Environment=SUPERVISOR_REPORT_LANGUAGE=English" \
    "Environment=SUPERVISOR_CODEX_UNAVAILABLE=fail" \
    "Environment=SUPERVISOR_CODEX_UNAVAILABLE_AFTER_HOURS=3" \
    "Environment=SUPERVISOR_AUTOMATION_MCP_CONFIG=$MCP_CONFIG" \
    "Environment=SUPERVISOR_ALLOW_TOOLS=google" \
    "Environment=\"SUPERVISOR_SERVER_EXTRA_SECRET_PATHS=$DATA_DIR/secrets.env $DATA_DIR/bulava.env $DATA_DIR/runner.db $DATA_DIR/runner.lock $GOOGLE_DIR\"" \
    "Environment=PATH=/usr/local/bin:$RUN_HOME/.local/bin:/usr/bin:/bin" \
    "Environment=DISABLE_AUTOUPDATER=1"
}

# config.yaml must load before the units are written: a runner that restarts on one it refuses stops
# for good (exit 2). Nothing to read yet before the runner phase has installed it.
require_valid_config() {
  [ -x "$RUNNER_OPT/venv/bin/arceus-runner" ] && [ -f "$DATA_DIR/config.yaml" ] || return 0
  load_console_settings || die "$DATA_DIR/config.yaml is not valid: run arceus-runner check --offline"
}

engine_unit_text() {
  cat <<UNIT
[Unit]
Description=Bulava engine: tmux server for the workers
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$RUN_HOME
$(engine_env_lines)
# Optional: a \`claude setup-token\` token. Without it Claude uses the user's own /login.
# Workers inherit the tmux server's environment.
EnvironmentFile=-$DATA_DIR/bulava.env
ExecStart=/usr/bin/tmux -L bulava new-session -d -s keeper 'sleep infinity'
ExecStop=/usr/bin/tmux -L bulava kill-server
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
}

unit_text() {
  cat <<UNIT
[Unit]
Description=Arceus runner: issue tracker and code host bridge for Bulava
After=network-online.target bulava-engine.service
Wants=network-online.target bulava-engine.service

[Service]
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$DATA_DIR
Environment=ARCEUS_RUNNER_CONFIG=$DATA_DIR/config.yaml
# No EnvironmentFile: the runner reads secrets.env itself, so its tokens never sit in an
# environment, and hands bulava.env only to the engine commands it runs.
ExecStart=$RUNNER_OPT/venv/bin/arceus-runner run
Restart=always
RestartSec=30
# 2 = config error, 3 = another runner holds the lock: restarting will not fix either.
RestartPreventExitStatus=2 3
# dispatch.sh starts the engine's watchdogs from the runner's process; a runner restart must not
# take them down (engine/SERVER.md).
KillMode=process
NoNewPrivileges=yes
# No PrivateTmp: the engine's tmux socket is in /tmp/tmux-<uid>/, shared with bulava-engine.service.
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
}
# Units the operator has started but not enabled for good: they run now and are gone after the next
# reboot, with the agent's task left waiting for a runner that never comes back. The word, not the exit
# code: `enabled-runtime` also exits 0 and does not outlive the boot.
not_autostarted() {
  local u
  for u in bulava-engine runner; do
    systemctl is-active --quiet "$u" 2>/dev/null \
      && [ "$(systemctl is-enabled "$u" 2>/dev/null)" != enabled ] && printf '%s\n' "$u"
  done
  return 0
}
check_systemd() {
  require_valid_config
  same_file "$UNIT" "$(unit_text)" 644 root:root && same_file "$ENGINE_UNIT" "$(engine_unit_text)" 644 root:root \
    && [ -z "$(not_autostarted)" ]
}
apply_systemd() {
  local engine_changed=0 busy u
  put_file "$UNIT" 644 root:root "$(unit_text)" || true
  put_file "$ENGINE_UNIT" 644 root:root "$(engine_unit_text)" && engine_changed=1
  systemctl daemon-reload
  # Running means the operator has turned them on; enabling keeps them on through a reboot.
  for u in $(not_autostarted); do
    # A --runtime enable sits in /run beside the one written now and would still be what is-enabled names.
    systemctl disable --runtime --quiet "$u" 2>/dev/null || true
    systemctl enable --quiet "$u" && say "  $u was running but not enabled: enabled, so it starts after a reboot"
  done
  # Installed, not started: the operator fills in config.yaml and bulava.env, runs
  # `arceus-runner check`, then `systemctl enable --now bulava-engine runner`.
  # A running engine keeps its old environment until it restarts, and a restart ends the workers'
  # sessions: only between tasks, as the engine itself is swapped.
  if [ "$engine_changed" = 1 ] && systemctl is-active --quiet bulava-engine 2>/dev/null; then
    busy="$(running_bulava_tasks | tr '\n' ' ')"
    if [ -z "${busy// /}" ]; then
      systemctl restart bulava-engine && say "  bulava-engine restarted with the new settings"
    else
      say "  bulava-engine has a task running ($busy): restart it between tasks (sudo systemctl restart bulava-engine)"
    fi
  fi
}

# The project's assistant (assistant/ in the repository, docs/assistant.md): a process of its own per project,
# arceus-assistant@<run user>, from one install in ASSISTANT_OPT; config.yaml's `assistant:` section says whether it
# runs. Every project this setup knows counts: this folder's and the console's (CONSOLE_PROJECT_DIR). Its tokens are
# in assistant.env, made here empty and 600 before the console phase, so the console's container never sees it.
ASSISTANT_OPT=/opt/arceus-assistant
SYSTEMD_DIR=/etc/systemd/system
ASSISTANT_UNIT=$SYSTEMD_DIR/arceus-assistant@.service
MIRROR_UNIT=$SYSTEMD_DIR/arceus-assistant-mirror@.service
MIRROR_TIMER=$SYSTEMD_DIR/arceus-assistant-mirror@.timer
assistant_projects() {   # user data-folder home, one project a line
  printf '%s %s %s\n' "$RUN_USER" "$DATA_DIR" "$RUN_HOME"
  [ "$CONSOLE_PROJECT_DIR" = "$ARCEUS_HOME" ] || [ -z "$CONSOLE_USER" ] \
    || printf '%s %s %s\n' "$CONSOLE_USER" "$CONSOLE_DATA" "$CONSOLE_HOME"
}
assistant_env() {   # user data home: the project's ASSISTANT_* lines; nothing when config.yaml cannot say
  [ -x "$RUNNER_OPT/venv/bin/arceus-runner" ] && [ -f "$2/config.yaml" ] || return 0
  runuser -u "$1" -- env HOME="$3" "$RUNNER_OPT/venv/bin/arceus-runner" --config "$2/config.yaml" console env \
    2>/dev/null | grep '^ASSISTANT_' || true
}
aget() { printf '%s\n' "$1" | sed -n "s/^$2=//p"; }
assistant_source_hash() {   # the venv holds the runner's package too: its settings are the runner's config
  (cd "$REPO_DIR" && find assistant/pyproject.toml assistant/assistant assistant/deploy runner/pyproject.toml runner/src \
    -type f ! -path '*/__pycache__/*' ! -name '*.pyc' ! -path '*.egg-info*' ! -path 'runner/src/build/*' -print0 \
    | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
}
# The same bot from before Arceus (~/assistant with its own unit): two connections to one Slack app split its
# events between them, so the new one never starts beside it.
legacy_assistant_units() {   # user
  local f n
  for f in "$SYSTEMD_DIR"/*.service; do
    [ -f "$f" ] || continue
    n="$(basename "$f" .service)"
    case "$n" in arceus-assistant*) continue ;; esac
    grep -q 'assistant\.server' "$f" || continue
    [ "$(sed -n 's/^User=//p' "$f" | head -1)" = "$1" ] || continue
    { systemctl is-active --quiet "$n" || systemctl is-enabled --quiet "$n"; } 2>/dev/null && printf '%s\n' "$n"
  done
  return 0
}
assistant_unit_text() {
  cat <<UNIT
[Unit]
Description=Arceus assistant of %i: the web chat behind the console and the bot in Slack
After=network-online.target
Wants=network-online.target

[Service]
User=%i
WorkingDirectory=~
Environment=PYTHONUNBUFFERED=1
# Which config.yaml: the drop-in setup writes for each project (arceus-assistant@<user>.service.d/arceus.conf).
# No EnvironmentFile: the assistant reads assistant.env itself, at every use; its tokens never sit in an environment.
ExecStart=$ASSISTANT_OPT/venv/bin/arceus-assistant
Restart=on-failure
RestartSec=5
# 2 = config.yaml refused: restarting will not fix it.
RestartPreventExitStatus=2
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
}
mirror_unit_text() {
  cat <<UNIT
[Unit]
Description=Arceus assistant of %i: refresh the read-only code mirror it reads from the factory's clones

[Service]
Type=oneshot
User=%i
# ASSISTANT_WORKSPACE and ASSISTANT_MIRROR: the drop-in setup writes for each project.
ExecStart=/bin/bash $ASSISTANT_OPT/src/deploy/mirror.sh \${ASSISTANT_WORKSPACE} \${ASSISTANT_MIRROR}
Nice=10
UNIT
}
mirror_timer_text() {
  cat <<UNIT
[Unit]
Description=Arceus assistant of %i: refresh its code mirror every hour

[Timer]
OnCalendar=hourly
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
}
assistant_dropin() {  # unit user: its drop-in's path
  printf '%s/%s@%s.service.d/arceus.conf' "$SYSTEMD_DIR" "$1" "$2"
}
assistant_dropin_text() {  # data env
  printf '%s\n' "# Written by setup.sh: this project's config.yaml. Do not edit." "[Service]" \
    "Environment=ARCEUS_RUNNER_CONFIG=$1/config.yaml"
}
mirror_dropin_text() {  # env
  printf '%s\n' "# Written by setup.sh from config.yaml (paths.workspace, assistant.home). Do not edit." "[Service]" \
    "Environment=ASSISTANT_WORKSPACE=$(aget "$1" ASSISTANT_WORKSPACE)" \
    "Environment=ASSISTANT_MIRROR=$(aget "$1" ASSISTANT_HOME)/mirror"
}
assistant_secrets_ok() {  # user data env: its assistant.env, when in the data folder, is there and the user's alone
  local s; s="$(aget "$3" ASSISTANT_SECRETS)"
  case "$s" in "$2"/*) [ -f "$s" ] && [ ! -L "$s" ] && [ "$(stat -c '%a %U' "$s")" = "600 $1" ] ;; *) return 0 ;; esac
}
assistant_wanted() {
  local u d h
  while read -r u d h; do
    [ "$(aget "$(assistant_env "$u" "$d" "$h")" ASSISTANT_ENABLED)" = 1 ] && return 0
  done <<< "$(assistant_projects)"
  return 1
}
assistant_installed() {
  [ -x "$ASSISTANT_OPT/venv/bin/arceus-assistant" ] \
    && [ "$(cat "$ASSISTANT_OPT/.source-sha256" 2>/dev/null)" = "$(assistant_source_hash)" ]
}
started_after_install() {  # unit: active since the code now installed (setup restarts it after an install)
  local since installed
  since="$(date -d "$(systemctl show "$1" -p ActiveEnterTimestamp --value)" +%s 2>/dev/null)" || return 1
  installed="$(stat -c %Y "$ASSISTANT_OPT/.source-sha256" 2>/dev/null)" || return 1
  [ "$since" -ge "$installed" ]
}
check_assistant() {
  local u d h env wanted=0
  while read -r u d h; do
    env="$(assistant_env "$u" "$d" "$h")"
    [ -n "$env" ] || continue
    assistant_secrets_ok "$u" "$d" "$env" || return 1
    if [ "$(aget "$env" ASSISTANT_ENABLED)" = 1 ]; then
      wanted=1
      [ -z "$(legacy_assistant_units "$u")" ] || return 1
      same_file "$(assistant_dropin arceus-assistant "$u")" "$(assistant_dropin_text "$d")" 644 root:root || return 1
      same_file "$(assistant_dropin arceus-assistant-mirror "$u")" "$(mirror_dropin_text "$env")" 644 root:root \
        || return 1
      systemctl is-enabled --quiet "arceus-assistant@$u" && systemctl is-active --quiet "arceus-assistant@$u" \
        && systemctl is-enabled --quiet "arceus-assistant-mirror@$u.timer" || return 1
      started_after_install "arceus-assistant@$u" || return 1
    else
      ! systemctl is-enabled --quiet "arceus-assistant@$u" 2>/dev/null \
        && ! systemctl is-active --quiet "arceus-assistant@$u" 2>/dev/null || return 1
    fi
  done <<< "$(assistant_projects)"
  [ "$wanted" = 1 ] || return 0
  assistant_installed && same_file "$ASSISTANT_UNIT" "$(assistant_unit_text)" 644 root:root \
    && same_file "$MIRROR_UNIT" "$(mirror_unit_text)" 644 root:root \
    && same_file "$MIRROR_TIMER" "$(mirror_timer_text)" 644 root:root
}
apply_assistant() {
  local u d h env s legacy units_changed=0 code_changed=0 own
  # assistant.env first, for every project: made before the console phase lists the data folder's secrets.
  while read -r u d h; do
    env="$(assistant_env "$u" "$d" "$h")"
    [ -n "$env" ] || continue
    s="$(aget "$env" ASSISTANT_SECRETS)"
    case "$s" in "$d"/*) ;; *) continue ;; esac
    [ ! -L "$s" ] || die "$s is a symbolic link: the assistant's tokens must be a file of the run user's"
    if [ ! -e "$s" ]; then
      ( umask 077; printf '%s\n' "# The assistant's tokens (docs/assistant.md): SLACK_APP_TOKEN, SLACK_BOT_TOKEN," \
          "# YOUTRACK_TOKEN, ANTHROPIC_API_KEY. Never in config.yaml. Set them from the console's Assistant page." > "$s" )
      chown "$u:$(id -gn "$u")" "$s"
      say "  created $s (600): the assistant's tokens go there"
    else
      chmod 600 "$s"; chown "$u:$(id -gn "$u")" "$s"
    fi
  done <<< "$(assistant_projects)"
  if ! assistant_wanted; then
    while read -r u d h; do
      if systemctl is-enabled --quiet "arceus-assistant@$u" 2>/dev/null \
         || systemctl is-active --quiet "arceus-assistant@$u" 2>/dev/null; then
        systemctl disable --now --quiet "arceus-assistant@$u" "arceus-assistant-mirror@$u.timer" 2>/dev/null || true
        say "  arceus-assistant@$u stopped: assistant.enabled is off in its config.yaml"
      fi
    done <<< "$(assistant_projects)"
    return 0
  fi
  # Refused before anything is installed or started: the old bot must be carried over and stopped first.
  while read -r u d h; do
    [ "$(aget "$(assistant_env "$u" "$d" "$h")" ASSISTANT_ENABLED)" = 1 ] || continue
    legacy="$(legacy_assistant_units "$u" | tr '\n' ' ')"
    [ -z "${legacy// /}" ] || die "the assistant of '$u' still runs from before Arceus (${legacy% }): two connections to one Slack app split its events. Carry it over (arceus-runner assistant import ~/assistant/config.yaml --apply, as $u), stop it (sudo systemctl disable --now ${legacy% }), then run setup again. Its conversations stay where they are."
  done <<< "$(assistant_projects)"
  if ! assistant_installed; then
    install -d -m 755 -o root -g root "$ASSISTANT_OPT"
    rsync -a --delete --exclude '__pycache__' --exclude '*.egg-info' --exclude '.venv' --exclude 'tests' \
      "$REPO_DIR/assistant/pyproject.toml" "$REPO_DIR/assistant/assistant" "$REPO_DIR/assistant/deploy" \
      "$ASSISTANT_OPT/src/"
    [ -x "$ASSISTANT_OPT/venv/bin/python" ] || python3 -m venv "$ASSISTANT_OPT/venv"
    rm -rf "$ASSISTANT_OPT/src/build" "$ASSISTANT_OPT/src/"*.egg-info
    # The runner's package from its own install (the runner phase ran first), then the assistant with voice.
    "$ASSISTANT_OPT/venv/bin/pip" install --quiet --disable-pip-version-check --upgrade \
      "$RUNNER_OPT/src" "$ASSISTANT_OPT/src[voice]"
    assistant_source_hash > "$ASSISTANT_OPT/.source-sha256"
    chown -R root:root "$ASSISTANT_OPT"; chmod -R go-w "$ASSISTANT_OPT"
    code_changed=1
    say "  installed the assistant in $ASSISTANT_OPT"
  fi
  put_file "$ASSISTANT_UNIT" 644 root:root "$(assistant_unit_text)" && units_changed=1
  put_file "$MIRROR_UNIT" 644 root:root "$(mirror_unit_text)" && units_changed=1
  put_file "$MIRROR_TIMER" 644 root:root "$(mirror_timer_text)" && units_changed=1
  while read -r u d h; do
    env="$(assistant_env "$u" "$d" "$h")"
    [ "$(aget "$env" ASSISTANT_ENABLED)" = 1 ] || continue
    put_file "$(assistant_dropin arceus-assistant "$u")" 644 root:root "$(assistant_dropin_text "$d")" \
      && units_changed=1
    put_file "$(assistant_dropin arceus-assistant-mirror "$u")" 644 root:root "$(mirror_dropin_text "$env")" \
      && units_changed=1
  done <<< "$(assistant_projects)"
  systemctl daemon-reload
  while read -r u d h; do
    env="$(assistant_env "$u" "$d" "$h")"
    own="arceus-assistant@$u"
    if [ "$(aget "$env" ASSISTANT_ENABLED)" != 1 ]; then
      if systemctl is-enabled --quiet "$own" 2>/dev/null || systemctl is-active --quiet "$own" 2>/dev/null; then
        systemctl disable --now --quiet "$own" "arceus-assistant-mirror@$u.timer" 2>/dev/null || true
        say "  $own stopped: assistant.enabled is off in its config.yaml"
      fi
      continue
    fi
    systemctl enable --quiet --now "arceus-assistant-mirror@$u.timer"
    [ -d "$(aget "$env" ASSISTANT_HOME)/mirror" ] \
      || systemctl start --no-block "arceus-assistant-mirror@$u.service"
    if systemctl is-active --quiet "$own" && { [ "$code_changed" = 1 ] || [ "$units_changed" = 1 ] \
         || ! started_after_install "$own"; }; then
      systemctl restart "$own" && say "  $own restarted with the new code (a reply under way says it was cut off)"
    elif ! systemctl is-active --quiet "$own"; then
      systemctl enable --quiet --now "$own" && say "  $own started (127.0.0.1:$(aget "$env" ASSISTANT_PORT))"
    fi
    systemctl enable --quiet "$own"
  done <<< "$(assistant_projects)"
}

# Grafana, Prometheus, Loki, Alloy and node_exporter in Docker, all on 127.0.0.1.
# Grafana is reached through an SSH tunnel; its admin password is generated once into data/.
obs_source_hash() {
  (cd "$REPO_DIR/observability" && find . -type f ! -name build_dashboards.py -print0 | sort -z \
    | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
}
# Grafana shows the console's project (CONSOLE_PROJECT_DIR): its runner's metrics and events, labelled with its
# name, and the dashboards keep to that label, so another project's history in the same Prometheus and Loki is
# never shown as this one's.
obs_project() { console_get CONSOLE_PROJECT; }
obs_metrics_port() { console_get CONSOLE_METRICS_PORT; }
obs_env() {
  printf 'GRAFANA_ADMIN_FILE=%s\nARCEUS_DATA=%s\nBULAVA_STATE=%s\nARCEUS_PROJECT=%s\n' \
    "$OBS_DIR/secrets/grafana-admin" "$CONSOLE_DATA" "$CONSOLE_HOME/.claude/supervisor" "$(obs_project)"
  # With the console on, Grafana is its /grafana/ and lets in only whom the console names (docs/console.md).
  if console_enabled; then
    printf 'GRAFANA_ANONYMOUS=false\nGRAFANA_AUTH_PROXY=true\nGRAFANA_SUB_PATH=true\nGRAFANA_ROOT_URL=%s/grafana/\n' \
      "$(console_get CONSOLE_PUBLIC_URL)"
  fi
}
# Grafana's own health address: under /grafana/ once it serves from there.
grafana_health() {
  if console_enabled; then echo http://127.0.0.1:3000/grafana/api/health; else echo http://127.0.0.1:3000/api/health; fi
}
obs_running() {
  local s running
  running="$(docker compose --project-directory "$OBS_DIR" ps --status running --format '{{.Service}}' 2>/dev/null)"
  for s in "${OBS_SERVICES[@]}"; do printf '%s\n' "$running" | grep -qx "$s" || return 1; done
}
obs_stale_services() {  # services whose container still reads an older copy of its config file
  local m svc inside file id pid
  for m in "${OBS_FILE_MOUNTS[@]}"; do
    svc="${m%%:*}"; inside="${m#*:}"; inside="${inside%:*}"; file="${m##*:}"
    id="$(docker compose --project-directory "$OBS_DIR" ps -q "$svc" 2>/dev/null)"
    pid="$([ -z "$id" ] || docker inspect -f '{{.State.Pid}}' "$id" 2>/dev/null)"
    [ -n "$pid" ] && [ "$pid" != 0 ] || continue  # not running: `up -d` starts it with the current file
    # Through the process's own root: `docker cp` would resolve the mount on the host and see the new file.
    [ "$(sha256sum < "/proc/$pid/root$inside")" = "$(sha256sum < "$OBS_DIR/$file")" ] || printf '%s\n' "$svc"
  done
}
# The repository's prometheus.yml and dashboards, made the project's: the runner's scrape target is its metrics
# port and carries `project`, and every query of the runner's metrics or events asks for that label.
obs_render() {  # $1: a folder holding a copy of the repository's observability/
  local project port
  project="$(obs_project)"; port="$(obs_metrics_port)"
  # config.yaml not readable yet (a first setup before it is filled in): the repository's files as they are.
  [ -n "$project" ] && [ -n "$port" ] || return 0
  "$RUNNER_OPT/venv/bin/python" - "$1" "$project" "$port" <<'PY'
import json, pathlib, re, sys
root, project, port = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
prom = root / "prometheus.yml"
text, n = re.subn(r'(job_name: runner\n\s*static_configs: )\[\{targets: \["127\.0\.0\.1:\d+"\]\}\]',
                  r'\1[{targets: ["127.0.0.1:%s"], labels: {project: "%s"}}]' % (port, project), prom.read_text())
if n != 1:
    sys.exit("prometheus.yml: the runner's scrape job is not where setup.sh expects it")
prom.write_text(text)
sel = 'project="%s"' % project
def metric(m):  # runner_x or runner_x{...} -> with the project's label
    name, labels = m.group(1), m.group(2)
    return '%s{%s%s}' % (name, sel, (',' + labels) if labels else '')
for path in sorted((root / "grafana" / "dashboards").glob("*.json")):
    def fix(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "expr" and isinstance(v, str):
                    v = re.sub(r'\b(runner_[a-z0-9_]+)(?:\{([^}]*)\})?', metric, v)
                    node[k] = re.sub(r'\{(?=source=)', '{%s, ' % sel, v)  # Loki: {source=...} streams
                else:
                    fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)
    data = json.loads(path.read_text())
    fix(data)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
PY
}
obs_rendered_ok() {  # the installed copy is the repository's, made this project's
  local tmp ok=0
  tmp="$(mktemp -d)"
  rsync -a --exclude build_dashboards.py "$REPO_DIR/observability/" "$tmp/"
  (obs_render "$tmp") >/dev/null 2>&1 || ok=1
  if [ "$ok" = 0 ]; then
    cmp -s "$tmp/prometheus.yml" "$OBS_DIR/prometheus.yml" && diff -rq "$tmp/grafana/dashboards" "$OBS_DIR/grafana/dashboards" >/dev/null || ok=1
  fi
  rm -rf "$tmp"; return "$ok"
}
check_observability() {
  load_console_settings 2>/dev/null || true  # Grafana's sign-in follows the console's; unreadable: as without it
  [ "$(cat "$OBS_DIR/.source-sha256" 2>/dev/null)" = "$(obs_source_hash)" ] || return 1
  obs_rendered_ok || return 1
  [ -z "$(obs_stale_services)" ] || return 1
  same_file "$OBS_DIR/.env" "$(obs_env)" 644 root:root || return 1
  [ -s "$DATA_DIR/grafana-admin-password" ] && [ -s "$OBS_DIR/secrets/grafana-admin" ] || return 1
  obs_running && curl -fsS -o /dev/null "$(grafana_health)"
}
apply_observability() {
  load_console_settings || true
  install -d -m 755 -o root -g root "$OBS_DIR"
  rsync -a --delete --exclude build_dashboards.py --exclude .env --exclude .source-sha256 --exclude secrets \
    "$REPO_DIR/observability/" "$OBS_DIR/"
  obs_render "$OBS_DIR"
  chown -R root:root "$OBS_DIR"; chmod -R go-w "$OBS_DIR"
  put_file "$OBS_DIR/.env" 644 root:root "$(obs_env)" || true
  if [ ! -s "$DATA_DIR/grafana-admin-password" ]; then
    (umask 077; openssl rand -base64 24 | tr -d '\n' > "$DATA_DIR/grafana-admin-password")
    chown "$RUN_USER:$RUN_GROUP" "$DATA_DIR/grafana-admin-password"
    say "  Grafana admin password: $DATA_DIR/grafana-admin-password"
  fi
  # Grafana runs as uid 472 in its container: it gets its own copy, readable by that uid only.
  install -d -m 755 -o root -g root "$OBS_DIR/secrets"
  install -m 400 -o 472 -g 472 "$DATA_DIR/grafana-admin-password" "$OBS_DIR/secrets/grafana-admin"
  docker compose --project-directory "$OBS_DIR" up -d --remove-orphans --quiet-pull 2>&1 | sed 's/^/  /'
  local stale; stale="$(obs_stale_services)"
  if [ -n "$stale" ]; then
    say "  recreating with the new config: $(echo $stale)"
    # shellcheck disable=SC2086
    docker compose --project-directory "$OBS_DIR" up -d --force-recreate --no-deps $stale 2>&1 | sed 's/^/  /'
  fi
  obs_source_hash > "$OBS_DIR/.source-sha256"
  local _; for _ in $(seq 1 30); do curl -fs -o /dev/null "$(grafana_health)" && break; sleep 2; done
}

# The web console (console/ in the repository): the console and nginx in Docker, the console as the run
# user without sudo, capabilities or the secrets of the data folder. Switched on and configured in
# config.yaml (console:); what this phase needs from there, `arceus-runner console env` prints.
CONSOLE_SETTINGS=""
# Read once, in this shell (never in a $( ) of its own: a failure there would only end that subshell, and the
# console would look switched off). Returns 1 when config.yaml or the installed runner cannot say.
load_console_settings() {
  [ -n "$CONSOLE_SETTINGS" ] && return 0
  [ -x "$RUNNER_OPT/venv/bin/arceus-runner" ] || return 1
  # As the run user, with its home: config.yaml's ~ means that home, as for the runner.
  CONSOLE_SETTINGS="$(runuser -u "$CONSOLE_USER" -- env HOME="$CONSOLE_HOME" "$RUNNER_OPT/venv/bin/arceus-runner" \
    --config "$CONSOLE_DATA/config.yaml" console env 2>&1)" \
    || { printf '  %s\n' "$CONSOLE_SETTINGS" | head -5 >&2; CONSOLE_SETTINGS=""; return 1; }
}
console_settings() { printf '%s\n' "$CONSOLE_SETTINGS"; }
console_get() { console_settings | sed -n "s/^$1=//p"; }
console_enabled() { [ "$(console_get CONSOLE_ENABLED)" = 1 ]; }
console_compose() {
  docker compose --project-directory "$CONSOLE_DIR" -f "$CONSOLE_DIR/compose.yaml" -f "$CONSOLE_DIR/mounts.yaml" "$@"
}
console_source_hash() {   # the image is built from runner/, console/ and the extra packages: its tag is their hash
  with_extra "$(cd "$REPO_DIR" && find console runner/pyproject.toml runner/src -type f ! -path '*/__pycache__/*' \
    ! -name '*.pyc' ! -path '*.egg-info*' ! -path 'runner/src/build/*' -print0 | sort -z | xargs -0 sha256sum \
    | sha256sum | cut -d' ' -f1)" | cut -c1-16
}
console_env() {
  printf '%s\n' "CONSOLE_IMAGE_TAG=$(console_source_hash)" "RUN_UID=$(id -u "$CONSOLE_USER")" \
    "RUN_GID=$(id -g "$CONSOLE_USER")" "RUN_HOME=$CONSOLE_HOME" "DATA_DIR=$CONSOLE_DATA" \
    "CONSOLE_PUBLIC_URL=$(console_get CONSOLE_PUBLIC_URL)" "CONSOLE_SESSION_IDLE=$(console_get CONSOLE_SESSION_IDLE)" \
    "CONSOLE_SESSION_MAX=$(console_get CONSOLE_SESSION_MAX)"
}
yaml_str() { local s="${1//\\/\\\\}"; printf '"%s"' "${s//\"/\\\"}"; }
# What the console sees beyond the data folder (read-only), and what in the data folder it must not: every
# file or folder there that holds a credential, by name, replaced by an empty one. Symbolic links are left:
# their targets are outside what the container sees.
console_secret_paths() {
  { console_get CONSOLE_SECRET_FILE
    find "$CONSOLE_DATA" -mindepth 1 -maxdepth 1 \( -type f -o -type d \) \( -iname '*.env' -o -iname 'secrets*' \
      -o -iname '*password*' -o -iname '*credential*' -o -iname '*.key' -o -iname '*.pem' -o -iname '*token*' \
      -o -name google \) 2>/dev/null
  } | while IFS= read -r p; do
    case "$p" in "$CONSOLE_DATA"/*) [ -e "$p" ] && [ ! -L "$p" ] && printf '%s\n' "$p" ;; esac
  done | sort -u
}
console_forbidden_dir() {  # $1: a folder outside the data folder that config.yaml asks the console to read
  local p="${1%/}" sys
  [ -z "$p" ] && return 0
  case "$CONSOLE_HOME/" in "$p"/*) return 0 ;; esac          # the home itself, or a folder above it (/home, /)
  case "$p/" in "$CONSOLE_HOME"/.*) return 0 ;; esac         # the home's hidden folders: .ssh, .claude, .config
  for sys in /etc /root /proc /sys /dev /run /boot /usr /bin /sbin /lib /lib64 /var /opt/arceus-runner \
             /opt/arceus-console /opt/arceus-observability /opt/agent-tools /opt/bulava; do
    case "$p/" in "$sys"/*) return 0 ;; esac
  done
  return 1
}
console_mounts() {
  local p
  printf '%s\n' "# Written by setup.sh from config.yaml and the data folder. Do not edit." "services:" "  console:" "    volumes:"
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    case "$p/" in "$CONSOLE_DATA"/*) continue ;; esac
    # Never the machine's own folders, the run user's home or anything above it: what is in them (keys, tokens,
    # other users' files) is not the console's to read. A reports folder there is refused, and said.
    if console_forbidden_dir "$p"; then
      printf '  console: %s is not mounted into the console: it is the home, above it, or a system folder\n' "$p" >&2
      continue
    fi
    [ -d "$p" ] || install -d -m 700 -o "$CONSOLE_USER" -g "$CONSOLE_GROUP" "$p"
    printf '      - {type: bind, source: %s, target: %s, read_only: true}\n' "$(yaml_str "$p")" "$(yaml_str "$p")"
  done <<< "$(console_get CONSOLE_READ_DIR)"
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    if [ -d "$p" ]; then
      printf '      - {type: tmpfs, target: %s, tmpfs: {size: 4096, mode: 0o500}}\n' "$(yaml_str "$p")"
    else
      printf '      - {type: bind, source: /dev/null, target: %s, read_only: true}\n' "$(yaml_str "$p")"
    fi
  done <<< "$(console_secret_paths)"
  if [ "$(console_get CONSOLE_TLS)" = 1 ]; then
    printf '%s\n' "  nginx:" "    volumes:" "      - {type: bind, source: ./tls, target: /etc/nginx/tls, read_only: true}"
  fi
}
# console.tls: a certificate of its own for nginx, made once (ten years), for the load balancer's HTTPS to it.
console_tls_ok() {
  [ "$(console_get CONSOLE_TLS)" != 1 ] && return 0
  [ -s "$CONSOLE_DIR/tls/cert.pem" ] && [ -s "$CONSOLE_DIR/tls/key.pem" ]
}
apply_console_tls() {
  console_tls_ok && return 0
  local host; host="$(console_get CONSOLE_PUBLIC_URL | sed -E 's#^https?://##')"
  install -d -m 700 -o root -g root "$CONSOLE_DIR/tls"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=${host:-arceus-console}" \
    -keyout "$CONSOLE_DIR/tls/key.pem" -out "$CONSOLE_DIR/tls/cert.pem" >/dev/null 2>&1
  chmod 600 "$CONSOLE_DIR/tls/key.pem"; chmod 644 "$CONSOLE_DIR/tls/cert.pem"
  say "  nginx: made a certificate of its own for the load balancer's HTTPS ($CONSOLE_DIR/tls)"
}
# What nginx's listen names: the port alone (every address) or console.listen with it; an IPv6 one in brackets.
console_listen() {
  local addr port; addr="$(console_get CONSOLE_LISTEN)"; port="$(console_get CONSOLE_PORT)"
  case "$addr" in
    "") printf '%s' "$port" ;;
    *:*) printf '[%s]:%s' "$addr" "$port" ;;
    *) printf '%s:%s' "$addr" "$port" ;;
  esac
}
console_nginx_conf() {
  local net
  cat <<CONF
# Written by setup.sh from config.yaml (console:). Do not edit.
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 512; }
http {
  include /etc/nginx/mime.types;
  server_tokens off;
  access_log off;
  # Redirects name the path only: nginx does not know the https:// address the load balancer serves.
  absolute_redirect off;
  client_max_body_size 2m;
  client_body_temp_path /tmp/body;
  proxy_temp_path /tmp/proxy;
  fastcgi_temp_path /tmp/fastcgi;
  uwsgi_temp_path /tmp/uwsgi;
  scgi_temp_path /tmp/scgi;
  # The visitor's own address, as the proxies in front of this one (console.trusted_proxies) say.
$(for net in $(console_get CONSOLE_TRUSTED_PROXIES); do printf '  set_real_ip_from %s;\n' "$net"; done)
  real_ip_header X-Forwarded-For;
  real_ip_recursive on;
  map \$http_upgrade \$connection_upgrade { default upgrade; '' close; }
  # Where a visitor without a session was going, for the sign-in to send them back: the path as the browser sent
  # it (still percent-encoded, so no line break can reach the header), without its query.
  map \$request_uri \$grafana_path { ~^(?<path>/grafana/[^?]*) \$path; default /grafana/; }
  server {
$(if [ "$(console_get CONSOLE_TLS)" = 1 ]; then
    printf '    listen %s ssl default_server;\n' "$(console_listen)"
    printf '    # The load balancer in front talks HTTPS to this backend; the certificate is setup'"'"'s own.\n'
    printf '    ssl_certificate /etc/nginx/tls/cert.pem;\n    ssl_certificate_key /etc/nginx/tls/key.pem;\n'
    printf '    ssl_protocols TLSv1.2 TLSv1.3;\n'
  else
    printf '    listen %s default_server;\n' "$(console_listen)"
  fi)
    # Grafana, once the console has said who asks. Its answer's headers replace any X-WEBAUTH-* the browser
    # sent; no session: to the sign-in.
    location = /_console_auth {
      internal;
      proxy_pass http://$CONSOLE_APP/_auth/grafana;
      proxy_pass_request_body off;
      proxy_set_header Content-Length "";
      proxy_set_header Host \$host;
      proxy_set_header X-Real-IP \$remote_addr;
    }
    location /grafana/ {
      auth_request /_console_auth;
      auth_request_set \$arceus_user \$upstream_http_x_arceus_user;
      auth_request_set \$arceus_name \$upstream_http_x_arceus_name;
      auth_request_set \$arceus_role \$upstream_http_x_arceus_role;
      error_page 401 = @signin;
      proxy_set_header X-WEBAUTH-USER \$arceus_user;
      proxy_set_header X-WEBAUTH-NAME \$arceus_name;
      proxy_set_header X-WEBAUTH-ROLE \$arceus_role;
      proxy_set_header Host \$host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_http_version 1.1;
      proxy_set_header Upgrade \$http_upgrade;
      proxy_set_header Connection \$connection_upgrade;
      proxy_pass http://127.0.0.1:3000;
    }
    location = /grafana { return 301 /grafana/; }
    location @signin { return 302 /login?next=\$grafana_path; }
$(if [ "$(console_get ASSISTANT_WEB)" = 1 ]; then cat <<ASSISTANT
    # The project's assistant (assistant: in config.yaml): its web chat, for whom the console says is signed in.
    # nginx writes X-Assistant-User from the console's answer over anything the browser sent.
    location /assistant/ {
      auth_request /_console_auth;
      auth_request_set \$arceus_user \$upstream_http_x_arceus_user;
      error_page 401 = @assistant_signin;
      proxy_set_header X-Assistant-User \$arceus_user;
      proxy_set_header Host \$host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_http_version 1.1;
      proxy_buffering off;
      proxy_read_timeout 900s;
      client_max_body_size 60m;
      proxy_pass http://127.0.0.1:$(console_get ASSISTANT_PORT);
    }
    location = /assistant { return 301 /assistant/; }
    location @assistant_signin { return 302 /login?next=/assistant/; }
ASSISTANT
fi)
$([ -n "${CONSOLE_EXTRA_LOCATIONS:-}" ] && printf '    # From %s (CONSOLE_EXTRA_LOCATIONS in setup.conf).\n' "$CONSOLE_EXTRA_LOCATIONS" && cat -- "$CONSOLE_EXTRA_LOCATIONS")
    location / {
      proxy_pass http://$CONSOLE_APP;
      proxy_http_version 1.1;
      proxy_set_header Host \$host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$remote_addr;
      proxy_read_timeout 120s;
    }
  }
}
CONF
}
# The host's side of the console's operations (runner/src/arceus_runner/console/hostops.py): a path unit wakes
# a root oneshot for each request the console writes; a timer marks lost operations interrupted and writes the
# services' state for the console. Root reads the data folder without following links.
OPS_UNITS=(arceus-ops.path arceus-ops.service arceus-ops-tick.service arceus-ops-tick.timer)
ops_command() {
  printf '%s console hostops --data %s --setup %s --runner-unit %s --engine-unit %s %s' \
    "$RUNNER_OPT/venv/bin/arceus-runner" "$CONSOLE_DATA" "$ARCEUS_HOME/setup/setup.sh" "$CONSOLE_RUNNER_UNIT" \
    "$CONSOLE_ENGINE_UNIT" "$1"
}
ops_unit_text() {
  case "$1" in
    arceus-ops.path) cat <<UNIT
[Unit]
Description=Arceus console: requests for the host's operations

[Path]
DirectoryNotEmpty=$CONSOLE_DATA/console/ops/requests
Unit=arceus-ops.service

[Install]
WantedBy=paths.target
UNIT
    ;;
    arceus-ops.service) cat <<UNIT
[Unit]
Description=Arceus console: take the console's requests (restart, setup) from its fixed list

[Service]
Type=oneshot
ExecStart=$(ops_command dispatch)
UNIT
    ;;
    arceus-ops-tick.service) cat <<UNIT
[Unit]
Description=Arceus console: mark lost operations interrupted, write the services' state

[Service]
Type=oneshot
ExecStart=$(ops_command tick)
UNIT
    ;;
    arceus-ops-tick.timer) cat <<UNIT
[Unit]
Description=Arceus console: the host executor's pass

[Timer]
OnBootSec=15s
OnUnitActiveSec=20s
AccuracySec=2s

[Install]
WantedBy=timers.target
UNIT
    ;;
  esac
}
ops_units_ok() {
  local u
  for u in "${OPS_UNITS[@]}"; do same_file "/etc/systemd/system/$u" "$(ops_unit_text "$u")" 644 root:root || return 1; done
  systemctl is-enabled -q arceus-ops.path && systemctl is-active -q arceus-ops.path \
    && systemctl is-enabled -q arceus-ops-tick.timer && systemctl is-active -q arceus-ops-tick.timer \
    && [ "$(stat -c '%U' "$CONSOLE_DATA/console/ops/requests" 2>/dev/null)" = "$CONSOLE_USER" ]
}
apply_ops_units() {
  local u changed=0
  install -d -m 700 -o "$CONSOLE_USER" -g "$CONSOLE_GROUP" "$CONSOLE_DATA/console/ops"
  install -d -m 700 -o "$CONSOLE_USER" -g "$CONSOLE_GROUP" "$CONSOLE_DATA/console/ops/requests"
  install -d -m 700 -o root -g root "$FACTS_DIR/ops"
  for u in "${OPS_UNITS[@]}"; do put_file "/etc/systemd/system/$u" 644 root:root "$(ops_unit_text "$u")" && changed=1; done
  systemctl daemon-reload
  systemctl enable --now --quiet arceus-ops.path arceus-ops-tick.timer
  if [ "$changed" = 1 ]; then systemctl restart arceus-ops.path arceus-ops-tick.timer; fi
}
remove_ops_units() {
  local u
  systemctl disable --now --quiet arceus-ops.path arceus-ops-tick.timer 2>/dev/null || true
  for u in "${OPS_UNITS[@]}"; do rm -f "/etc/systemd/system/$u"; done
  systemctl daemon-reload
}
console_running() {
  local running
  running="$(console_compose ps --status running --format '{{.Service}}' 2>/dev/null)"
  printf '%s\n' "$running" | grep -qx console && printf '%s\n' "$running" | grep -qx nginx
}
console_stale_nginx() {  # nginx still reads an older nginx.conf: an update replaces the file, not the one it opened
  local id pid
  id="$(console_compose ps -q nginx 2>/dev/null)"
  pid="$([ -z "$id" ] || docker inspect -f '{{.State.Pid}}' "$id" 2>/dev/null)"
  [ -n "$pid" ] && [ "$pid" != 0 ] || return 1
  [ "$(sha256sum < "/proc/$pid/root/etc/nginx/nginx.conf")" != "$(sha256sum < "$CONSOLE_DIR/nginx.conf")" ]
}
console_port_taken() {  # something that is not the console's nginx listens on the port
  local port; port="$(console_get CONSOLE_PORT)"
  ss -Hltnp "sport = :$port" 2>/dev/null | grep -v '"nginx"' | grep -q .
}
check_console() {
  # config.yaml or the runner it needs is not ready: not done (the runner phase, or a fix of config.yaml, first).
  load_console_settings || return 1
  if ! console_enabled; then
    # Off in config.yaml: nothing of it runs, and nothing on the host takes its requests.
    [ ! -f "$CONSOLE_DIR/compose.yaml" ] || [ -z "$(console_compose ps -q 2>/dev/null)" ] || return 1
    [ ! -f /etc/systemd/system/arceus-ops.path ]
    return
  fi
  ops_units_ok || return 1
  [ "$(cat "$CONSOLE_DIR/.source-sha256" 2>/dev/null)" = "$(console_source_hash)" ] || return 1
  same_file "$CONSOLE_DIR/.env" "$(console_env)" 644 root:root || return 1
  same_file "$CONSOLE_DIR/nginx.conf" "$(console_nginx_conf)" 644 root:root || return 1
  same_file "$CONSOLE_DIR/mounts.yaml" "$(console_mounts)" 644 root:root || return 1
  console_tls_ok || return 1
  docker image inspect "arceus-console:$(console_source_hash)" >/dev/null 2>&1 || return 1
  console_running && ! console_stale_nginx && curl -fsS -o /dev/null "http://$CONSOLE_APP/healthz"
}
apply_console() {
  load_console_settings \
    || die "the console's settings cannot be read from $CONSOLE_DATA/config.yaml: run arceus-runner check --offline"
  if ! console_enabled; then
    if [ -f "$CONSOLE_DIR/compose.yaml" ]; then
      console_compose down 2>&1 | sed 's/^/  /'
      say "  console.enabled is false in config.yaml: the console is stopped"
    fi
    [ ! -f /etc/systemd/system/arceus-ops.path ] || remove_ops_units
    return 0
  fi
  # The assistant's route used to be added by hand through CONSOLE_EXTRA_LOCATIONS; twice, nginx would not start.
  if [ "$(console_get ASSISTANT_WEB)" = 1 ] && [ -n "$CONSOLE_EXTRA_LOCATIONS" ] \
     && grep -q 'location /assistant/' "$CONSOLE_EXTRA_LOCATIONS"; then
    die "$CONSOLE_EXTRA_LOCATIONS (CONSOLE_EXTRA_LOCATIONS in setup.conf) still has the assistant's /assistant/: setup writes it now from config.yaml. Take that block out of the file (or the line out of setup.conf), then run setup again."
  fi
  console_port_taken && die "port $(console_get CONSOLE_PORT) is taken by something else (ss -ltnp): free it or set console.port"
  install -d -m 755 -o root -g root "$CONSOLE_DIR" "$CONSOLE_DIR/build"
  rsync -a --delete --exclude .env --exclude nginx.conf --exclude mounts.yaml --exclude .source-sha256 \
    --exclude build --exclude tls "$REPO_DIR/console/" "$CONSOLE_DIR/"
  install -d -m 755 -o root -g root "$CONSOLE_DIR/build/runner"
  rsync -a --delete --exclude __pycache__ --exclude '*.egg-info' --exclude build \
    "$REPO_DIR/runner/pyproject.toml" "$REPO_DIR/runner/src" "$CONSOLE_DIR/build/runner/"
  # The same wheels the runner has: the console reads config.yaml, so it must import every provider it names.
  [ "$(cat "$RUNNER_OPT/.source-sha256" 2>/dev/null || true)" = "$(runner_source_hash)" ] \
    || die "the runner is not installed as setup.conf says (RUNNER_EXTRA_PACKAGES?): run the runner phase first"
  rm -rf "$CONSOLE_DIR/build/extra"
  install -d -m 755 -o root -g root "$CONSOLE_DIR/build/extra"
  [ ! -d "$EXTRA_DIR" ] || cp -a "$EXTRA_DIR/." "$CONSOLE_DIR/build/extra/"
  chown -R root:root "$CONSOLE_DIR"; chmod -R go-w "$CONSOLE_DIR"
  local tag; tag="$(console_source_hash)"
  if ! docker image inspect "arceus-console:$tag" >/dev/null 2>&1; then
    say "  building the console's image arceus-console:$tag"
    docker build --quiet --label arceus.keep=true -t "arceus-console:$tag" -f "$CONSOLE_DIR/Dockerfile" \
      "$CONSOLE_DIR/build" 2>&1 | sed 's/^/  /'
  fi
  # console.db and the operations' requests: the run user's, in the data folder the container sees.
  install -d -m 700 -o "$CONSOLE_USER" -g "$CONSOLE_GROUP" "$CONSOLE_DATA/console"
  apply_ops_units
  apply_console_tls
  put_file "$CONSOLE_DIR/.env" 644 root:root "$(console_env)" || true
  put_file "$CONSOLE_DIR/mounts.yaml" 644 root:root "$(console_mounts)" || true
  put_file "$CONSOLE_DIR/nginx.conf" 644 root:root "$(console_nginx_conf)" || true
  console_compose up -d --remove-orphans --quiet-pull 2>&1 | sed 's/^/  /'
  if console_stale_nginx; then
    say "  nginx: recreated with the new nginx.conf"
    console_compose up -d --force-recreate --no-deps nginx 2>&1 | sed 's/^/  /'
  fi
  local _; for _ in $(seq 1 30); do curl -fs -o /dev/null "http://$CONSOLE_APP/healthz" && break; sleep 2; done
  # Older images of the console: nothing runs them any more.
  local old
  for old in $(docker image ls arceus-console --format '{{.Tag}}'); do
    [ "$old" = "$tag" ] || docker image rm -f "arceus-console:$old" >/dev/null 2>&1 || true
  done
  console_source_hash > "$CONSOLE_DIR/.source-sha256"
  if [ -z "$(runuser -u "$CONSOLE_USER" -- env HOME="$CONSOLE_HOME" "$RUNNER_OPT/venv/bin/arceus-runner" \
        --config "$CONSOLE_DATA/config.yaml" console users 2>/dev/null | grep -v '^No accounts')" ]; then
    say "  console: nobody can sign in yet. Make the first admin:"
    say "    $RUNNER_OPT/venv/bin/arceus-runner console invite --admin you@example.com"
  fi
}

# --- facts ---------------------------------------------------------------------------------------

version_of() { "$@" 2>/dev/null | head -1 || true; }
write_facts() {
  local status_json="$1"
  install -d -m 755 "$FACTS_DIR"
  . /etc/os-release
  jq -n \
    --arg at "$(date -u +%FT%TZ)" --arg os "$PRETTY_NAME" --arg kernel "$(uname -r)" --arg arch "$(uname -m)" \
    --arg systemd "$(systemctl --version | head -1)" --arg python "$(python3 --version 2>&1)" \
    --arg docker "$(version_of docker --version)" --arg compose "$(version_of docker compose version --short)" \
    --arg node "$(version_of node --version)" --arg npm "$(version_of npm --version)" \
    --arg chrome "$(version_of google-chrome-stable --version)" \
    --arg claude "$(installed_npm_version @anthropic-ai/claude-code)" --arg codex "$(installed_npm_version @openai/codex)" \
    --arg mcp "$(installed_npm_version chrome-devtools-mcp)" --arg google_mcp "$(installed_workspace_mcp)" \
    --arg runner "$(version_of "$RUNNER_OPT/venv/bin/arceus-runner" --version)" \
    --arg bulava_version "$(cat "$ENGINE/.engine-version" 2>/dev/null || true)" \
    --arg bulava_source "$(engine_stamp)" \
    --arg user "$RUN_USER" --arg sudo "$(sudo -l -U "$RUN_USER" 2>/dev/null | grep -q NOPASSWD && echo nopasswd || echo other)" \
    --argjson phases "$status_json" \
    '{written_at: $at, os: $os, kernel: $kernel, arch: $arch, systemd: $systemd, python: $python,
      docker: $docker, compose: $compose, node: $node, npm: $npm, chrome: $chrome,
      claude_code: $claude, codex: $codex, chrome_devtools_mcp: $mcp, workspace_mcp: $google_mcp, runner: $runner,
      bulava: {engine_version: $bulava_version, source: $bulava_source},
      run_as: {user: $user, sudo: $sudo, docker_group: true,
               trust_boundary: "vm: workers run as this user and can read the runner tokens; Bulava server-profile denies are a second line, not isolation"},
      phases: $phases}' > "$FACTS_DIR/facts.json"
  chmod 644 "$FACTS_DIR/facts.json"
}

# --- main ----------------------------------------------------------------------------------------

preflight
selected=("${PHASES[@]}")
if [ -n "$ONLY" ]; then
  printf '%s\n' "${PHASES[@]}" | grep -qx "$ONLY" || die "no phase '$ONLY' (see --list)"
  selected=("$ONLY")
fi

declare -A STATUS
pending=0
for phase in "${selected[@]}"; do
  if [ "$phase" = bulava ] && ! bulava_wanted; then
    STATUS[$phase]=skipped
    printf '%s[skip]%s %-14s BULAVA_SOURCE is empty: install the engine after wave 1\n' "$c_warn" "$c_off" "$phase"
    continue
  fi
  if "check_$phase"; then
    STATUS[$phase]=ok
    printf '%s[ ok ]%s %s\n' "$c_ok" "$c_off" "$phase"
    continue
  fi
  if [ "$MODE" = check ]; then
    STATUS[$phase]=pending; pending=1
    printf '%s[todo]%s %s\n' "$c_warn" "$c_off" "$phase"
    continue
  fi
  printf '[....] %s\n' "$phase"
  "apply_$phase"
  if "check_$phase"; then
    STATUS[$phase]=changed
    printf '%s[done]%s %s\n' "$c_ok" "$c_off" "$phase"
  else
    STATUS[$phase]=failed
    die "phase $phase ran but its check still fails"
  fi
done

status_json="$(for k in "${!STATUS[@]}"; do printf '%s\n%s\n' "$k" "${STATUS[$k]}"; done \
  | jq -Rn '[inputs] as $l | reduce range(0; $l|length; 2) as $i ({}; .[$l[$i]] = $l[$i+1])')"
if [ "$MODE" = apply ] && [ -z "$ONLY" ]; then
  write_facts "$status_json"
  say "facts: $FACTS_DIR/facts.json"
fi
exit "$pending"
