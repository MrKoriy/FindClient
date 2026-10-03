"""Вход в CRM: одноразовые magic-ссылки из бота и серверные сессии.

Модель безопасности:
- подпись magic-ссылок делается только `CRM_SECRET_KEY` (>= 32 символа), никаких
  запасных секретов вроде CRM_PASS / BOT_TOKEN / зашитых строк;
- пустой OWNER_IDS = вход по ссылке запрещён всем (fail-closed);
- nonce ссылки фиксируется в базе при первом использовании - повтор не пройдёт;
- сессия - случайный идентификатор, в базе хранится только его sha256, поэтому
  logout и «выйти везде» реально отзывают доступ.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from dotenv import load_dotenv

load_dotenv()
load_dotenv("crm.env")

MAGIC_TOKEN_TTL = 900  # 15 минут на вход по ссылке
SESSION_TTL = int(os.environ.get("CRM_SESSION_TTL", str(7 * 86400)) or 7 * 86400)  # 7 дней
MIN_SECRET_LEN = 32
# Секреты, которые когда-либо были в репозитории: они публичны и не годятся.
_KNOWN_PUBLIC_SECRETS = frozenset({
    "findclient-fallback-secret-key-2026",
    "test-secret-key-for-pytest-only-32chars!!",
})


class AuthConfigError(RuntimeError):
    """CRM настроена небезопасно - вход запрещён до исправления конфигурации."""


def get_auth_secret() -> str:
    """Секрет подписи magic-ссылок. Только CRM_SECRET_KEY, fail-closed."""
    sec = (os.environ.get("CRM_SECRET_KEY") or "").strip()
    if len(sec) < MIN_SECRET_LEN or sec in _KNOWN_PUBLIC_SECRETS:
        raise AuthConfigError(
            "CRM_SECRET_KEY не задан или короче 32 символов. "
            "Сгенерируйте: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return sec


def get_owner_ids() -> set[int]:
    """ID владельцев, которым разрешён вход по ссылке. Пусто = никому."""
    raw = os.environ.get("OWNER_IDS", "")
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def is_owner(user_id: int | None) -> bool:
    owners = get_owner_ids()
    return bool(owners) and user_id in owners


def _sign(sec: str, payload: str) -> str:
    return hmac.new(sec.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_magic_token(user_id: int, secret: str = "") -> str:
    """Подписанная одноразовая ссылка для Telegram-пользователя."""
    sec = secret or get_auth_secret()
    now = int(time.time())
    nonce = secrets.token_hex(16)
    payload = f"{user_id}:{now}:{nonce}"
    raw = f"{payload}:{_sign(sec, payload)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8").rstrip("=")


def parse_magic_token(
    token_str: str, secret: str = "", max_age: int = MAGIC_TOKEN_TTL
) -> tuple[int, str] | None:
    """Проверяет подпись, срок и владельца. Возвращает (user_id, nonce) без потребления."""
    if not token_str or len(token_str) > 512:
        return None
    sec = secret or get_auth_secret()
    padded = token_str + "=" * (-len(token_str) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        user_id_str, ts_str, nonce, sig = raw.split(":")
        user_id = int(user_id_str)
        ts = int(ts_str)
    except Exception:
        return None

    now = int(time.time())
    if ts > now + 60 or now - ts > max_age:
        return None
    if not hmac.compare_digest(sig, _sign(sec, f"{user_id}:{ts}:{nonce}")):
        return None
    if not is_owner(user_id):
        return None
    return user_id, nonce


def verify_magic_token(token_str: str, secret: str = "", max_age: int = MAGIC_TOKEN_TTL) -> int | None:
    """Проверка без потребления nonce (для тестов и диагностики). Для входа - consume_magic_token."""
    parsed = parse_magic_token(token_str, secret, max_age)
    return parsed[0] if parsed else None


async def consume_magic_token(token_str: str, crm_db_path: str | None = None) -> int | None:
    """Проверяет ссылку и атомарно помечает её nonce использованным. Второй раз - None."""
    parsed = parse_magic_token(token_str)
    if not parsed:
        return None
    from crm import db as crm_db

    user_id, nonce = parsed
    if not await crm_db.consume_nonce(nonce, crm_db=crm_db_path):
        return None
    return user_id


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def hash_session_id(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()
