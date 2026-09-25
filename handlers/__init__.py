"""Telegram bot handlers registration."""

from aiogram import Dispatcher

from handlers.history import router as history_router
from handlers.scrape import router as scrape_router
from handlers.start import router as start_router
from handlers.stats import router as stats_router


def register_routers(dp: Dispatcher) -> None:
    """Register all handler routers with the dispatcher."""
    dp.include_router(start_router)
    dp.include_router(scrape_router)
    dp.include_router(history_router)
    dp.include_router(stats_router)
