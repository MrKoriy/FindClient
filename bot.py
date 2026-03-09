"""Bot entry point -- creates Bot, Dispatcher, registers routers, starts polling."""

import asyncio

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher

from config import Settings
from handlers import register_routers

settings = Settings.from_env()

bot = Bot(token=settings.BOT_TOKEN)
dp = Dispatcher()

register_routers(dp)


async def main() -> None:
    """Start the bot with long polling."""
    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    asyncio.run(main())
