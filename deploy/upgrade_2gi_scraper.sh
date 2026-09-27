#!/usr/bin/env bash
# In-place upgrade of the EXISTING /opt/2gi_scraper deployment to origin/main.
# Keeps: venv, .env (BOT_TOKEN), scraper.db, systemd unit name.
# Makes:  a real git clone, adds OWNER_IDS, drops the dead TWOGIS_API_KEY.
# Rollback: tar is printed at the start; `systemctl start 2gi-scraper` after restoring.
set -euo pipefail

APP_DIR=/opt/2gi_scraper
SERVICE=2gi-scraper.service
REPO=${REPO:-https://github.com/MrKoriy/FindClient.git}
BRANCH=${BRANCH:-main}
OWNER_IDS=${OWNER_IDS:-1432816193}
TS=$(date +%Y%m%d-%H%M%S)
BACKUP=/root/2gi_scraper-backup-$TS.tar.gz

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"
[ -d "$APP_DIR" ] || die "$APP_DIR not found"

log "Backup -> $BACKUP"
tar czf "$BACKUP" -C /opt \
  --exclude='2gi_scraper/venv' \
  --exclude='2gi_scraper/__pycache__' \
  --exclude='2gi_scraper/.pytest_cache' \
  2gi_scraper
ls -lh "$BACKUP"

log "Stop service"
systemctl stop "$SERVICE"

log "Stash .env and database"
cp -a "$APP_DIR/.env" /root/2gi_scraper.env.keep
[ -f "$APP_DIR/scraper.db" ] && cp -a "$APP_DIR/scraper.db" /root/2gi_scraper.db.keep && echo "db stashed"

log "Clear old code (venv / .env / scraper.db kept)"
find "$APP_DIR" -mindepth 1 -maxdepth 1 \
  ! -name venv ! -name .env ! -name scraper.db -exec rm -rf {} +
ls -A "$APP_DIR"

log "Clone $BRANCH"
rm -rf /root/fc-clone
git clone --quiet --branch "$BRANCH" "$REPO" /root/fc-clone
cp -a /root/fc-clone/. "$APP_DIR/"
rm -rf /root/fc-clone
git -C "$APP_DIR" --no-pager log -1 --format='code: %h %ad %s' --date=short

log "Dependencies into the existing venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/python3" - <<'PY'
import aiogram, telethon, openpyxl, curl_cffi, aiosqlite, aiohttp
print("deps ok: aiogram", aiogram.__version__, "| telethon", telethon.__version__)
PY

log "Write .env (BOT_TOKEN carried over, OWNER_IDS added, TWOGIS_API_KEY dropped)"
TOKEN=$(grep -m1 '^BOT_TOKEN=' /root/2gi_scraper.env.keep | cut -d= -f2- | tr -d '\r\n')
[ -n "$TOKEN" ] || die "no BOT_TOKEN found in the previous .env"
cat > "$APP_DIR/.env" <<EOF
BOT_TOKEN=$TOKEN
OWNER_IDS=$OWNER_IDS
YANDEX_API_KEY=
HTTP_PROXY=
TG_API_ID=
TG_API_HASH=
TG_SESSION=
ORDERS_POLL_INTERVAL=300
REQUEST_DELAY=0.3
DB_PATH=scraper.db
EOF
chmod 600 "$APP_DIR/.env"
echo "wrote $APP_DIR/.env: $(grep -c . "$APP_DIR/.env") lines"

log "systemd unit"
cat > "/etc/systemd/system/$SERVICE" <<UNIT
[Unit]
Description=FindClient Telegram bot (maps + Telegram leads + freelance orders)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$APP_DIR/venv/bin/python3 bot.py
Restart=always
RestartSec=5
TimeoutStopSec=20
KillSignal=SIGINT
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes
StandardOutput=journal
StandardError=journal
SyslogIdentifier=2gi-scraper

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null

log "Start"
systemctl start "$SERVICE"
sleep 6
systemctl is-active "$SERVICE" || true
systemctl --no-pager --lines=0 status "$SERVICE" | head -12 || true

log "Log"
journalctl -u "$SERVICE" -n 40 --no-pager || true

log "Rollback if needed"
echo "  systemctl stop $SERVICE"
echo "  tar xzf $BACKUP -C /opt   # restores the old tree"
echo "  systemctl start $SERVICE"
