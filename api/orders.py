"""Freelance order sources. Each fetcher returns the latest orders as list[Order].

Verified 2026-09-25: Kwork (JSON), FL.ru (RSS), freelance.ru (HTML),
freelancejob.ru (RSS), public Telegram channels (t.me/s web preview).
Not usable from a server: Habr Freelance (closed), Weblancer/YouDo (anti-bot),
Avito (IP blocks; its API has no search of other people's listings).
"""

import html as html_lib
import json
import logging
import random
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable

import aiohttp

from api.common import USER_AGENTS
from models.order import Order

log = logging.getLogger(__name__)

Fetcher = Callable[[aiohttp.ClientSession], Awaitable[list[Order]]]

# Kwork: 37 Создание сайта, 38 Доработка сайта, 79 Вёрстка, 24 Веб-дизайн.
KWORK_CATEGORIES = (37, 38, 79)
# FL.ru RSS category 2 = «Сайты».
FL_CATEGORIES = (2,)

# Public channels that post freelance web orders (t.me/s preview must be enabled).
DEFAULT_TG_CHANNELS = ("rabota_freelancee", "webfrl", "freelancetaverna", "zakaz_design", "workasap")

DEFAULT_KEYWORDS = (
    "сайт", "лендинг", "landing", "одностранич", "tilda", "тильд", "wordpress", "вордпресс",
    "интернет-магазин", "интернет магазин", "верстк", "сверстать", "битрикс", "bitrix", "webflow",
    "joomla", "opencart", "insales", "веб-разработ", "веб разработ", "квиз", "многостранич",
)
# In descriptions a bare "сайт" is too common, so require an explicit request.
_DESC_RE = re.compile(
    r"(созда|разработ|сдела|собра|сверста|переделa|переделать|редизайн|нуж[ен]|требуется|ищу|заказать)"
    r"\w*\s+(?:\S+\s+){0,3}?(сайт|лендинг|landing|интернет[- ]магазин)"
    r"|\b(tilda|тильд\w*|wordpress|битрикс|bitrix|webflow|лендинг\w*)\b",
    re.I,
)
# Posts from people offering their own services, and job vacancies.
_NEGATIVE = (
    "ищу работу", "ищу заказ", "предлагаю услуги", "возьму заказ", "#резюме", "резюме",
    "вакансия", "вакансию", "в штат", "оклад", "з/п", "зарплата", "полная занятость",
    "ищу проект", "ищу себе", "ищу позицию", "мнение о сайте", "отзыв о сайте", "копирайтер",
    "без опыта", "#помогу", "#ищу_работу", "делаю сайты", "сделаю сайт", "создаю сайты",
    "разрабатываю сайты", "портфолио в профиле",
)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "ru-RU,ru;q=0.9",
    }


