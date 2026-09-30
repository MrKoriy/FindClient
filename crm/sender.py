"""Воркер отправки: разбирает очередь с лимитами и паузами.

Отдельный процесс, а не часть панели. Причины:

* панель должна отвечать, даже когда идёт длинная пауза между сообщениями;
* упавший воркер не должен ронять панель, и наоборот;
* отправку видно и можно остановить одной командой, не трогая веб.

Лимиты - не украшение. Телеграм считает не только объём, но и **скорость**:
30 сообщений за 6 часов не ловят ограничение, те же 30 за полтора часа ловят
почти всегда. Поэтому паузы здесь в десятках секунд, а после каждых
`cooldown_every` сообщений - длинная пауза.

Отправка выключена, пока в настройках `enabled` не станет `1`. Это защита от
случайного запуска: по умолчанию воркер только смотрит на очередь.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
load_dotenv("crm.env")

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from crm import db as crm_db

log = logging.getLogger("crm.sender")

SESSION = os.environ.get(
    "CRM_TELETHON_SESSION",
    os.environ.get("TG_SESSION_FILE", str(Path.home() / ".hermes" / "telethon_vibecoders")),
)
API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")

# Сколько ждать, если Телеграм попросил подождать больше суток: смысла спать
# столько в процессе нет, лучше честно выйти и дать systemd перезапустить.
MAX_INLINE_WAIT = 3600
POLL_EMPTY = 30
# Пауза перед повтором после долгого FloodWait: лимит снимется не скоро,
# но и в пустую молотить каждые 30 секунд незачем.
FLOOD_RETRY_PAUSE = 300


def _load_env() -> None:
    """Подхватывает .env проекта, если переменные не заданы снаружи."""
    global API_ID, API_HASH
    if API_ID and API_HASH:
        return
    path = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key == "TG_API_ID" and not API_ID:
            API_ID = int(value or 0)
        elif key == "TG_API_HASH" and not API_HASH:
            API_HASH = value


def local_now(settings: dict) -> datetime:
    offset = int(settings.get("timezone_offset", "3"))
    return datetime.now(UTC) + timedelta(hours=offset)


def in_work_hours(settings: dict) -> bool:
    now = local_now(settings)
    start = int(settings.get("work_from", "0"))
    end = int(settings.get("work_to", "24"))
    return start <= now.hour < end


def make_client():
    from telethon import TelegramClient

    _load_env()
    if not API_ID or not API_HASH:
        raise RuntimeError("TG_API_ID/TG_API_HASH не найдены ни в окружении, ни в .env")
    return TelegramClient(SESSION, API_ID, API_HASH)


async def _adaptive_throttle(crm_path: str | None, wait: int = 0, peer_flood: bool = False) -> None:
    """Снижает daily_cap и увеличивает cooldown при флуд-ошибках — никогда не повышает."""
    settings = await crm_db.get_settings(crm_path)
    cap = int(settings.get("daily_cap", "10"))
    cooldown = int(settings.get("cooldown_seconds", "1200"))
    consecutive = int(settings.get("consecutive_floods", "0")) + 1
    factor = 0.5 if consecutive >= 3 else 0.6
    new_cap = min(cap, int(cap * factor))
    new_cap = max(1, new_cap)
    new_cooldown = min(3600, int(cooldown * 1.5))
    await crm_db.set_settings(
        {"daily_cap": str(new_cap), "cooldown_seconds": str(new_cooldown),
         "consecutive_floods": str(consecutive)},
        crm_path,
    )
    reason = "PeerFlood" if peer_flood else f"FloodWait {wait}с"
    try:
        msg = f"{reason}: cap {cap}->{new_cap}, cooldown {cooldown}->{new_cooldown}, n={consecutive}"  # noqa: E501
        await crm_db.log_event("flood", msg, crm_path)
    except Exception:
        pass
    log.warning(
        "adaptive throttle: %s cap %s->%s cooldown %s->%s floods=%s",  # noqa: E501
        reason, cap, new_cap, cooldown, new_cooldown, consecutive,
    )


async def _reset_floods_on_success(crm_path: str | None) -> None:
    """Сброс consecutive_floods и запись warmup_started_at при первой успешной отправке."""
    settings = await crm_db.get_settings(crm_path)
    updates: dict[str, str] = {}
    if int(settings.get("consecutive_floods", "0")) != 0:
        updates["consecutive_floods"] = "0"
    if not (settings.get("warmup_started_at") or "").strip():
        from datetime import UTC
        from datetime import datetime as _dt
        updates["warmup_started_at"] = _dt.now(UTC).isoformat()
    if updates:
        await crm_db.set_settings(updates, crm_path)


async def send_one(client, message: dict, settings: dict, crm_path: str | None = None) -> tuple[str, str, int | None]:
    """Отправляет одно сообщение. Возвращает (статус, ошибка, tg_id).

    Проверка холостого хода стоит до импорта Telethon намеренно: в холостом
    режиме ничего не отправляется, и требовать установленный Telethon для
    прогона очереди «на сухую» незачем.

    При FloodWait > 300с и PeerFlood автоматически снижает cap (adaptive throttle).
    """
    chat = (message.get("chat") or "").strip()
    if not chat:
        return "failed", "не указан получатель", None

    if settings.get("dry_run") == "1":
        log.info("[холостой ход] ушло бы в @%s: %s", chat, (message["body"] or "")[:60])
        return "skipped", "холостой ход", None

    from telethon.errors import FloodWaitError, PeerFloodError, UserPrivacyRestrictedError

    # Две попытки: короткий FloodWait честно отрабатывается сном и повтором,
    # долгий - возвращается наверх как "queued", сообщение остаётся в очереди.
    for attempt in range(2):
        try:
            entity = await client.get_entity("@" + chat.lstrip("@"))
            sent = await client.send_message(entity, message["body"])
            return "sent", "", getattr(sent, "id", None)
        except FloodWaitError as exc:
            wait = int(exc.seconds)
            log.warning("FloodWait %s с (~%.1f ч)", wait, wait / 3600)
            if wait > 300:
                await _adaptive_throttle(crm_path, wait=wait)
            if wait > MAX_INLINE_WAIT or attempt == 1:
                return "queued", f"FloodWait {wait} с", None
            await asyncio.sleep(wait + 5)
        except PeerFloodError:
            await _adaptive_throttle(crm_path, peer_flood=True)
            return "failed", "PEER_FLOOD: аккаунт ограничен для сообщений незнакомым", None
        except UserPrivacyRestrictedError:
            return "failed", "приватность получателя запрещает сообщения", None
        except Exception as exc:  # noqa: BLE001
            return "failed", f"{type(exc).__name__}: {str(exc)[:200]}", None
    return "failed", "FloodWait не отработал за две попытки", None


async def run_once(client, crm_path: str | None = None) -> bool:
    """Обрабатывает одно сообщение. False - очередь пуста или нельзя отправлять.

    `crm_path` протянут параметром, а не берётся из окружения: иначе воркер
    нельзя ни протестировать на временной базе, ни запустить на другой.
    """
    settings = await crm_db.get_settings(crm_path)
    if settings.get("enabled") != "1":
        return False

    if not in_work_hours(settings):
        log.info("вне рабочих часов (%s)", local_now(settings).strftime("%H:%M"))
        return False

    try:
        cap = crm_db.effective_daily_cap(settings)
    except AttributeError:
        cap = int(settings.get("daily_cap", "10"))
    today = await crm_db.sent_today(
        crm_path, tz_offset=int(settings.get("timezone_offset", "3"))
    )
    if today >= cap:
        log.info("дневной лимит выбран: %s/%s", today, cap)
        return False

    # Drip: только если включено; пропускаем blocked/replied цели
    if settings.get("drip_enabled") == "1":
        try:
            due = await crm_db.list_due_sequences(limit=10, crm_db=crm_path)
        except Exception:
            due = []
        for seq in due:
            target = await crm_db.get_target(seq["target_id"], crm_db=crm_path)
            if not target or target.get("status") in ("blocked", "replied", "refused", "skip"):
                await crm_db.cancel_sequences(target["id"] if target else 0, crm_db=crm_path)
                continue
            chat = (target.get("username") or "").strip()
            if not chat:
                continue
            mid = await crm_db.queue_message(seq["target_id"], chat, seq["body"], crm_db=crm_path)
            await crm_db.mark_sequence_queued(seq["id"], mid, crm_db=crm_path)
            log.info("drip step %s -> queued as message %s for @%s", seq["id"], mid, chat)

    await crm_db.release_expired_leases(crm_path)
    message = await crm_db.claim_next(str(id(client)), crm_db=crm_path)
    if not message:
        return False

    # re-check target status after claim (blocked between claim and send)
    if message.get("target_id"):
        tgt = await crm_db.get_target(int(message["target_id"]), crm_db=crm_path)
        if tgt and tgt.get("status") in ("blocked", "replied", "refused", "skip"):
            await crm_db.mark_message(message["id"], "skipped", "target blocked/replied", None, crm_db=crm_path)
            return True

    log.info("отправляю @%s (%s/%s за сегодня)", message["chat"], today + 1, cap)
    status, error, tg_id = await send_one(client, message, settings, crm_path)
    if status == "queued":
        # Долгий FloodWait: release lease so it retries after backoff
        await crm_db.mark_message(message["id"], "queued", error, None, crm_db=crm_path)
        await asyncio.sleep(min(FLOOD_RETRY_PAUSE, 300))
        return True
    if status == "failed" and "PEER_FLOOD" in error:
        # persist block: treat as hard stop, long cooldown, no retry this tick
        await crm_db.set_settings({"blocked_until": str(int(__import__("time").time()) + 24*3600)}, crm_path)
        await crm_db.mark_message(message["id"], "failed", error, None, crm_db=crm_path)
        await asyncio.sleep(FLOOD_RETRY_PAUSE)
        return True
    await crm_db.mark_message(message["id"], status, error, tg_id, crm_db=crm_path)
    log.info("  -> %s %s", status, error)

    if status == "sent":
        await _reset_floods_on_success(crm_path)
        every = int(settings.get("cooldown_every", "8"))
        sent_so_far = today + 1
        if every and sent_so_far % every == 0:
            pause = int(settings.get("cooldown_seconds", "1200"))
            log.info("длинная пауза %s с после %s сообщений", pause, sent_so_far)
        else:
            low = int(settings.get("min_delay", "90"))
            high = max(low, int(settings.get("max_delay", "240")))
            pause = random.randint(low, high)
        await asyncio.sleep(pause)
    return True


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    await crm_db.init_db()
    client = make_client()
    await client.connect()
    if not await client.is_user_authorized():
        log.error("сессия %s не авторизована", SESSION)
        return
    me = await client.get_me()
    log.info("воркер запущен, аккаунт @%s (id=%s)", getattr(me, "username", "?"), me.id)

    try:
        while True:
            worked = await run_once(client)
            if not worked:
                await asyncio.sleep(POLL_EMPTY)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
