#!/usr/bin/env bash
# Установка CRM для FindClient на сервере.
#
# Идемпотентен: повторный запуск не меняет пароль и не трогает базу.
#
#   cd /opt/2gi_scraper && bash deploy/install_crm.sh
#
# Пароль: если crm.env уже есть, переиспользуется. Иначе берётся из CRM_PASS,
# а если её нет - генерируется и печатается в конце. CRM_SECRET_KEY генерируется
# один раз и дописывается в crm.env и .env бота (ссылки /crm подписывает бот).
#
# Обязательные переменные окружения при первом запуске:
#   CRM_HOSTNAME=crm.example.com   # имя, на которое есть сертификат Let's Encrypt
# Необязательные: CRM_PUBLIC_PORT (9444), CRM_LINK (ссылка в шаблонах).

set -euo pipefail

PROJECT="${PROJECT:-/opt/2gi_scraper}"
VENV="${VENV:-$PROJECT/venv}"
ENV_FILE="$PROJECT/crm.env"
HTPASSWD=/etc/nginx/.htpasswd-findclient
SITE=findclient-crm
PORT="${CRM_PUBLIC_PORT:-9444}"
HOSTNAME_="${CRM_HOSTNAME:-}"
if [[ -z "$HOSTNAME_" && -f "$ENV_FILE" ]]; then
    HOSTNAME_=$(grep -m1 '^CRM_HOSTNAME=' "$ENV_FILE" | cut -d= -f2- || true)
