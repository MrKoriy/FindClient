"""Общая защита первого контакта для двух систем рассылок.

В проекте две независимые отправки: рассылки бота (`scraper.db`: crm_leads,
stoplist) и CRM-панель с воркером (`crm.db`: targets, messages). У каждой свой
дедуп и стоп-лист, поэтому человек мог получить первое сообщение из обеих.
Перед первым сообщением каждая система спрашивает другую базу (только чтение):
писали ли этому получателю и не просил ли он не писать.

Нет файла или таблицы - другой системы нет, проверка проходит. Любая другая
ошибка (например, база заблокирована) пробрасывается: вызывающий код в этом
случае не отправляет (fail-closed).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3


def _norm(username: str) -> str:
    return (username or "").strip().lstrip("@").lower()


def _query(path: str, sql_and_params: list[tuple[str, tuple]]) -> bool:
    if not path or not os.path.exists(path):
        return False
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        for sql, params in sql_and_params:
            try:
                if conn.execute(sql, params).fetchone():
                    return True
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc) or "no such column" in str(exc):
                    continue
                raise
        return False
    finally:
        conn.close()


def _panel_has(path: str, username: str) -> bool:
    u = _norm(username)
    if not u:
        return False
    return _query(path, [
        ("SELECT 1 FROM messages WHERE lower(ltrim(chat, '@')) = ? AND status IN ('sent', 'sending') LIMIT 1", (u,)),
        ("SELECT 1 FROM targets WHERE lower(ltrim(username, '@')) = ?"
         " AND status IN ('sent', 'replied', 'refused', 'blocked') LIMIT 1", (u,)),
    ])


def _bot_has(path: str, username: str) -> bool:
    u = _norm(username)
    if not u:
        return False
    return _query(path, [
        ("SELECT 1 FROM stoplist WHERE key = ? LIMIT 1", ("u:" + u,)),
        ("SELECT 1 FROM crm_leads WHERE lower(tg_username) = ? AND status NOT IN ('new', 'failed') LIMIT 1", (u,)),
    ])


async def contacted_by_panel(username: str, crm_db_path: str) -> bool:
    """Для бота: писала ли этому @username CRM-панель или он там отказался/заблокировал."""
    return await asyncio.to_thread(_panel_has, crm_db_path, username)


async def contacted_by_bot(username: str, scraper_db_path: str) -> bool:
    """Для воркера панели: писал ли бот этому @username или он в стоп-листе бота."""
    return await asyncio.to_thread(_bot_has, scraper_db_path, username)
