#!/usr/bin/env bash
# Установка CRM для FindClient на сервере.
#
# Идемпотентен: повторный запуск не меняет пароль и не трогает базу.
#
#   cd /opt/2gi_scraper && bash deploy/install_crm.sh
#
# Пароль: если crm.env уже есть, переиспользуется. Иначе берётся из CRM_PASS,
# а если её нет - генерируется и печатается в конце.

set -euo pipefail

PROJECT="${PROJECT:-/opt/2gi_scraper}"
VENV="${VENV:-$PROJECT/venv}"
ENV_FILE="$PROJECT/crm.env"
HTPASSWD=/etc/nginx/.htpasswd-findclient
SITE=findclient-crm
PORT=9444
HOSTNAME_=94-103-1-126.sslip.io
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
# На 443 то же имя занято shift-plus.conf, и это не пустой конфиг: там
# /vless-xhttp -> Xray (127.0.0.1:10085) и / -> 127.0.0.1:8080. Первая версия
# панели встала туда же, nginx выбрал её первой по алфавиту файлов, и чужой
# VLESS-эндпоинт начал отдавать 401. Порт уникальный - конфликт невозможен,
# но проверяем и падаем, а не полагаемся на это.
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

say "4/7 Создаю $HTPASSWD"
printf '%s:%s\n' "$CRM_USER" "$(openssl passwd -apr1 "$CRM_PASS")" > "$HTPASSWD"
chmod 640 "$HTPASSWD"
chown root:www-data "$HTPASSWD" 2>/dev/null || true

say "5/7 Ставлю юниты systemd"
install -m 644 deploy/findclient-crm.service /etc/systemd/system/
install -m 644 deploy/findclient-sender.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now findclient-crm.service >/dev/null
systemctl enable --now findclient-sender.service >/dev/null
systemctl restart findclient-crm.service findclient-sender.service

say "6/7 Ставлю конфиг nginx и открываю порт"
install -m 644 deploy/nginx-findclient-crm.conf "/etc/nginx/sites-available/$SITE.conf"
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
URL="https://$HOSTNAME_:$PORT/"
code=$(curl -sk -o /dev/null -w '%{http_code}' "$URL" -u "$CRM_USER:$CRM_PASS" || true)
echo "панель отвечает: HTTP $code"
[[ "$code" == "200" ]] || echo "панель не ответила 200 - смотри journalctl -u findclient-crm -n 50" >&2

# Чужой VLESS-эндпоинт должен быть нетронут. 401 здесь означал бы, что мы
# его перехватили: это уже случалось, поэтому проверка встроена.
vless=$(curl -sk -o /dev/null -w '%{http_code}' "https://$HOSTNAME_/vless-xhttp" || true)
echo "чужой /vless-xhttp: HTTP $vless (401 = перехватили бы)"
[[ "$vless" == "401" ]] && echo "ВНИМАНИЕ: перехватили чужой эндпоинт!" >&2 || true

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
