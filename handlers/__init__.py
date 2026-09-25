"""Telegram bot handlers registration."""

from aiogram import Dispatcher

from handlers.history import router as history_router
from handlers.niches import router as niches_router
from handlers.orders import router as orders_router
from handlers.scrape import router as scrape_router
from handlers.start import router as start_router
from handlers.stats import router as stats_router
from handlers.tg_leads import router as tg_router


def register_routers(dp: Dispatcher) -> None:
    """Register all handler routers with the dispatcher (start first: /cancel works in any state)."""
    for router in (start_router, scrape_router, niches_router, orders_router,
                   tg_router, history_router, stats_router):
        dp.include_router(router)