def _strip_tags(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    return html_lib.unescape(re.sub(r"<[^>]+>", "", text)).replace("\r", "").strip()


def matches(order: Order, keywords=DEFAULT_KEYWORDS, minus=()) -> bool:
    """Title contains a keyword, or description explicitly asks for a site."""
    title = order.title.lower()
    text = f"{title}\n{order.description.lower()}"
    if any(n in text for n in _NEGATIVE) or any(m.lower() in text for m in minus if m):
        return False
    if any(k.lower() in title for k in keywords):
        return True
    custom = [k.lower() for k in keywords if k.lower() not in DEFAULT_KEYWORDS]
    return bool(_DESC_RE.search(order.description)) or any(k in text for k in custom)


# ----------------------------------------------------------------------
# Kwork
# ----------------------------------------------------------------------

def parse_kwork_wants(wants: list[dict]) -> list[Order]:
    orders = []
    for w in wants:
        wid = str(w.get("id", ""))
        if not wid:
            continue
        price = w.get("priceLimit") or ""
        possible = w.get("possiblePriceLimit") or ""
        budget = ""
        if price:
            budget = f"до {float(price):,.0f} ₽".replace(",", " ")
            if possible and float(possible) > float(price):
                budget += f" (допустимо {float(possible):,.0f} ₽)".replace(",", " ")
        orders.append(Order(
            source="kwork",
            id=wid,
            title=w.get("name", "").strip(),
            url=f"https://kwork.ru/projects/{wid}/view",
            description=_strip_tags(w.get("description", ""))[:1500],
            budget=budget,
            published=w.get("date_create", ""),
        ))
    return orders


async def fetch_kwork(session: aiohttp.ClientSession) -> list[Order]:
    orders: list[Order] = []
    for cat in KWORK_CATEGORIES:
        headers = {**_headers(), "X-Requested-With": "XMLHttpRequest", "Referer": "https://kwork.ru/projects"}
        async with session.post("https://kwork.ru/projects", data={"c": cat, "page": 1}, headers=headers) as r:
            text = await r.text()
        try:
            data = json.loads(text).get("data") or {}
            wants = data.get("wants") or (data.get("pagination") or {}).get("data") or []
        except ValueError:
            wants = _kwork_state_wants(text)
        orders.extend(parse_kwork_wants(wants))
    return orders


def _kwork_state_wants(html: str) -> list[dict]:
    i = html.find("window.stateData=")
    if i < 0:
        return []
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[i + len("window.stateData="):])
    except ValueError:
        return []
    return (obj.get("wantsListData") or {}).get("wants") or []


# ----------------------------------------------------------------------
# RSS feeds (FL.ru, freelancejob.ru)
# ----------------------------------------------------------------------

_BUDGET_RE = re.compile(r"\s*\(Бюджет:\s*([^)]*)\)\s*$")


def parse_fl_rss(xml_text: str) -> list[Order]:
    orders = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        m = re.search(r"/projects/(\d+)/", link)
        if not m:
            continue
        title = html_lib.unescape(item.findtext("title") or "").strip()
        budget = ""
        bm = _BUDGET_RE.search(title)
        if bm:
            budget = html_lib.unescape(bm.group(1)).replace("\xa0", " ").strip()
            title = title[: bm.start()].strip()
        orders.append(Order(
            source="fl.ru",
            id=m.group(1),
            title=title,
            url=link,
            description=_strip_tags(item.findtext("description") or "")[:1500],
            budget=re.sub(r"\s+", " ", budget),
            published=(item.findtext("pubDate") or "").strip(),
        ))
    return orders


async def fetch_fl(session: aiohttp.ClientSession) -> list[Order]:
    orders: list[Order] = []
    for cat in FL_CATEGORIES:
        async with session.get(f"https://www.fl.ru/rss/all.xml?category={cat}", headers=_headers()) as r:
            orders.extend(parse_fl_rss(await r.text()))
    return orders


def parse_freelancejob_rss(text: str) -> list[Order]:
    # Declared windows-1251 but actually UTF-8 and not well-formed — parse with regex.
    orders = []
    for block in re.findall(r"<item>(.*?)</item>", text, re.S):
        def tag(name: str) -> str:
            m = re.search(rf"<{name}>(.*?)</{name}>", block, re.S)
            return _strip_tags(m.group(1)) if m else ""
        link = tag("link")
        m = re.search(r"/vacancy/(\d+)/", link)
        if not m:
            continue
        orders.append(Order(
            source="freelancejob",
            id=m.group(1),
            title=tag("title"),
            url=link,
            description=tag("description")[:1500],
            published=tag("dc:date"),
        ))
    return orders


async def fetch_freelancejob(session: aiohttp.ClientSession) -> list[Order]:
    async with session.get("https://www.freelancejob.ru/rss.php", headers=_headers()) as r:
        raw = await r.read()
    return parse_freelancejob_rss(raw.decode("utf-8", errors="replace"))


# ----------------------------------------------------------------------
# freelance.ru
# ----------------------------------------------------------------------

