#!/usr/bin/env bash
# Iris installer / updater. Run as your normal user (not root): bash iris-install.sh
# Safe to re-run: keeps your .env (secrets), config/local.yaml and data/.
set -euo pipefail

IRIS_HOME="${IRIS_HOME:-$HOME/iris}"
SELF="$(readlink -f "$0")"
say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then echo "Run this as your normal user (e.g. pi), not with sudo."; exit 1; fi

say "Installing system packages (asks for your password)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip sqlite3 curl ca-certificates tzdata git >/dev/null

say "Unpacking Iris into $IRIS_HOME"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LINE=$(awk '/^__IRIS_PAYLOAD__$/{print NR + 1; exit}' "$SELF")
tail -n +"$LINE" "$SELF" | base64 -d | tar xz -C "$TMP"
mkdir -p "$IRIS_HOME/config" "$IRIS_HOME/data"
rm -rf "$IRIS_HOME/iris"
cp -r "$TMP/iris" "$IRIS_HOME/iris"
cp "$TMP/config/defaults.yaml" "$IRIS_HOME/config/defaults.yaml"
cp "$TMP/requirements.txt" "$IRIS_HOME/requirements.txt"
if [ ! -f "$IRIS_HOME/config/local.yaml" ]; then
  cat > "$IRIS_HOME/config/local.yaml" <<'YAML'
# Your overrides. Same structure as defaults.yaml; anything here wins.
# Examples:
# quiet_hours: {start: "23:30", end: "07:00"}
# extra_locations:
#   - {id: rome, name: Rome, lat: 41.9028, lon: 12.4964, tz: Europe/Rome, country: IT, profile: watch}
YAML
fi

say "Setting up Python environment"
[ -d "$IRIS_HOME/venv" ] || python3 -m venv "$IRIS_HOME/venv"
"$IRIS_HOME/venv/bin/pip" install -q --upgrade pip >/dev/null
"$IRIS_HOME/venv/bin/pip" install -q -r "$IRIS_HOME/requirements.txt"

# ---------------------------------------------------------------- secrets
ENV="$IRIS_HOME/.env"
touch "$ENV"; chmod 600 "$ENV"
getv() { grep -E "^$1=" "$ENV" | tail -1 | cut -d= -f2- || true; }
setv() { grep -vE "^$1=" "$ENV" > "$ENV.tmp" || true; echo "$1=$2" >> "$ENV.tmp"; mv "$ENV.tmp" "$ENV"; chmod 600 "$ENV"; }

if [ -z "$(getv TELEGRAM_BOT_TOKEN)" ]; then
  say "Telegram bot token"
  echo "Paste the token @BotFather gave you (input is hidden), then press Enter:"
  read -rs TOKEN; echo
  setv TELEGRAM_BOT_TOKEN "$TOKEN"
fi
TOKEN="$(getv TELEGRAM_BOT_TOKEN)"

