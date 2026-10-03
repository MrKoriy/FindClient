#!/usr/bin/env bash
# Выделенный непривилегированный пользователь для бота, панели и воркера.
#
#   source deploy/ensure_user.sh   # из deploy.sh / install_crm.sh (нужен root)
#
# Идемпотентен: создаёт системного пользователя `findclient`, отдаёт ему
# каталог проекта и переносит файл Telethon-сессии воркера из /root в
# $APP_DIR/.sessions (из /root сервис без root-прав его не прочтёт).
set -euo pipefail

APP_DIR="${APP_DIR:-${PROJECT:-/opt/2gi_scraper}}"
SVC_USER="${SVC_USER:-findclient}"

if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
    echo "создан пользователь $SVC_USER"
fi

install -d -m 700 -o "$SVC_USER" -g "$SVC_USER" "$APP_DIR/.sessions"

CRM_ENV="$APP_DIR/crm.env"
if [[ -f "$CRM_ENV" ]]; then
    old=$(grep -m1 '^TG_SESSION_FILE=' "$CRM_ENV" | cut -d= -f2- || true)
    if [[ -n "$old" && "$old" == /root/* ]]; then
        new="$APP_DIR/.sessions/$(basename "$old")"
        for f in "$old" "$old.session"; do
            [[ -f "$f" ]] && cp -a "$f" "$APP_DIR/.sessions/" && echo "сессия перенесена: $f"
        done
        sed -i "s#^TG_SESSION_FILE=.*#TG_SESSION_FILE=$new#" "$CRM_ENV"
        echo "TG_SESSION_FILE -> $new (оригинал в /root оставлен как резервная копия)"
    fi
fi

chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"
for f in "$APP_DIR/.env" "$APP_DIR/crm.env"; do
    [[ -f "$f" ]] && chmod 600 "$f"
done
true
