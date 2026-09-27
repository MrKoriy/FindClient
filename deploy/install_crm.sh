#!/usr/bin/env bash
# Установка CRM для FindClient на сервере.
#
# Идемпотентен: повторный запуск не меняет пароль и не трогает базу.
# Запускать из каталога проекта на сервере:
#
#   cd /opt/2gi_scraper && bash deploy/install_crm.sh
#
# Пароль: если crm.env уже есть, он переиспользуется. Иначе берётся из
# переменной CRM_PASS, а если её нет — генерируется и печатается в конце.

set -euo pipefail

PROJECT="${PROJECT:-/opt/2gi_scraper}"
VENV="${VENV:-$PROJECT/venv}"
ENV_FILE="$PROJECT/crm.env"
HTPASSWD=/etc/nginx/.htpasswd-findclient
SITE=findclient-crm
DOMAIN=94-103-1-126.sslip.io

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "нужен root" >&2
    exit 1
fi

cd "$PROJECT"

say "1/7 Проверяю окружение"
[[ -x "$VENV/bin/python" ]] || { echo "нет $VENV/bin/python" >&2; exit 1; }
[[ -f "$PROJECT/crm/app.py" ]] || { echo "нет crm/app.py — сначала git pull" >&2; exit 1; }
"$VENV/bin/python" -c "import aiohttp, telethon" \
    || { echo "в venv нет aiohttp или telethon: $VENV/bin/pip install -r requirements.txt" >&2; exit 1; }

say "2/7 Готовлю $ENV_FILE"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    echo "файл уже есть, пароль оставляю прежним"
else
    CRM_USER="${CRM_USER:-leonid}"
    CRM_PASS="${CRM_PASS:-$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)}"
    cat > "$ENV_FILE" <<EOF
# CRM FindClient. Файл читает systemd, права 600.
CRM_HOST=127.0.0.1
CRM_PORT=8787
CRM_USER=$CRM_USER
CRM_PASS=$CRM_PASS
CRM_DB=$PROJECT/crm.db
DB_PATH=$PROJECT/scraper.db
CRM_LINK=https://leonidautomations.ru/demo/
TG_SESSION_FILE=/root/.hermes/telethon_vibecoders
EOF
    chmod 600 "$ENV_FILE"
    echo "создан новый"
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

say "3/7 Создаю $HTPASSWD"
printf '%s:%s\n' "$CRM_USER" "$(openssl passwd -apr1 "$CRM_PASS")" > "$HTPASSWD"
chmod 640 "$HTPASSWD"
chown root:www-data "$HTPASSWD" 2>/dev/null || true

say "4/7 Ставлю юниты systemd"
install -m 644 deploy/findclient-crm.service /etc/systemd/system/
install -m 644 deploy/findclient-sender.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now findclient-crm.service
systemctl enable --now findclient-sender.service
systemctl restart findclient-crm.service findclient-sender.service

say "5/7 Ставлю nginx"
install -m 644 deploy/nginx-findclient-crm.conf "/etc/nginx/sites-available/$SITE.conf"
ln -sf "/etc/nginx/sites-available/$SITE.conf" "/etc/nginx/sites-enabled/$SITE.conf"
if ! nginx -t; then
    echo "конфиг nginx не прошёл проверку — откатываю" >&2
    rm -f "/etc/nginx/sites-enabled/$SITE.conf"
    exit 1
fi
systemctl reload nginx

say "6/7 Заполняю базу: цели и группы"
cd "$PROJECT"
set -a; source "$ENV_FILE"; set +a
"$VENV/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("PROJECT", "/opt/2gi_scraper"))
from crm import db as crm_db
from crm.app import seed_templates

crm_db.init_db()
seed_templates()
try:
    print("целей добавлено:", crm_db.import_targets())
except FileNotFoundError as exc:
    print("цели не залиты:", exc)
from data.niches import NICHES
print("групп добавлено:", crm_db.import_groups(niches_module=NICHES))
s = crm_db.summary()
print("итог:", s["targets"]["all"], "целей,", s["groups_total"], "групп")
PY

say "7/7 Проверяю доступность"
sleep 3
code=$(curl -sk -o /dev/null -w '%{http_code}' "https://$DOMAIN/" -u "$CRM_USER:$CRM_PASS" || true)
echo "панель отвечает: HTTP $code"
if [[ "$code" != "200" ]]; then
    echo "панель не ответила 200 — смотри journalctl -u findclient-crm -n 50" >&2
fi

cat <<EOF

Готово.

  Панель:  https://$DOMAIN/
  Логин:   $CRM_USER
  Пароль:  $CRM_PASS

Отправка сейчас ВЫКЛЮЧЕНА и в холостом режиме. Включать в панели:
«Настройки» -> «Отправка разрешена» и снять «Холостой ход».

  systemctl status findclient-crm
  systemctl status findclient-sender
  systemctl stop findclient-sender     # остановить отправку, панель останется
EOF
