"""Telegram lead sourcing.

* With a user account (Telethon, TG_API_ID/TG_API_HASH/TG_SESSION):
  search public chats by keywords, read public groups without joining, collect
  message authors as leads, and watch groups for "нужен сайт"-type requests.
* Without an account: read public channels via the t.me/s web preview and pull
  phones / @usernames / links out of posts (ads of builders, realtors...).

A Bot API bot cannot read groups it is not a member of, hence the user account.
"""

import asyncio
import html
import logging
import random
import re
from collections.abc import Awaitable, Callable
from datetime import datetime

import aiohttp

from api.common import USER_AGENTS, phone_key
from api.orders import fetch_tg_channel_posts
from data.niches import NICHES
from models.order import Order
from models.tg_lead import TgLead

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]

PHONE_RE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{4,31})")
TME_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?![/\w])")

# Requests for a website posted in chats (builders asking who can make a site).
SITE_REQUEST_RE = re.compile(
    r"(нуж[ен|на|но]\w*|ищу|ищем|кто\s+(?:может|делает|сделает)|посоветуйте|требуется)"
    r"[^.?!\n]{0,40}(сайт|лендинг|интернет[- ]магазин|разработчик\w*\s+сайт)",
    re.I,
)
# Words showing an author runs a business/brigade — used to rank leads.
BUSINESS_HINTS = (
    "бригада", "выполним", "выполняем", "под ключ", "строим", "монтаж", "ремонт", "смета",
    "договор", "гарантия", "опыт", "лет", "объект", "ооо", "ип ", "компания", "производ",
    "поставка", "аренда", "продажа", "звоните", "пишите", "прайс", "цена",
)


# Known chats are not people: skip them when they are mentioned inside posts.
_KNOWN_CHATS = {c.lower() for n in NICHES for c in n.tg_chats}


def _clean_phone(p: str) -> str:
    digits = re.sub(r"\D", "", p)
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    return "+" + digits if len(digits) == 11 else p.strip()


def _business_score(text: str, messages: int, has_contact: bool) -> int:
    t = text.lower()
    score = min(40, sum(8 for h in BUSINESS_HINTS if h in t))
    score += min(30, messages * 5)
    score += 30 if has_contact else 0
    return min(100, score)


def _channel_username(ref: str) -> str:
    ref = ref.strip()
    m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]+)", ref)
    return (m.group(1) if m else ref).lstrip("@")


async def chat_info(session: aiohttp.ClientSession, username: str) -> dict:
    """Title, type (group/channel) and member count from the public t.me page."""
    username = _channel_username(username)
    async with session.get(f"https://t.me/{username}", headers={"User-Agent": random.choice(USER_AGENTS)}) as r:
        page = await r.text()
    title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    extra = re.search(r'<div class="tgme_page_extra">([^<]*)</div>', page)
    extra_text = html.unescape(extra.group(1)) if extra else ""
    num = re.sub(r"[^\d]", "", extra_text.split(",")[0]) if extra_text else ""
    return {
        "username": username,
        "title": html.unescape(title.group(1)) if title else username,
        "type": "channel" if "subscriber" in extra_text or "подписч" in extra_text else
                "group" if "member" in extra_text or "участник" in extra_text else "user",
        "members": int(num) if num else 0,
    }


