"""Bot entry point -- creates Bot, Dispatcher, registers routers, starts polling."""

import asyncio

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher

from config import Settings
from db.database import Database
from handlers import register_routers
from services.scrape_service import ScrapeService

settings = Settings.from_env()

bot = Bot(token=settings.BOT_TOKEN)
dp = Dispatcher()

register_routers(dp)


async def main() -> None:
    """Start the bot with long polling."""
    db = Database()
    await db.connect()

    scrape_service = ScrapeService(db=db, request_delay=settings.REQUEST_DELAY)

    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            db=db,
            scrape_service=scrape_service,
        )
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
