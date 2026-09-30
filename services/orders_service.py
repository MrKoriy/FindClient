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
from models.order import Order

try:
    from api.rerank import rerank_orders  # type: ignore
except ImportError:  # pragma: no cover
    rerank_orders = None  # type: ignore

try:
    from services.classifier import SITE_ORDER_Q, qualify_texts  # type: ignore
except ImportError:  # pragma: no cover
    SITE_ORDER_Q = ""  # type: ignore
    qualify_texts = None  # type: ignore

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
        llm_rerank: bool = False,
        bai_api_key: str = "",
        bai_base_url: str = "",
        bai_model: str = "",
        llm=None,
    ) -> None:
        self.db = db
        self.sender = sender
        self.interval = max(60, interval)
        self.tg_groups_fetcher = tg_groups_fetcher
        self.llm_rerank = llm_rerank
        self.bai_api_key = bai_api_key
        self.bai_base_url = bai_base_url
        self.bai_model = bai_model
        self.llm = llm
        self.last_errors: dict[str, str] = {}
        self._task: asyncio.Task | None = None

    async def chat_config(self, chat_id: int) -> dict:
        stored = await self.db.get_many_settings(chat_id, [ENABLED, KEYWORDS, MINUS, SOURCES_KEY, TG_CHANNELS])
        return {
            "enabled": stored.get(ENABLED, False),
            "keywords": stored.get(KEYWORDS, list(DEFAULT_KEYWORDS)),
            "minus": stored.get(MINUS, []),
            "sources": stored.get(SOURCES_KEY, list(DEFAULT_SOURCES)),
            "tg_channels": stored.get(TG_CHANNELS, list(DEFAULT_TG_CHANNELS)),
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
        for name, res in zip(fetchers, results, strict=True):
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

        # unified LLM config: CRM settings always consulted regardless of env flag
        bai_key = self.bai_api_key
        bai_url = self.bai_base_url or "https://api.b.ai/v1"
        bai_model = self.bai_model or "qwen3.8-flash"
        crm_llm_enabled = False
        try:
            from crm.db import get_settings as _get_crm_settings  # type: ignore

            crm_s = await _get_crm_settings()
            crm_llm_enabled = crm_s.get("orders_llm_rerank") == "1"
            if not bai_key:
                bai_key = (crm_s.get("bai_api_key") or "").strip()
                bai_url = (crm_s.get("bai_base_url") or bai_url).strip()
                bai_model = (crm_s.get("bai_model") or bai_model).strip()
        except Exception:
            pass
        use_llm = bool(rerank_orders is not None and bai_key and (self.llm_rerank or crm_llm_enabled))
        llm_cache: dict[tuple[tuple[str, ...], tuple[str, ...]], dict[str, dict]] = {}

        async def _llm_meta_for(cfg: dict) -> dict[str, dict]:
            key = (tuple(cfg["keywords"]), tuple(cfg["minus"]))
            if key in llm_cache:
                return llm_cache[key]
            if not use_llm or not new_orders:
                llm_cache[key] = {}
                return {}
            try:
                scored = await rerank_orders(  # type: ignore
                    new_orders, tuple(cfg["keywords"]), tuple(cfg["minus"]),
                    bai_key=bai_key, bai_url=bai_url, bai_model=bai_model,
                )
                m = {o.uid: meta for o, meta in scored}
                llm_cache[key] = m
                return m
            except Exception as e:
                log.debug("llm_rerank failed: %s", e)
                llm_cache[key] = {}
                return {}

        sent: dict[int, list[Order]] = {}
        matched_uids: set[str] = set()
        delivered_uids: set[str] = set()
        truncated_uids: set[str] = set()
        for chat_id, cfg in configs.items():
            # if LLM enabled, rerank then filter by relevant+score threshold
            if use_llm:
                llm_map = await _llm_meta_for(cfg)
                if llm_map:
                    hits = [
                        o for o in new_orders
                        if o.uid not in rejected
                        and self._source_key(o) in cfg["sources"]
                        and llm_map.get(o.uid, {}).get("relevant", matches(o, cfg["keywords"], cfg["minus"]))
                    ]
                    # sort hits by llm score desc
                    hits.sort(key=lambda o: -int(llm_map.get(o.uid, {}).get("score", 0)))
                else:
                    hits = [
                        o for o in new_orders
                        if o.uid not in rejected
                        and self._source_key(o) in cfg["sources"] and matches(o, cfg["keywords"], cfg["minus"])
                    ]
            else:
                hits = [
                    o for o in new_orders
                    if o.uid not in rejected
                    and self._source_key(o) in cfg["sources"] and matches(o, cfg["keywords"], cfg["minus"])
                ]
            if first_run and len(hits) > _FIRST_RUN_LIMIT:
                truncated_uids.update(o.uid for o in hits[_FIRST_RUN_LIMIT:])
                hits = hits[:_FIRST_RUN_LIMIT]
            sent[chat_id] = hits
            matched_uids.update(o.uid for o in hits)
            if self.sender:
                for o in hits:
                    try:
                        await self.sender(chat_id, format_order(o))
                        delivered_uids.add(o.uid)
                        try:
                            await self.db.mark_delivered(o.uid, chat_id, True)
                        except Exception:
                            pass
                    except Exception as exc:
                        log.warning("send to %s failed: %s", chat_id, exc)
                        try:
                            await self.db.mark_delivered(o.uid, chat_id, False)
                        except Exception:
                            pass
                    await asyncio.sleep(0.05)

        # per-subscriber failed deliveries remain retryable; global seen only
        # for fully delivered or unmatched (not when matched but not delivered)
        await self.db.mark_orders_seen([
            {"uid": o.uid, "source": o.source, "title": o.title, "url": o.url,
             "budget": o.budget, "matched": o.uid in matched_uids}
            for o in new_orders
            if (o.uid in delivered_uids or o.uid not in matched_uids)
            and o.uid not in truncated_uids
            and not (o.uid in matched_uids and o.uid not in delivered_uids)
        ])
        return sent

    async def _jev_rejects(self, orders: list[Order], configs: dict) -> set[str]:
        if not (getattr(self, "llm", None) and getattr(self.llm, "jev_enabled", False)):
            return set()
        if qualify_texts is None:
            return set()
        candidates = [o for o in orders if any(matches(o, cfg["keywords"], cfg["minus"]) for cfg in configs.values())]
        if not candidates:
            return set()
        probs = await qualify_texts(self.llm, [f"{o.title}\n{o.description}" for o in candidates], SITE_ORDER_Q)  # type: ignore[arg-type]
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