async def harvest_channels(
    channels: list[str], pages: int = 3, on_progress: Progress | None = None,
) -> list[TgLead]:
    """Keyless: extract contacts from recent posts of public channels."""
    leads: dict[str, TgLead] = {}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for idx, ch in enumerate(channels, 1):
            ch = _channel_username(ch)
            if on_progress:
                await on_progress(f"Канал @{ch} ({idx}/{len(channels)})")
            posts: list[dict] = []
            before = ""
            for _ in range(pages):
                url_ch = f"{ch}?before={before}" if before else ch
                try:
                    batch = await fetch_tg_channel_posts(session, url_ch)
                except Exception as exc:
                    log.warning("channel %s failed: %s", ch, exc)
                    break
                if not batch:
                    break
                posts += batch
                before = min(int(p["id"]) for p in batch if p["id"].isdigit())
                await asyncio.sleep(random.uniform(0.8, 1.5))
            for p in posts:
                text = p["text"] + "\n" + "\n".join(p.get("links", []))
                contacts = [_clean_phone(ph) for ph in PHONE_RE.findall(text)]
                handles = {h for h in MENTION_RE.findall(text) + TME_RE.findall(text)
                           if h.lower() != ch.lower() and h.lower() not in _KNOWN_CHATS
                           and not h.lower().endswith("bot")}
                if not contacts and not handles:
                    continue
                key = phone_key(contacts[0]) if contacts else sorted(handles)[0].lower()
                lead = leads.get(key)
                if not lead:
                    lead = TgLead(user_id=0, username=sorted(handles)[0] if handles else "",
                                  phone=", ".join(dict.fromkeys(contacts)),
                                  sample=p["text"][:400], last_seen=p["date"][:16])
                    leads[key] = lead
                lead.chats.add(f"@{ch}")
                lead.messages += 1
                lead.name = lead.name or p["text"].split("\n", 1)[0][:60]
                lead.score = _business_score(lead.sample, lead.messages, True)
    return sorted(leads.values(), key=lambda l: l.score, reverse=True)


