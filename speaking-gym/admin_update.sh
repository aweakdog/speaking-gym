#!/usr/bin/env bash
set -euo pipefail
umask 077

REPO="git@github.com:aweakdog/speaking-gym.git"
BRANCH="main"
SOURCE="$HOME/speaking-gym-source"
LIVE="$(cd "$(dirname "$0")" && pwd)"
DATA="${SG_DATA_DIR:-$HOME/speaking-gym-data}"
MARKER="$DATA/deployed-commit"
LOCK="$DATA/admin-update.lock"
NODE="$HOME/.local/node/bin/node"

mkdir -p "$DATA"
exec 9>"$LOCK"
flock -n 9 || { echo "another update is already running"; exit 20; }
[[ -x "$NODE" ]] || { echo "Node.js 18+ is required at $NODE"; exit 26; }
node_major="$($NODE -p 'process.versions.node.split(".")[0]')"
(( node_major >= 18 )) || { echo "Node.js 18+ required, found $($NODE --version)"; exit 26; }

export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
if [[ ! -d "$SOURCE/.git" ]]; then
  git clone --quiet --filter=blob:none --no-checkout "$REPO" "$SOURCE"
fi

origin="$(git -C "$SOURCE" remote get-url origin)"
[[ "$origin" == "$REPO" ]] || { echo "refusing unexpected origin: $origin"; exit 21; }

git -C "$SOURCE" fetch --quiet --prune origin "refs/heads/$BRANCH"
target="$(git -C "$SOURCE" rev-parse FETCH_HEAD)"
[[ "$target" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid target commit"; exit 22; }

current=""
if [[ -f "$MARKER" ]]; then
  current="$(tr -d '[:space:]' < "$MARKER")"
fi
if [[ -n "$current" && "$current" != "$target" ]]; then
  [[ "$current" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid deployed marker"; exit 23; }
  git -C "$SOURCE" merge-base --is-ancestor "$current" "$target" || {
    echo "refusing non-fast-forward update from $current to $target"
    exit 24
  }
fi

if [[ "$current" == "$target" ]]; then
  echo "already current: $target"
  exit 0
fi

stage="$(mktemp -d "$DATA/update-stage.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
git -C "$SOURCE" archive "$target" speaking-gym | tar -x -C "$stage" --strip-components=1

python3 - "$stage" <<'PY'
import os, sys
root = sys.argv[1]
for base, dirs, files in os.walk(root):
    for name in dirs + files:
        path = os.path.join(base, name)
        if os.path.islink(path):
            raise SystemExit("refusing repository containing symlink: " + path)
        if name == "config.json" or name.endswith((".pem", ".db")):
            raise SystemExit("refusing repository containing secret/data file: " + path)
PY

"$NODE" --check "$stage/app.js"
"$NODE" --check "$stage/data.js"
"$NODE" --check "$stage/sw.js"
python3 -c "import ast; ast.parse(open('$stage/server.py').read())"
(
  cd "$stage"
  "$NODE" -e "
const fs = require('fs');
eval(fs.readFileSync('data.js','utf8') + \`
fs.writeFileSync('topics.json', JSON.stringify({
  categories: Object.values(TOPICS).map(t => ({ label: t.label, topics: t.items.map(i => i.q) }))
}));
\`)"
)

rsync -a --no-links --exclude='*.pem' --exclude='*.db' --exclude='*.log' --exclude='run.sh' --exclude='admin_update.sh' "$stage/" "$LIVE/"
printf '%s\n' "$target" > "$MARKER"
echo "deployed: $target"
echo "restart required"
