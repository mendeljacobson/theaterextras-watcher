#!/usr/bin/env bash
# One-shot installer for the TheaterExtras watcher on a fresh Ubuntu server.
# Safe to re-run: it updates the script and restarts the service.
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/mendeljacobson/theaterextras-watcher/main"
DIR="/opt/te-watcher"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run this as root (you are, if you opened the DigitalOcean console)." >&2
  exit 1
fi

echo "==> Installing Python..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 curl >/dev/null

echo "==> Downloading the watcher..."
mkdir -p "$DIR/state"
curl -fsSL -o "$DIR/watch.py" "$REPO_RAW/watch.py"
python3 -c "import ast,sys; ast.parse(open('$DIR/watch.py').read())" \
  || { echo "The downloaded script is not valid Python - stopping." >&2; exit 1; }

if [ -f "$DIR/env" ]; then
  echo "==> Existing settings found; keeping them."
else
  echo
  echo "Two things to paste. Neither is echoed anywhere but this file,"
  echo "which is readable only by root."
  echo
  printf "TheaterExtras access token: "
  read -r TE_TOK
  printf "ntfy topic name: "
  read -r NTFY_T
  if [ -z "$TE_TOK" ] || [ -z "$NTFY_T" ]; then
    echo "Both values are required - run this again." >&2
    exit 1
  fi
  cat > "$DIR/env" <<ENVEOF
TE_ACCESS_TOKEN=$TE_TOK
NTFY_TOPIC=$NTFY_T
TE_EXCLUDE_REGIONS=Los Angeles
ALERT_NEW_SHOWTIMES=on
ALERT_TICKET_DROPS=on
AVAIL_COOLDOWN_HOURS=2
MAX_INDIVIDUAL=20
REPEATS=1000000000
SLEEP_SECONDS=30
HEARTBEAT_HOURS=168
STATE_PATH=$DIR/state/seen.json
ENVEOF
  chmod 600 "$DIR/env"
fi

echo "==> Installing the service..."
cat > /etc/systemd/system/te-watcher.service <<'UNITEOF'
[Unit]
Description=TheaterExtras listing watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/te-watcher
EnvironmentFile=/opt/te-watcher/env
ExecStart=/usr/bin/python3 /opt/te-watcher/watch.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable te-watcher >/dev/null 2>&1
systemctl restart te-watcher
sleep 6

echo
if systemctl is-active --quiet te-watcher; then
  echo "==> RUNNING. Recent output:"
  echo
  journalctl -u te-watcher -n 12 --no-pager -o cat || true
  echo
  echo "All set. It checks every 30 seconds and restarts itself if it ever crashes."
  echo "Watch it live:  journalctl -u te-watcher -f     (Ctrl+C to stop watching)"
else
  echo "==> IT DID NOT START. The error is below:"
  echo
  journalctl -u te-watcher -n 25 --no-pager -o cat || true
fi
