#!/usr/bin/env bash
# Idempotent deploy/update of FindClient on a Debian/Ubuntu server.
#
#   sudo bash deploy.sh              # install or update to origin/main
#   sudo bash deploy.sh --no-start   # install/update but don't (re)start the service
#
# What it does: system deps -> service user -> clone or fast-forward pull ->
# venv + pip install -r requirements.txt -> install systemd unit -> start.
# It never touches .env or scraper.db if they already exist.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/findclient}
APP_USER=${APP_USER:-findclient}
REPO=${REPO:-https://github.com/MrKoriy/FindClient.git}
BRANCH=${BRANCH:-main}
START=1
[ "${1:-}" = "--no-start" ] && START=0

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash deploy.sh"

log "System packages"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 python3-pip git curl ca-certificates
else
  die "no apt-get/dnf — install python3 (>=3.10), venv, pip, git manually"
fi

PY=python3
"$PY" - <<'PY' || die "python3 is older than 3.10 (the code uses X | None / match-style typing)"
import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
echo "python: $($PY -V)"

log "Service user"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
  echo "created user $APP_USER"
else
  echo "user $APP_USER exists"
fi

log "Code"
mkdir -p "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" remote set-url origin "$REPO"
  git -C "$APP_DIR" fetch --prune origin
  before=$(git -C "$APP_DIR" rev-parse --short HEAD)
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" merge --ff-only "origin/$BRANCH"
  after=$(git -C "$APP_DIR" rev-parse --short HEAD)
  echo "code: $before -> $after"
else
  git clone --branch "$BRANCH" "$REPO" "$APP_DIR"
  echo "code: cloned $(git -C "$APP_DIR" rev-parse --short HEAD)"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

log "Python environment"
[ -x "$APP_DIR/.venv/bin/python" ] || "$PY" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade -q pip wheel
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
echo "installed: $("$APP_DIR/.venv/bin/pip" list --format=freeze | wc -l) packages"

log "systemd unit"
install -m 0644 "$APP_DIR/deploy/findclient.service" /etc/systemd/system/findclient.service
systemctl daemon-reload
systemctl enable findclient >/dev/null

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  log "STOP: fill in $APP_DIR/.env"
  cat <<EOF
Created $APP_DIR/.env from .env.example. Edit it:

    nano $APP_DIR/.env      # BOT_TOKEN is required, OWNER_IDS strongly recommended

Then:

    systemctl start findclient
    journalctl -u findclient -f

EOF
  exit 0
fi
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

if grep -q 'your_telegram_bot_token_here' "$APP_DIR/.env"; then
  die "$APP_DIR/.env still holds the placeholder BOT_TOKEN — put the real token in and re-run"
fi

if [ "$START" -eq 1 ]; then
  log "Restart"
  # Stop anything else already polling this token (old deployment), or Telegram
  # returns 409 Conflict and neither copy works.
  systemctl stop findclient 2>/dev/null || true
  systemctl start findclient
  sleep 3
  systemctl --no-pager --lines=25 status findclient || true
  log "Log"
  journalctl -u findclient -n 30 --no-pager || true
else
  echo "--no-start: service installed but not started"
fi

log "Done. Tail logs with: journalctl -u findclient -f"
