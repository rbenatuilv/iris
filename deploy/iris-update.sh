#!/usr/bin/env bash
# Iris self-updater. Pulls the private GitHub repo, tests the new version, deploys it,
# checks the service stays healthy, and rolls back automatically if anything fails.
#   iris-update            check for a new version (run every 10 min by iris-update.timer)
#   iris-update --force    redeploy the latest version even if nothing changed
#   iris-update --setup    first-time setup: create the read-only deploy key and clone the repo
set -uo pipefail

IRIS_HOME="${IRIS_HOME:-$HOME/iris}"
REPO="$IRIS_HOME/repo"
VENV="$IRIS_HOME/venv"
KEY="$HOME/.ssh/iris_deploy"
ENV_FILE="$IRIS_HOME/.env"
export GIT_SSH_COMMAND="ssh -4 -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes"

getv() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- ; }
log() { echo "[iris-update] $*"; }
notify() {
  local token chat; token="$(getv TELEGRAM_BOT_TOKEN)"; chat="$(getv TELEGRAM_CHAT_ID)"
  [ -n "$token" ] && [ -n "$chat" ] || return 0
  curl -4 -s -m 20 "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" --data-urlencode "text=$1" -d disable_notification=true >/dev/null || true
}

# ------------------------------------------------------------------ setup
if [ "${1:-}" = "--setup" ]; then
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  [ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "iris-pi-deploy" -f "$KEY"
  echo
  echo "1) Send this PUBLIC key to Claude (it is safe to share; it only allows reading the repo):"
  echo
  cat "$KEY.pub"
  echo
  read -rp "2) GitHub repository (owner/name, e.g. robbv/iris): " SLUG
  [ -n "$SLUG" ] || { echo "No repository given."; exit 1; }
  grep -vE '^IRIS_REPO=' "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || true
  echo "IRIS_REPO=git@github.com:${SLUG}.git" >> "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE" && chmod 600 "$ENV_FILE"
  echo "3) When Claude confirms the key was added, press Enter to test it."
  read -r _
  # GitHub over IPv4 (your network has no IPv6 route)
  if [ -d "$REPO/.git" ]; then git -C "$REPO" remote set-url origin "git@github.com:${SLUG}.git"
  else git clone -q "git@github.com:${SLUG}.git" "$REPO" || { echo "Clone failed: is the key added to the repo?"; exit 1; }
  fi
  echo "Connected to ${SLUG}. Iris will now check for updates every 10 minutes."
  git -C "$REPO" config core.fileMode false
  exec "$0" --force
fi

# ------------------------------------------------------------------ check
exec 9>"/tmp/iris-update.lock"; flock -n 9 || exit 0
[ -d "$REPO/.git" ] || { log "not set up (run: iris-update --setup)"; exit 0; }
cd "$REPO" || exit 1
git fetch -q origin main || { log "fetch failed"; exit 1; }
LOCAL="$(git rev-parse HEAD)"; REMOTE="$(git rev-parse origin/main)"
DEPLOYED="$(cat "$IRIS_HOME/.deployed_commit" 2>/dev/null || true)"
if [ "$REMOTE" = "$DEPLOYED" ] && [ "${1:-}" != "--force" ]; then exit 0; fi
MSG="$(git log -1 --format=%s "$REMOTE")"
log "updating to ${REMOTE:0:7}: $MSG"

# 1) test the new version in a staging copy (unit + replay tests, all offline)
git reset -q --hard "$REMOTE"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
cp -r iris config tests requirements.txt pytest.ini "$STAGE"/ 2>/dev/null
"$VENV/bin/pip" install -q -r requirements.txt pytest respx >/dev/null 2>&1
if ! (cd "$STAGE" && "$VENV/bin/python" -m pytest -q -x -p no:cacheprovider tests > "$STAGE/test.log" 2>&1); then
  log "tests failed, not deploying"; tail -20 "$STAGE/test.log"
  notify "⚠️ Iris update ${REMOTE:0:7} failed its tests and was NOT installed. Still running the previous version."
  [ -n "$DEPLOYED" ] && git reset -q --hard "$DEPLOYED"
  exit 1
fi

# 2) deploy, keeping a copy of the current version for rollback
rm -rf "$IRIS_HOME/.prev"; mkdir -p "$IRIS_HOME/.prev/config"
cp -r "$IRIS_HOME/iris" "$IRIS_HOME/.prev/" && cp "$IRIS_HOME/config/defaults.yaml" "$IRIS_HOME/.prev/config/"
rm -rf "$IRIS_HOME/iris"; cp -r "$STAGE/iris" "$IRIS_HOME/iris"
cp "$STAGE/config/defaults.yaml" "$IRIS_HOME/config/defaults.yaml"
cp "$STAGE/requirements.txt" "$IRIS_HOME/requirements.txt"
BEFORE="$(systemctl show -p NRestarts --value iris 2>/dev/null || echo 0)"
sudo -n /usr/bin/systemctl restart iris

# 3) watch it for a minute; roll back if it crashes
sleep 60
AFTER="$(systemctl show -p NRestarts --value iris 2>/dev/null || echo 0)"
if systemctl is-active --quiet iris && [ "$AFTER" = "$BEFORE" ]; then
  echo "$REMOTE" > "$IRIS_HOME/.deployed_commit"
  VER="$(grep -o '"[0-9.]*"' "$IRIS_HOME/iris/__init__.py" | tr -d '"')"
  log "deployed ${REMOTE:0:7} (v$VER)"
  notify "🔄 Iris updated to v$VER — $MSG"
  if [ -n "$DEPLOYED" ] && ! git diff --quiet "$DEPLOYED" "$REMOTE" -- deploy/; then
    notify "ℹ️ This update also changed the installer. Run once on the Pi when convenient: bash ~/iris/repo/dist/iris-install.sh"
  fi
else
  log "service unhealthy after update, rolling back"
  rm -rf "$IRIS_HOME/iris"; cp -r "$IRIS_HOME/.prev/iris" "$IRIS_HOME/iris"
  cp "$IRIS_HOME/.prev/config/defaults.yaml" "$IRIS_HOME/config/defaults.yaml"
  sudo -n /usr/bin/systemctl restart iris
  [ -n "$DEPLOYED" ] && git reset -q --hard "$DEPLOYED"
  notify "⚠️ Iris update ${REMOTE:0:7} crashed after install and was rolled back automatically."
  exit 1
fi
