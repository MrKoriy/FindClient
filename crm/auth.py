"""Модуль генерации и проверки одноразовых токенов и сессий для входа в CRM."""

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
SESSION_TTL = 30 * 86400  # 30 дней жизни сессионной куки


def get_auth_secret() -> str:
    """Возвращает общий секрет для подписи токенов и сессий."""
    return (
        os.environ.get("CRM_SECRET_KEY")
        or os.environ.get("CRM_PASS")
        or os.environ.get("BOT_TOKEN")
        or "findclient-fallback-secret-key-2026"
    )


def get_owner_ids() -> set[int]:
    """Возвращает список ID владельцев, которым разрешен вход."""
    raw = os.environ.get("OWNER_IDS", "")
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def generate_magic_token(user_id: int, secret: str = "") -> str:
    """Генерирует криптографически подписанный одноразовый токен для Telegram-пользователя."""
    sec = secret or get_auth_secret()
    now = int(time.time())
    nonce = secrets.token_hex(6)
    payload = f"{user_id}:{now}:{nonce}"
    sig = hmac.new(sec.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8").rstrip("=")


def verify_magic_token(token_str: str, secret: str = "", max_age: int = MAGIC_TOKEN_TTL) -> int | None:
    """Проверяет валидность токена и возвращает user_id, если всё верно."""
    if not token_str:
        return None
    sec = secret or get_auth_secret()
    padded = token_str + "=" * (-len(token_str) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        parts = raw.split(":")
        if len(parts) != 4:
            return None
        user_id_str, ts_str, nonce, sig = parts
        user_id = int(user_id_str)
        ts = int(ts_str)
    except Exception:
        return None

    now = int(time.time())
    if abs(now - ts) > max_age:
        return None

    payload = f"{user_id}:{ts}:{nonce}"
    expected_sig = hmac.new(sec.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected_sig):
        return None

    owners = get_owner_ids()
    if owners and user_id not in owners:
        return None

    return user_id


def create_session_cookie(user_id: int, secret: str = "") -> str:
    """Создает подписанное значение для сессионной куки crm_session."""
    sec = secret or get_auth_secret()
    now = int(time.time())
    payload = f"{user_id}:{now}"
    sig = hmac.new(sec.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8").rstrip("=")


def verify_session_cookie(cookie_val: str, secret: str = "", max_age: int = SESSION_TTL) -> int | None:
    """Проверяет сессионную куку и возвращает user_id."""
    if not cookie_val:
        return None
    sec = secret or get_auth_secret()
    padded = cookie_val + "=" * (-len(cookie_val) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        parts = raw.split(":")
        if len(parts) != 3:
            return None
        user_id_str, ts_str, sig = parts
        user_id = int(user_id_str)
        ts = int(ts_str)
    except Exception:
        return None

    now = int(time.time())
    if abs(now - ts) > max_age:
        return None

    payload = f"{user_id}:{ts}"
    expected_sig = hmac.new(sec.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected_sig):
        return None

    owners = get_owner_ids()
    if owners and user_id not in owners:
        return None

    return user_id
