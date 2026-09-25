"""Bot entry point: wires services, starts order polling and long polling."""

import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from config import Settings
from db.database import Database
from handlers import register_routers
from handlers.common import AccessMiddleware
from handlers.tg_leads import WATCH_KEY
from services.orders_service import OrdersService
from services.scrape_service import ScrapeService
from services.telegram_service import TelegramUserService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="scrape", description="Компании с карт"),
    BotCommand(command="find", description="Быстрый поиск: ниша | город | кол-во"),
    BotCommand(command="niches", description="Денежные ниши"),
    BotCommand(command="tg", description="Лиды из Telegram"),
    BotCommand(command="orders", description="Автопоиск заказов"),
    BotCommand(command="history", description="История сборов"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="cancel", description="Отменить шаг"),
]


async def main() -> None:
    settings = Settings.from_env()
    if not settings.OWNER_IDS:
        log.warning("OWNER_IDS is empty — anyone who finds the bot can use it")

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()
    access = AccessMiddleware(settings.OWNER_IDS)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    register_routers(dp)

    db = Database(settings.DB_PATH)
    await db.connect()

    tg_service = TelegramUserService(settings.TG_API_ID, settings.TG_API_HASH, settings.TG_SESSION)
    if await tg_service.start():
        log.info("Telegram user account connected")
    else:
        log.info("Telegram user account disabled: %s", tg_service.error)

    async def watched_chats() -> list[str]:
        chats: list[str] = []
        for value in await db.all_values(WATCH_KEY):
            chats += [c for c in value if c not in chats]
        return chats

    async def send(chat_id: int, text: str) -> None:
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)

    orders_service = OrdersService(
        db, sender=send, interval=settings.ORDERS_POLL_INTERVAL,
        tg_groups_fetcher=tg_service.make_site_requests_fetcher(watched_chats) if tg_service.enabled else None,
    )
    scrape_service = ScrapeService(
        db=db, request_delay=settings.REQUEST_DELAY,
        yandex_api_key=settings.YANDEX_API_KEY, proxy=settings.HTTP_PROXY,
    )

    await bot.set_my_commands(COMMANDS)
    orders_service.start()
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            db=db,
            settings=settings,
            scrape_service=scrape_service,
            orders_service=orders_service,
            tg_service=tg_service,
        )
    finally:
        await orders_service.stop()
        await tg_service.stop()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
