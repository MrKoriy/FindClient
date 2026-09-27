#!/usr/bin/env bash
# Установка CRM для FindClient на сервере.
#
# Идемпотентен: повторный запуск не меняет пароль, не перевыпускает
# сертификат и не трогает базу.
#
#   cd /opt/2gi_scraper && bash deploy/install_crm.sh
#
# Пароль: если crm.env уже есть, переиспользуется. Иначе берётся из CRM_PASS,
# а если её нет — генерируется и печатается в конце.

set -euo pipefail

PROJECT="${PROJECT:-/opt/2gi_scraper}"
VENV="${VENV:-$PROJECT/venv}"
ENV_FILE="$PROJECT/crm.env"
HTPASSWD=/etc/nginx/.htpasswd-findclient
SITE=findclient-crm
DOMAIN=crm.94-103-1-126.sslip.io
CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
WEBROOT=/var/www/certbot

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { echo "$*" >&2; exit 1; }

if [[ $EUID -ne 0 ]]; then
    die "нужен root"
fi

cd "$PROJECT"

say "1/8 Проверяю окружение"
[[ -x "$VENV/bin/python" ]] || die "нет $VENV/bin/python"
[[ -f "$PROJECT/crm/app.py" ]] || die "нет crm/app.py — сначала git pull"
"$VENV/bin/python" -c "import aiohttp, telethon" \
    || die "в venv нет aiohttp или telethon: $VENV/bin/pip install -r requirements.txt"

say "2/8 Проверяю, что имя $DOMAIN свободно"
# Голое 94-103-1-126.sslip.io занято shift-plus.conf: там VLESS-эндпоинт
# /vless-xhttp -> Xray и / -> :8080. Если сесть на то же имя, nginx выберет
# первый по алфавиту файл и чужая точка входа молча отвалится. Поэтому
# проверяем явно и падаем, а не полагаемся на порядок файлов.
conflicts=$(grep -rl "server_name[^;]*\b$DOMAIN\b" /etc/nginx/sites-enabled/ 2>/dev/null \
            | grep -v "$SITE" || true)
if [[ -n "$conflicts" ]]; then
    die "имя $DOMAIN уже занято: $conflicts
Возьмите другое имя или уберите тот конфиг — молча перекрывать нельзя."
fi
echo "свободно"

say "3/8 Готовлю $ENV_FILE"
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

say "4/8 Создаю $HTPASSWD"
printf '%s:%s\n' "$CRM_USER" "$(openssl passwd -apr1 "$CRM_PASS")" > "$HTPASSWD"
chmod 640 "$HTPASSWD"
chown root:www-data "$HTPASSWD" 2>/dev/null || true

say "5/8 Ставлю юниты systemd"
install -m 644 deploy/findclient-crm.service /etc/systemd/system/
install -m 644 deploy/findclient-sender.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now findclient-crm.service >/dev/null
systemctl enable --now findclient-sender.service >/dev/null
systemctl restart findclient-crm.service findclient-sender.service

say "6/8 Сертификат"
mkdir -p "$WEBROOT"
if [[ -f "$CERT_DIR/fullchain.pem" ]]; then
    echo "сертификат уже есть"
else
    echo "выписываю через certbot (порт 80, webroot)"
    install -m 644 deploy/nginx-findclient-crm-acme.conf "/etc/nginx/sites-available/$SITE.conf"
    ln -sf "/etc/nginx/sites-available/$SITE.conf" "/etc/nginx/sites-enabled/$SITE.conf"
    nginx -t >/dev/null || { rm -f "/etc/nginx/sites-enabled/$SITE.conf"; die "черновой конфиг не прошёл проверку"; }
    systemctl reload nginx
    if ! certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" \
            --non-interactive --agree-tos --register-unsafely-without-email --keep-until-expiring; then
        rm -f "/etc/nginx/sites-enabled/$SITE.conf"
        systemctl reload nginx
        die "certbot не смог выписать сертификат — панель не поднята, чужие сайты не тронуты"
    fi
fi

say "7/8 Ставлю боевой конфиг nginx"
install -m 644 deploy/nginx-findclient-crm.conf "/etc/nginx/sites-available/$SITE.conf"
ln -sf "/etc/nginx/sites-available/$SITE.conf" "/etc/nginx/sites-enabled/$SITE.conf"
if ! nginx -t; then
    rm -f "/etc/nginx/sites-enabled/$SITE.conf"
    systemctl reload nginx || true
    die "конфиг nginx не прошёл проверку — откатил, чужие сайты не тронуты"
fi
systemctl reload nginx

say "8/8 Заполняю базу и проверяю"
set -a; source "$ENV_FILE"; set +a
PROJECT="$PROJECT" "$VENV/bin/python" - <<'PY'
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

sleep 3
code=$(curl -sk -o /dev/null -w '%{http_code}' "https://$DOMAIN/" -u "$CRM_USER:$CRM_PASS" || true)
echo "панель отвечает: HTTP $code"

# Заодно убеждаемся, что чужой VLESS-эндпоинт не тронут.
vless=$(curl -sk -o /dev/null -w '%{http_code}' https://94-103-1-126.sslip.io/vless-xhttp || true)
echo "чужой /vless-xhttp: HTTP $vless (401 означал бы, что мы его перехватили)"
[[ "$vless" == "401" ]] && echo "ВНИМАНИЕ: похоже, перехватили чужой эндпоинт!" >&2

[[ "$code" == "200" ]] || echo "панель не ответила 200 — смотри journalctl -u findclient-crm -n 50" >&2

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
