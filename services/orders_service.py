"""Background polling of freelance sources and push of matching orders to subscribed chats."""

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable

import aiohttp

from api.orders import (
    DEFAULT_KEYWORDS,
    DEFAULT_TG_CHANNELS,
    SOURCES,
    Fetcher,
    make_tg_channels_fetcher,
    matches,
)
from db.database import Database
from services.classifier import SITE_ORDER_Q, qualify_texts
from models.order import Order

log = logging.getLogger(__name__)

# Per-chat settings keys.
ENABLED = "orders_enabled"
KEYWORDS = "orders_keywords"
MINUS = "orders_minus"
SOURCES_KEY = "orders_sources"
TG_CHANNELS = "orders_tg_channels"

ALL_SOURCES = {**{k: v[0] for k, v in SOURCES.items()}, "tg": "Telegram-каналы", "tg_groups": "Telegram-чаты (аккаунт)"}
DEFAULT_SOURCES = ["kwork", "fl", "freelance_ru", "freelancejob", "tg", "tg_groups"]
_FIRST_RUN_LIMIT = 10  # on an empty history send only the newest few instead of a flood

Sender = Callable[[int, str], Awaitable[None]]


def format_order(o: Order) -> str:
    source = ALL_SOURCES.get(o.source, o.source)
    lines = [f"<b>{html.escape(o.title[:200] or 'Заказ')}</b>"]
    meta = " · ".join(p for p in (html.escape(source), html.escape(o.budget), html.escape(o.published)) if p)
    lines.append(meta)
    if o.description:
        desc = o.description if len(o.description) < 600 else o.description[:600] + "…"
        lines.append("\n" + html.escape(desc))
    lines.append(f'\n<a href="{html.escape(o.url, quote=True)}">Открыть заказ</a>')
    return "\n".join(lines)


class OrdersService:
    def __init__(
        self,
        db: Database,
        sender: Sender | None = None,
        interval: int = 300,
        tg_groups_fetcher: Fetcher | None = None,
        llm=None,
    ) -> None:
        self.db = db
        self.llm = llm
        self.sender = sender
        self.interval = max(60, interval)
        self.tg_groups_fetcher = tg_groups_fetcher
        self.last_errors: dict[str, str] = {}
        self._task: asyncio.Task | None = None

    async def chat_config(self, chat_id: int) -> dict:
        return {
            "enabled": await self.db.get_setting(chat_id, ENABLED, False),
            "keywords": await self.db.get_setting(chat_id, KEYWORDS, list(DEFAULT_KEYWORDS)),
            "minus": await self.db.get_setting(chat_id, MINUS, []),
            "sources": await self.db.get_setting(chat_id, SOURCES_KEY, list(DEFAULT_SOURCES)),
            "tg_channels": await self.db.get_setting(chat_id, TG_CHANNELS, list(DEFAULT_TG_CHANNELS)),
        }

    async def fetch_all(self, sources: set[str], tg_channels: set[str]) -> list[Order]:
        fetchers: dict[str, Fetcher] = {k: v[1] for k, v in SOURCES.items() if k in sources}
        if "tg" in sources and tg_channels:
            fetchers["tg"] = make_tg_channels_fetcher(sorted(tg_channels))
        if "tg_groups" in sources and self.tg_groups_fetcher:
            fetchers["tg_groups"] = self.tg_groups_fetcher

        orders: list[Order] = []
        timeout = aiohttp.ClientTimeout(total=40)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            results = await asyncio.gather(
                *(f(session) for f in fetchers.values()), return_exceptions=True
            )
        for name, res in zip(fetchers, results):
            if isinstance(res, BaseException):
                self.last_errors[name] = repr(res)[:200]
                log.warning("orders source %s failed: %r", name, res)
            else:
                self.last_errors.pop(name, None)
                orders.extend(res)
        return orders

    async def poll_once(self, only_chat: int | None = None) -> dict[int, list[Order]]:
        """Fetch, find unseen orders, match per chat, send. Returns what was sent per chat."""
        chats = [only_chat] if only_chat else await self.db.chats_with_setting(ENABLED, True)
        if not chats:
            return {}
        configs = {c: await self.chat_config(c) for c in chats}
        sources = {s for cfg in configs.values() for s in cfg["sources"]}
        channels = {ch for cfg in configs.values() if "tg" in cfg["sources"] for ch in cfg["tg_channels"]}

        orders = await self.fetch_all(sources, channels)
        by_uid = {o.uid: o for o in orders}
        first_run = (await self.db.get_stats())["orders_seen"] == 0
        new_uids = await self.db.filter_new_order_uids(list(by_uid))
        new_orders = [by_uid[u] for u in by_uid if u in new_uids]

        rejected = await self._jev_rejects(new_orders, configs)
        sent: dict[int, list[Order]] = {}
        matched_uids: set[str] = set()
        for chat_id, cfg in configs.items():
            hits = [
                o for o in new_orders
                if o.uid not in rejected
                and self._source_key(o) in cfg["sources"] and matches(o, cfg["keywords"], cfg["minus"])
            ]
            if first_run:
                hits = hits[:_FIRST_RUN_LIMIT]
            sent[chat_id] = hits
            matched_uids.update(o.uid for o in hits)
            if self.sender:
                for o in hits:
                    try:
                        await self.sender(chat_id, format_order(o))
                    except Exception as exc:
                        log.warning("send to %s failed: %s", chat_id, exc)
                    await asyncio.sleep(0.05)

        await self.db.mark_orders_seen([
            {"uid": o.uid, "source": o.source, "title": o.title, "url": o.url,
             "budget": o.budget, "matched": o.uid in matched_uids}
            for o in new_orders
        ])
        return sent

    async def _jev_rejects(self, orders: list[Order], configs: dict) -> set[str]:
        """Second-stage filter: Jev drops keyword hits that are vacancies/ads (no-op without Jev)."""
        if not (self.llm and getattr(self.llm, "jev_enabled", False)):
            return set()
        candidates = [o for o in orders
                      if any(matches(o, cfg["keywords"], cfg["minus"]) for cfg in configs.values())]
        probs = await qualify_texts(self.llm, [f"{o.title}\n{o.description}" for o in candidates], SITE_ORDER_Q)
        return {o.uid for o, p in zip(candidates, probs) if p is not None and p < 0.4}

    @staticmethod
    def _source_key(o: Order) -> str:
        if o.source.startswith("tgg:"):
            return "tg_groups"
        if o.source.startswith("tg:"):
            return "tg"
        return {"kwork": "kwork", "fl.ru": "fl", "freelance.ru": "freelance_ru",
                "freelancejob": "freelancejob"}.get(o.source, o.source)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("orders poll failed")
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