class TelegramUserService:
    """Wrapper over a Telethon user session. All methods are safe to call when disabled."""

    def __init__(self, api_id: int, api_hash: str, session: str, proxy: str = "") -> None:
        self.api_id, self.api_hash, self.session_str = api_id, api_hash, session
        self.client = None
        self.error = ""
        self._last_ids: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def start(self) -> bool:
        if not (self.api_id and self.api_hash and self.session_str):
            self.error = "не заданы TG_API_ID / TG_API_HASH / TG_SESSION"
            return False
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError:
            self.error = "не установлен telethon (pip install telethon)"
            return False
        client = TelegramClient(
            StringSession(self.session_str), self.api_id, self.api_hash, flood_sleep_threshold=60,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self.error = "сессия не авторизована — запустите scripts/tg_login.py"
                await client.disconnect()
                return False
        except Exception as exc:
            self.error = f"ошибка подключения: {exc}"
            return False
        self.client = client
        return True

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()

    async def search_chats(
        self, keywords: list[str], modifiers: tuple[str, ...] = ("", "чат", "москва", "спб"),
        on_progress: Progress | None = None,
    ) -> list[dict]:
        """Telegram global search for public groups/channels by keywords."""
        from telethon.tl.functions.contacts import SearchRequest
        from telethon.tl.types import Channel

        found: dict[int, dict] = {}
        queries = [f"{k} {m}".strip() for k in keywords for m in modifiers]
        async with self._lock:
            for i, q in enumerate(queries, 1):
                if on_progress:
                    await on_progress(f"Поиск «{q}» ({i}/{len(queries)})")
                try:
                    res = await self.client(SearchRequest(q=q, limit=50))
                except Exception as exc:
                    log.warning("search %s failed: %s", q, exc)
                    continue
                for chat in res.chats:
                    if not isinstance(chat, Channel) or not chat.username or chat.id in found:
                        continue
                    found[chat.id] = {
                        "username": chat.username,
                        "title": chat.title,
                        "type": "group" if chat.megagroup else "channel",
                        "members": getattr(chat, "participants_count", 0) or 0,
                    }
                await asyncio.sleep(random.uniform(1.5, 3.0))
        return sorted(found.values(), key=lambda c: c["members"], reverse=True)

    async def harvest_authors(
        self, chats: list[str], per_chat: int = 400, with_bio: int = 40,
        skip_user_ids: set[int] | None = None, on_progress: Progress | None = None,
    ) -> list[TgLead]:
        """Collect authors of recent messages in public groups (no joining needed)."""
        from telethon.tl.functions.users import GetFullUserRequest
        from telethon.tl.types import User

        skip_user_ids = skip_user_ids or set()
        leads: dict[int, TgLead] = {}
        texts: dict[int, list[str]] = {}
        async with self._lock:
            for idx, ref in enumerate(chats, 1):
                name = _channel_username(ref)
                if on_progress:
                    await on_progress(f"Читаю @{name} ({idx}/{len(chats)})")
                try:
                    entity = await self.client.get_entity(name)
                    async for msg in self.client.iter_messages(entity, limit=per_chat):
                        sender = msg.sender
                        if not isinstance(sender, User) or sender.bot or sender.id in skip_user_ids:
                            continue
                        lead = leads.get(sender.id)
                        if not lead:
                            full_name = " ".join(p for p in (sender.first_name, sender.last_name) if p)
                            lead = TgLead(user_id=sender.id, username=sender.username or "",
                                          name=full_name, phone=sender.phone or "")
                            leads[sender.id] = lead
                        lead.chats.add(f"@{name}")
                        lead.messages += 1
                        text = msg.message or ""
                        if text:
                            texts.setdefault(sender.id, []).append(text)
                            if len(text) > len(lead.sample):
                                lead.sample = text[:400]
                            phones = PHONE_RE.findall(text)
                            if phones and not lead.phone:
                                lead.phone = _clean_phone(phones[0])
                        if msg.date and (not lead.last_seen or msg.date.isoformat() > lead.last_seen):
                            lead.last_seen = msg.date.strftime("%Y-%m-%d %H:%M")
                except Exception as exc:
                    log.warning("harvest %s failed: %s", name, exc)
                await asyncio.sleep(random.uniform(1.0, 2.5))

            for lead in leads.values():
                joined = " ".join(texts.get(lead.user_id, []))
                lead.score = _business_score(joined, lead.messages, bool(lead.phone or lead.username))

            ranked = sorted(leads.values(), key=lambda l: l.score, reverse=True)
            for i, lead in enumerate(ranked[:with_bio], 1):
                if on_progress and i % 10 == 0:
                    await on_progress(f"Био профилей {i}/{min(with_bio, len(ranked))}")
                try:
                    full = await self.client(GetFullUserRequest(lead.user_id))
                    lead.bio = full.full_user.about or ""
                    if lead.bio:
                        lead.score = min(100, lead.score + 10)
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.8, 1.6))
        return sorted(leads.values(), key=lambda l: l.score, reverse=True)

    def make_site_requests_fetcher(self, chats_getter: Callable[[], Awaitable[list[str]]]):
        """Orders fetcher: new 'нужен сайт' messages in watched public groups."""

        async def fetch(_session: aiohttp.ClientSession) -> list[Order]:
            if not self.client:
                return []
            orders: list[Order] = []
            async with self._lock:
                for ref in await chats_getter():
                    name = _channel_username(ref)
                    try:
                        last = self._last_ids.get(name, 0)
                        async for msg in self.client.iter_messages(name, limit=100, min_id=last):
                            self._last_ids[name] = max(self._last_ids.get(name, 0), msg.id)
                            text = msg.message or ""
                            if not SITE_REQUEST_RE.search(text):
                                continue
                            orders.append(Order(
                                source=f"tgg:{name}", id=str(msg.id),
                                title=text.split("\n", 1)[0][:120], url=f"https://t.me/{name}/{msg.id}",
                                description=text[:1500],
                                published=msg.date.strftime("%Y-%m-%d %H:%M") if isinstance(msg.date, datetime) else "",
                            ))
                    except Exception as exc:
                        log.warning("watch %s failed: %s", name, exc)
                    await asyncio.sleep(random.uniform(0.5, 1.5))
            return orders

        return fetch
