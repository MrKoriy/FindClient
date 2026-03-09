"""Telegram bot handlers registration."""

from aiogram import Dispatcher

from handlers.start import router as start_router


def register_routers(dp: Dispatcher) -> None:
    """Register all handler routers with the dispatcher."""
    dp.include_router(start_router)