if [ -z "$(getv TELEGRAM_CHAT_ID)" ]; then
  say "Linking your Telegram chat"
  for attempt in 1 2 3; do
    echo "Open Telegram, send any message (e.g. hello) to your bot, then press Enter here."
    read -r _
    CHAT=$(curl -fsS "https://api.telegram.org/bot${TOKEN}/getUpdates" | "$IRIS_HOME/venv/bin/python" -c '
import json,sys
d=json.load(sys.stdin); ids=[u["message"]["chat"]["id"] for u in d.get("result",[]) if "message" in u]
print(ids[-1] if ids else "")') || CHAT=""
    if [ -n "$CHAT" ]; then setv TELEGRAM_CHAT_ID "$CHAT"; echo "Linked to chat $CHAT"; break; fi
    warn "No message found yet (or the token is wrong)."
  done
  [ -n "$(getv TELEGRAM_CHAT_ID)" ] || { warn "Could not detect chat ID. Re-run the installer to retry."; exit 1; }
fi

if [ -z "$(getv HEALTHCHECK_URL)" ]; then
  say "healthchecks.io ping URL"
  echo "Paste your check's ping URL (https://hc-ping.com/...), or just press Enter to skip:"
  read -r HC
  [ -n "$HC" ] && setv HEALTHCHECK_URL "$HC"
fi

if [ -z "$(getv ANTHROPIC_API_KEY)" ]; then
  say "Anthropic API key (used from phase 2; you can add it now)"
  echo "Paste your API key (input is hidden), or press Enter to skip for now:"
  read -rs AK; echo
  [ -n "$AK" ] && setv ANTHROPIC_API_KEY "$AK"
fi

# ---------------------------------------------------------------- service
say "Installing the 'iris' command and the background service"
sudo tee /usr/local/bin/iris >/dev/null <<EOF
#!/usr/bin/env bash
case "\${1:-}" in
  logs)    exec journalctl -u iris -f -n 50 ;;
  update)  shift; exec /usr/local/bin/iris-update "\$@" ;;
  update-log) exec journalctl -u iris-update -n 40 --no-pager ;;
  restart) exec sudo systemctl restart iris ;;
  stop)    exec sudo systemctl stop iris ;;
  start)   exec sudo systemctl start iris ;;
  state)   exec systemctl status iris --no-pager ;;
  config)  exec \${EDITOR:-nano} "$IRIS_HOME/config/local.yaml" ;;
  *) cd "$IRIS_HOME" && IRIS_HOME="$IRIS_HOME" exec "$IRIS_HOME/venv/bin/python" -m iris "\$@" ;;
esac
EOF
sudo chmod 755 /usr/local/bin/iris

sudo tee /etc/systemd/system/iris.service >/dev/null <<EOF
[Unit]
Description=Iris security & alerts agent
Wants=network-online.target
After=network-online.target time-sync.target

[Service]
User=$USER
WorkingDirectory=$IRIS_HOME
Environment=IRIS_HOME=$IRIS_HOME
Environment=PYTHONUNBUFFERED=1
ExecStart=$IRIS_HOME/venv/bin/python -m iris run
Restart=always
RestartSec=15
NoNewPrivileges=true
ProtectSystem=full
MemoryMax=600M

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- self-updates
sudo install -m 755 "$TMP/deploy/iris-update.sh" /usr/local/bin/iris-update
# allow the updater (running as you) to restart Iris, and nothing else
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart iris" | sudo tee /etc/sudoers.d/iris >/dev/null
sudo chmod 440 /etc/sudoers.d/iris
sudo visudo -cf /etc/sudoers.d/iris >/dev/null || { sudo rm -f /etc/sudoers.d/iris; warn "sudoers rule invalid, removed"; }
sudo tee /etc/systemd/system/iris-update.service >/dev/null <<UNIT
[Unit]
Description=Iris self-update check
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$USER
Environment=IRIS_HOME=$IRIS_HOME
ExecStart=/usr/local/bin/iris-update
UNIT
sudo tee /etc/systemd/system/iris-update.timer >/dev/null <<UNIT
[Unit]
Description=Check for Iris updates every 10 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now iris-update.timer >/dev/null 2>&1

# Keep logs small and mostly in RAM-friendly limits (protects the SD card)
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=100M\n' | sudo tee /etc/systemd/journald.conf.d/iris.conf >/dev/null
sudo systemctl restart systemd-journald || true

say "Self-test (checks every source, Telegram and the heartbeat)"
iris selftest || warn "Some checks failed. Iris will still run and retry; tell Claude what failed."

sudo systemctl daemon-reload
sudo systemctl enable iris >/dev/null 2>&1
sudo systemctl restart iris
sleep 3
if systemctl is-active --quiet iris; then
  say "Iris is running ✔"
  echo "You should get a 'Iris started' message in Telegram. Useful commands on the Pi:"
  echo "  iris state      service status        iris logs     live logs"
  echo "  iris selftest   check sources         iris restart  restart"
  echo "  iris config     edit your settings (then: iris restart)"
  echo "  iris update     check for updates now     iris update-log   last update runs"
  if [ ! -d "$IRIS_HOME/repo/.git" ]; then
    echo
    echo "  Automatic updates are installed but not connected yet. Connect them with:  iris update --setup"
  fi
else
  warn "The service did not start. Show Claude the output of: journalctl -u iris -n 50"
  exit 1
fi
exit 0
__IRIS_PAYLOAD__
