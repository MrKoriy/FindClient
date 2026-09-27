"""Асинхронный POST-JSON клиент с повторами для внешних API CRM.

TypeSafe Jev и B.AI раньше звались через urllib.request прямо из
HTTP-хендлеров панели: один медленный ответ провайдера замораживал весь
интерфейс. Теперь все вызовы идут через aiohttp - событийный цикл панель
не блокируется, а параллельные запросы продолжают обслуживаться.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger("crm.http")

ATTEMPTS = 3
TIMEOUT = 12.0


async def post_json(
    url: str,
    payload: dict,
    headers: dict,
    attempts: int = ATTEMPTS,
    timeout: float = TIMEOUT,
) -> dict | None:
    """POST c JSON-телом, возвращает ответ или None, если все попытки иссякли.

    Ошибки 4xx (кроме 429) не ретраятся: неверный ключ или кривой запрос
    повторением не исправить. Обрывы связи, таймауты и 5xx - ретраятся.
    """
    last_error: Exception | str | None = None
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        for attempt in range(1, attempts + 1):
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if 200 <= resp.status < 300:
                        return await resp.json(content_type=None)
                    body = (await resp.text())[:200]
                    if 400 <= resp.status < 500 and resp.status != 429:
                        log.warning("POST %s -> %s: %s (повтора не будет)", url, resp.status, body)
                        return None
                    last_error = f"HTTP {resp.status}: {body}"
            except (TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
            if attempt < attempts:
                await asyncio.sleep(0.5 * attempt)
    log.warning("POST %s не удался за %s попыток: %s", url, attempts, last_error)
    return None