def parse_freelance_ru(html: str) -> list[Order]:
    orders = []
    for card in re.findall(r'<article class="task-card">(.*?)</article>', html, re.S):
        link = re.search(r'class="task-card__title-link" href="/task/view/(\d+)" title="([^"]*)"', card)
        if not link:
            continue
        desc = re.search(r'<p class="task-card__desc">(.*?)</p>', card, re.S)
        cat = re.search(r'task-chip--cat">([^<]*)<', card)
        budget = re.search(r'task-card__budget">\s*<span class="bold">([^<]*)<', card)
        date = re.search(r'task-card__foot-item" title="([^"]+)"', card)
        description = _strip_tags(desc.group(1)) if desc else ""
        if cat:
            description = f"[{html_lib.unescape(cat.group(1)).strip()}] {description}"
        orders.append(Order(
            source="freelance.ru",
            id=link.group(1),
            title=html_lib.unescape(link.group(2)).strip(),
            url=f"https://freelance.ru/task/view/{link.group(1)}",
            description=description[:1500],
            budget=html_lib.unescape(budget.group(1)).strip() if budget else "",
            published=date.group(1) if date else "",
        ))
    return orders


async def fetch_freelance_ru(session: aiohttp.ClientSession) -> list[Order]:
    async with session.get("https://freelance.ru/task", headers=_headers()) as r:
        return parse_freelance_ru(await r.text())


# ----------------------------------------------------------------------
# Telegram public channels (web preview)
# ----------------------------------------------------------------------

_TG_MSG_RE = re.compile(
    r'<div class="tgme_widget_message [^"]*"[^>]*data-post="([^"]+)"(.*?)'
    r'(?=<div class="tgme_widget_message_wrap|\Z)',
    re.S,
)


def parse_tg_channel(html: str, source: str = "telegram") -> list[dict]:
    """Parse t.me/s/<channel> into dicts: post, id, url, date, text."""
    out = []
    for post, body in _TG_MSG_RE.findall(html):
        text_m = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', body, re.S)
        date_m = re.search(r'<a class="tgme_widget_message_date" href="([^"]+)"><time datetime="([^"]+)"', body)
        raw = text_m.group(1) if text_m else ""
        out.append({
            "post": post,
            "id": post.split("/")[-1],
            "url": date_m.group(1) if date_m else f"https://t.me/{post}",
            "date": date_m.group(2) if date_m else "",
            "text": _strip_tags(raw),
            "links": [html_lib.unescape(h) for h in re.findall(r'href="((?:https?://t\.me/|tel:)[^"]+)"', raw)],
        })
    return out


async def fetch_tg_channel_posts(session: aiohttp.ClientSession, channel: str) -> list[dict]:
    channel = channel.lstrip("@").strip()
    async with session.get(f"https://t.me/s/{channel}", headers=_headers(), allow_redirects=False) as r:
        if r.status != 200:
            return []  # groups and channels without web preview redirect
        return parse_tg_channel(await r.text())


def make_tg_channels_fetcher(channels: tuple[str, ...] | list[str]) -> Fetcher:
    async def fetch(session: aiohttp.ClientSession) -> list[Order]:
        orders = []
        for ch in channels:
            try:
                posts = await fetch_tg_channel_posts(session, ch)
            except (aiohttp.ClientError, TimeoutError) as exc:
                log.warning("t.me/s/%s failed: %s", ch, exc)
                continue
            for p in posts:
                if not p["text"]:
                    continue
                first_line = p["text"].split("\n", 1)[0][:120]
                orders.append(Order(
                    source=f"tg:{ch.lstrip('@')}",
                    id=p["id"],
                    title=first_line,
                    url=p["url"],
                    description=p["text"][:1500],
                    published=p["date"],
                ))
        return orders
    return fetch


SOURCES: dict[str, tuple[str, Fetcher]] = {
    "kwork": ("Kwork", fetch_kwork),
    "fl": ("FL.ru", fetch_fl),
    "freelance_ru": ("Freelance.ru", fetch_freelance_ru),
    "freelancejob": ("FreelanceJob", fetch_freelancejob),
}