fi
[[ -n "$HOSTNAME_" ]] || { echo "задайте CRM_HOSTNAME=<домен панели>" >&2; exit 1; }
CERT_DIR="/etc/letsencrypt/live/$HOSTNAME_"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { echo "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "нужен root"

cd "$PROJECT"

say "1/7 Проверяю окружение"
[[ -x "$VENV/bin/python" ]] || die "нет $VENV/bin/python"
[[ -f "$PROJECT/crm/app.py" ]] || die "нет crm/app.py - сначала git pull"
[[ -f "$CERT_DIR/fullchain.pem" ]] || die "нет сертификата для $HOSTNAME_"
"$VENV/bin/python" -c "import aiohttp, telethon" \
    || die "в venv нет aiohttp или telethon: $VENV/bin/pip install -r requirements.txt"

say "2/7 Проверяю, что имя $HOSTNAME_ на порту $PORT свободно"
# На 443 это имя может обслуживать другой сайт сервера. Порт панели уникальный -
# конфликт server_name невозможен, но проверяем и падаем, а не полагаемся на это.
conflicts=$(grep -rlE "listen[^;]*[^0-9]$PORT\b" /etc/nginx/sites-enabled/ 2>/dev/null \
            | grep -v "$SITE" || true)
[[ -z "$conflicts" ]] || die "порт $PORT уже занят в nginx: $conflicts"
echo "свободно"

say "3/7 Готовлю $ENV_FILE"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    echo "файл уже есть, пароль оставляю прежним"
else
    CRM_USER="${CRM_USER:-owner}"
    CRM_PASS="${CRM_PASS:-$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)}"
    cat > "$ENV_FILE" <<EOF
# CRM FindClient. Файл читает systemd, права 600.
CRM_HOST=127.0.0.1
CRM_PORT=8787
CRM_USER=$CRM_USER
CRM_PASS=$CRM_PASS
CRM_DB=$PROJECT/crm.db
DB_PATH=$PROJECT/scraper.db
CRM_LINK=${CRM_LINK:-}
CRM_HOSTNAME=$HOSTNAME_
TG_SESSION_FILE=$PROJECT/.sessions/crm_sender
EOF
    chmod 600 "$ENV_FILE"
    echo "создан новый"
fi
# Секрет подписи magic-ссылок: один и тот же в crm.env и .env бота.
if ! grep -q '^CRM_SECRET_KEY=.\{32,\}' "$ENV_FILE"; then
    SECRET=$(grep -m1 '^CRM_SECRET_KEY=' "$PROJECT/.env" 2>/dev/null | cut -d= -f2- || true)
    [[ ${#SECRET} -ge 32 ]] || SECRET=$("$VENV/bin/python" -c "import secrets; print(secrets.token_urlsafe(48))")
    sed -i '/^CRM_SECRET_KEY=/d' "$ENV_FILE"
    echo "CRM_SECRET_KEY=$SECRET" >> "$ENV_FILE"
    echo "CRM_SECRET_KEY записан в crm.env"
fi
SECRET=$(grep -m1 '^CRM_SECRET_KEY=' "$ENV_FILE" | cut -d= -f2-)
if [[ -f "$PROJECT/.env" ]] && ! grep -qx "CRM_SECRET_KEY=$SECRET" "$PROJECT/.env"; then
    sed -i '/^CRM_SECRET_KEY=/d' "$PROJECT/.env"
    echo "CRM_SECRET_KEY=$SECRET" >> "$PROJECT/.env"
    echo "CRM_SECRET_KEY записан в .env бота - перезапустите 2gi-scraper"
fi
if ! grep -qE '^OWNER_IDS=[0-9]' "$ENV_FILE"; then
    OWN=$(grep -m1 '^OWNER_IDS=' "$PROJECT/.env" 2>/dev/null | cut -d= -f2- || true)
    [[ -n "$OWN" ]] || die "OWNER_IDS не задан ни в crm.env, ни в .env - вход по ссылке будет закрыт"
    sed -i '/^OWNER_IDS=/d' "$ENV_FILE"
    echo "OWNER_IDS=$OWN" >> "$ENV_FILE"
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

say "4/7 Создаю $HTPASSWD"
printf '%s:%s\n' "$CRM_USER" "$(openssl passwd -apr1 "$CRM_PASS")" > "$HTPASSWD"
chmod 640 "$HTPASSWD"
chown root:www-data "$HTPASSWD" 2>/dev/null || true

say "5/7 Пользователь сервиса и юниты systemd"
APP_DIR="$PROJECT" bash "$PROJECT/deploy/ensure_user.sh"
install -m 644 deploy/findclient-crm.service /etc/systemd/system/
install -m 644 deploy/findclient-sender.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now findclient-crm.service >/dev/null
systemctl enable --now findclient-sender.service >/dev/null
systemctl restart findclient-crm.service findclient-sender.service

say "6/7 Ставлю конфиг nginx и открываю порт"
sed -e "s/__CRM_HOSTNAME__/$HOSTNAME_/g" -e "s/__CRM_PUBLIC_PORT__/$PORT/g" \
    deploy/nginx-findclient-crm.conf > "/etc/nginx/sites-available/$SITE.conf"
chmod 644 "/etc/nginx/sites-available/$SITE.conf"
ln -sf "/etc/nginx/sites-available/$SITE.conf" "/etc/nginx/sites-enabled/$SITE.conf"
if ! nginx -t; then
    rm -f "/etc/nginx/sites-enabled/$SITE.conf"
    systemctl reload nginx || true
    die "конфиг nginx не прошёл проверку - откатил, чужие сайты не тронуты"
fi
systemctl reload nginx
ufw allow "$PORT/tcp" >/dev/null 2>&1 || true

say "7/7 Заполняю базу и проверяю"
set -a; source "$ENV_FILE"; set +a
# Функции crm.db асинхронные - без asyncio.run они бы вернули корутины и ничего не сделали.
sudo -u findclient --preserve-env PROJECT="$PROJECT" "$VENV/bin/python" - <<'PY'
import asyncio, os, sys
sys.path.insert(0, os.environ.get("PROJECT", "/opt/2gi_scraper"))
from crm import db as crm_db
from crm.app import seed_templates
from data.niches import NICHES


async def main() -> None:
    await crm_db.init_db()
    await seed_templates()
    try:
        print("целей добавлено:", await crm_db.import_targets())
    except FileNotFoundError as exc:
        print("цели не залиты:", exc)
    print("групп добавлено:", await crm_db.import_groups(niches_module=NICHES))
    s = await crm_db.summary()
    print("итог:", s["targets"]["all"], "целей,", s["groups_total"], "групп")


asyncio.run(main())
PY

sleep 3
URL="https://$HOSTNAME_:$PORT/"
code=$(curl -sk -o /dev/null -w '%{http_code}' "$URL" -u "$CRM_USER:$CRM_PASS" || true)
echo "панель отвечает: HTTP $code"
[[ "$code" == "200" ]] || echo "панель не ответила 200 - смотри journalctl -u findclient-crm -n 50" >&2


cat <<EOF

Готово.

  Панель:  $URL
  Логин:   $CRM_USER
  Пароль:  $CRM_PASS

Отправка сейчас ВЫКЛЮЧЕНА и в холостом режиме. Включать в панели:
«Настройки» -> «Отправка разрешена» и снять «Холостой ход».

  systemctl status findclient-crm
  systemctl status findclient-sender
  systemctl stop findclient-sender     # остановить отправку, панель останется
EOF
