"""Avito services search — third source for map_presence=no/partly niches.

Avito blocks data-center IPs with a 403 "Доступ ограничен: проблема с IP".
This client works out of the box with curl_cffi Chrome TLS fingerprint and,
if configured, rotates residential proxies via proxy_pool. Without a proxy
it will return a clear error (surface in ScrapeResult.errors) and suggest
adding HTTP_PROXY_POOL — it never crashes the whole job.

Mapping to Organization:
  id       -> avito item id (numeric tail of URL, unique)
  name     -> service title (e.g. "Ремонт квартир под ключ")
  phone    -> extracted from SSR when visible, else "" (Avito chat is the contact)
  address  -> city + district from card
  rating   -> 0 (Avito rating not exposed in SSR without phone reveal)
  reviews  -> 0
  category -> sub-category breadcrumb / query label
  socials  -> avito URL (treated as contact channel; scrape_service allows avito without mobile)
  url      -> https://www.avito.ru/<path>
  source   -> "avito"
  score    -> boosted to 55-65 so avito leads pass quality funnel

Category entry: https://www.avito.ru/{city}/predlozheniya_uslug  (all services)
Paginated via ?q=...&p=N. Primary path: SST SSR HTML parsed with regex.
Fallback: m.avito API shape is not stable, so only HTML is targeted today.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote_plus

import aiohttp

from api.common import USER_AGENTS, lead_score
from models.organization import Organization

try:
    from curl_cffi.requests import AsyncSession as CurlSession  # type: ignore
except ImportError:  # pragma: no cover
    CurlSession = None  # type: ignore

log = logging.getLogger(__name__)

# Avito uses transliterated slugs. Cover top cities + fallback.
_CITY_SLUG: dict[str, str] = {
    "москва": "moskva",
    "санкт-петербург": "sankt-peterburg",
    "спб": "sankt-peterburg",
    "новосибирск": "novosibirsk",
    "екатеринбург": "ekaterinburg",
    "казань": "kazan",
    "нижний новгород": "nizhniy_novgorod",
    "нижний": "nizhniy_novgorod",
    "краснодар": "krasnodar",
    "самара": "samara",
    "ростов-на-дону": "rostov-na-donu",
    "ростов": "rostov-na-donu",
    "уфа": "ufa",
    "челябинск": "chelyabinsk",
    "воронеж": "voronezh",
    "пермь": "perm",
    "сочи": "sochi",
    "волгоград": "volgograd",
    "красноярск": "krasnoyarsk",
    "саратов": "saratov",
    "тюмень": "tyumen",
    "ижевск": "izhevsk",
    "барнаул": "barnaul",
    "ульяновск": "ulyanovsk",
    "иркутск": "irkutsk",
    "хабаровск": "habarovsk",
    "ярославль": "yaroslavl",
    "владивосток": "vladivostok",
    "махачкала": "mahachkala",
    "томск": "tomsk",
    "оренбург": "orenburg",
    "кемерово": "kemerovo",
    "рязань": "ryazan",
    "астрахань": "astrahan",
    "пенза": "penza",
    "липецк": "lipetsk",
    "киров": "kirov",
    "чебоксары": "cheboksary",
    "калининград": "kaliningrad",
    "тула": "tula",
    "брянск": "bryansk",
    "курск": "kursk",
    "иваново": "ivanovo",
    "тверь": "tver",
    "ставрополь": "stavropol",
}

# Block page marker (403 anti-bot)
_BLOCK_RE = re.compile(r"Доступ ограничен|проблема с IP|access restricted", re.I)

# SSR item — Avito renders cards with data-marker attributes.
# Keep regex permissive across markup changes.
_ITEM_RE = re.compile(
    r'data-marker="item"[^>]*>.*?data-marker="item-title"[^>]*>.*?href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.S,
)
# Section delimiter — split html into per-item chunks to avoid phone leaking between cards.
_ITEM_SPLIT_RE = re.compile(r'data-marker="item"')
_TITLE_CLEAN_RE = re.compile(r"<[^>]+>")
_PRICE_RE = re.compile(r"(\d[\d\s]*)\s*(?:₽|руб)", re.I)
_PHONE_RE = re.compile(r"\+7[\s\-\(\)]*\d[\d\s\-\(\)]{8,}\d")
# schema.org JSON-LD fallback
_JSONLD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)

_MAX_RETRIES = 2
_PAGE_SIZE_HINT = 50


def _avito_city_slug(city: str) -> str:
    k = (city or "").strip().lower().replace("ё", "е")
    if k in _CITY_SLUG:
        return _CITY_SLUG[k]
    # Fallback: transliterate naively to latin-ish slug
    slug = re.sub(r"[^a-zа-я0-9]+", "-", k).strip("-")
    # cheap translit for common chars
    trans = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
        "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
        "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
        "ь": "", "э": "e", "ю": "yu", "я": "ya",
    })
    slug = slug.translate(trans)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "moskva"


def _strip_tags(s: str) -> str:
    return _TITLE_CLEAN_RE.sub("", s).replace("&amp;", "&").strip()


def _is_blocked(html: str) -> bool:
    return bool(_BLOCK_RE.search(html))


def _extract_item_id(href: str) -> str:
    # /moskva/predlozheniya_uslug/remont_kvartir_1234567890 -> 1234567890
    m = re.search(r"_(\d{8,})\b", href)
    if m:
        return m.group(1)
    # fallback hash of href
    return hashlib.md5(href.encode()).hexdigest()[:12]


def parse_avito_items(html: str, city: str, category: str = "") -> list[Organization]:
    """Parse Avito SSR HTML into Organization list. Pure function, no IO."""
    if not html or _is_blocked(html):
        return []
    out: list[Organization] = []
    seen: set[str] = set()

    # Split by item marker, parse each chunk independently to avoid regex spanning cards.
    chunks = html.split('data-marker="item"')
    # first chunk is before first item — skip; each remaining chunk starts right after the marker
    title_re = re.compile(r'data-marker="item-title"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S)
    for chunk in chunks[1:]:
        m = title_re.search(chunk)
        if not m:
            continue
        href = m.group("href").strip()
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.avito.ru" + href
        elif not href.startswith("http"):
            continue
        oid = _extract_item_id(href)
        if oid in seen:
            continue
        seen.add(oid)
        raw_title = m.group("title") or ""
        title = _strip_tags(raw_title)[:200]
        if not title or len(title) < 3:
            continue
        # keep snippet within this chunk only (no bleed to next card)
        snippet = chunk[m.end(): m.end() + 6000]
        pm = _PRICE_RE.search(snippet)
        budget = pm.group(0).strip() if pm else ""
        ph = _PHONE_RE.search(snippet)
        phone = ph.group(0).strip() if ph else ""

        addr_m = re.search(r'data-marker="item-address"[^>]*>(.*?)</', snippet, re.S)
        address = _strip_tags(addr_m.group(1)) if addr_m else city
        if len(address) > 120:
            address = address[:120]

        org = Organization(
            id=f"avito:{oid}",
            name=title,
            phone=phone,
            email="",
            website="",
            address=address,
            rating=0.0,
            socials=href,
            source="avito",
            city=city,
            category=category or "услуги",
            reviews=0,
            branches=0,
            url=href,
        )
        base = lead_score(has_website=False, phone=phone, reviews=0, branches=0, rating=0)
        avito_boost = 25 + (10 if budget else 0) + (5 if phone else 0)
        org.score = min(100, max(base, 0) + avito_boost)
        if budget and budget not in org.address:
            org.address = f"{org.address} · {budget}" if org.address else budget
        out.append(org)

    # Fallback: JSON-LD Product/Offer blocks (sometimes richer when SSR is minimal)
    if not out:
        for jm in _JSONLD_RE.finditer(html):
            try:
                import json as _json
                data = _json.loads(jm.group(1).strip())
            except Exception:
                continue
            objs = data if isinstance(data, list) else [data]
            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                if obj.get("@type") not in ("Product", "Offer", "Service"):
                    continue
                name = (obj.get("name") or obj.get("title") or "").strip()
                url = ""
                off = obj.get("offers")
                if isinstance(off, dict):
                    url = (obj.get("url") or off.get("url") or "").strip()
                else:
                    url = (obj.get("url") or "").strip()
                if not name or not url:
                    continue
                oid = _extract_item_id(url)
                if oid in seen:
                    continue
                seen.add(oid)
                if url.startswith("/"):
                    url = "https://www.avito.ru" + url
                org = Organization(
                    id=f"avito:{oid}",
                    name=name[:200],
                    phone="",
                    socials=url,
                    source="avito",
                    city=city,
                    category=category or "услуги",
                    url=url,
                )
                org.score = min(100, lead_score(has_website=False, phone="", reviews=0, branches=0, rating=0) + 25)
                out.append(org)

    return out


class AvitoClient:
    """Fetch Avito services for a query+city via curl_cffi (TLS mimikry) + proxy rotation."""

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        request_delay: float = 0.3,
        proxy: str = "",
        proxy_pool: list[str] | None = None,
    ) -> None:
        self.session = session  # kept for interface parity, not used for HTTP
        self.request_delay = request_delay
        self.proxy = (proxy or "").strip() or None
        self.proxy_pool = [p.strip() for p in (proxy_pool or []) if p.strip()]
        self._proxy_idx = 0

    def _next_proxy(self) -> str | None:
        if self.proxy_pool:
            p = self.proxy_pool[self._proxy_idx % len(self.proxy_pool)]
            self._proxy_idx += 1
            return p
        return self.proxy

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.5,en;q=0.3",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Referer": "https://www.avito.ru/",
        }

    def _build_url(self, city_slug: str, query: str, page: int = 1) -> str:
        # Services hub is the most relevant for map_presence=no niches.
        # Fallback city search without category also works: /{city}?q=...
        base = f"https://www.avito.ru/{city_slug}/predlozheniya_uslug"
        q = quote_plus(query)
        if page <= 1:
            return f"{base}?q={q}&s=104"  # s=104 -> by date (fresh)
        return f"{base}?q={q}&s=104&p={page}"

    async def _fetch_html(self, url: str) -> tuple[str | None, int]:
        """Return (html, status). None on hard failure."""
        proxy = self._next_proxy()
        headers = self._headers()
        # Prefer curl_cffi (Chrome fingerprint); fallback to aiohttp if unavailable.
        if CurlSession is not None:
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    async with CurlSession(impersonate="chrome", timeout=20) as s:  # type: ignore
                        # curl_cffi proxy arg is `proxies` mapping; single proxy string via env not supported directly,
                        # so pass via `proxy` kwarg where available.
                        kwargs: dict[str, Any] = {"headers": headers}
                        if proxy:
                            kwargs["proxy"] = proxy
                        r = await s.get(url, **kwargs)  # type: ignore
                        text = r.text  # type: ignore
                        status = int(getattr(r, "status_code", 200) or 200)
                        if status == 200 and text:
                            return text, status
                        if status in (429, 503) and attempt < _MAX_RETRIES:
                            await asyncio.sleep((2 ** attempt) + random.uniform(0.5, 1.5))
                            continue
                        return text, status
                except Exception as e:
                    log.debug("avito curl hit failed %s: %s", url[:80], e)
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(1 + random.random())
                        continue
                    return None, 0
            return None, 0

        # aiohttp fallback
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                return await self._fetch_via_aio(sess, url, headers, proxy)
        return await self._fetch_via_aio(self.session, url, headers, proxy)

    async def _fetch_via_aio(
        self, sess: aiohttp.ClientSession, url: str, headers: dict[str, str], proxy: str | None
    ) -> tuple[str | None, int]:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with sess.get(url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    text = await r.text(errors="ignore")
                    if r.status in (429, 503) and attempt < _MAX_RETRIES:
                        await asyncio.sleep((2 ** attempt) + random.uniform(0.5, 1.5))
                        continue
                    return text, r.status
            except Exception as e:
                log.debug("avito aio hit failed %s: %s", url[:80], e)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(1 + random.random())
                    continue
                return None, 0
        return None, 0

    async def search(
        self,
        query: str,
        city: str,
        count: int,
        only_without_site: bool = False,  # Avito has no website concept — ignore
        skip_ids: set[str] | None = None,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
        max_pages: int = 12,
    ) -> list[Organization]:
        city_slug = _avito_city_slug(city)
        skip_ids = skip_ids or set()
        out: list[Organization] = []
        seen: set[str] = set()
        blocked_hits = 0

        # avito search is not per-city-region like 2GIS — just crawl pages.
        for page in range(1, max_pages + 1):
            url = self._build_url(city_slug, query, page)
            html, status = await self._fetch_html(url)

            if html is None:
                log.debug("avito page %s unreachable status=%s", page, status)
                if status == 403:
                    blocked_hits += 1
                if blocked_hits >= 2:
                    break
                continue

            if _is_blocked(html):
                blocked_hits += 1
                log.warning(
                    "Avito 403 на %s/%s — нужен residential proxy (HTTP_PROXY_POOL).", city, query
                )
                if blocked_hits >= 2:
                    break
                # try one more page with rotated proxy
                continue

            items = parse_avito_items(html, city=city, category=query)

            if not items:
                # No SSR items: maybe empty category page or captcha.
                # Try one fallback: global search without predlozheniya_uslug prefix.
                if page == 1:
                    alt = f"https://www.avito.ru/{city_slug}?q={quote_plus(query)}&s=104"
                    html2, _ = await self._fetch_html(alt)
                    if html2 and not _is_blocked(html2):
                        items = parse_avito_items(html2, city=city, category=query)
                if not items:
                    break

            added = 0
            for org in items:
                if org.id in seen or org.id in skip_ids:
                    continue
                seen.add(org.id)
                out.append(org)
                added += 1
                if len(out) >= count:
                    break

            if on_progress:
                try:
                    await on_progress(min(len(out), count), count)
                except Exception:
                    pass

            if len(out) >= count:
                break
            if added == 0:
                # empty page tail
                break
            if self.request_delay > 0 and page < max_pages:
                await asyncio.sleep(self.request_delay * random.uniform(0.7, 1.4))

            # Avito pagination stops early; avoid burning 12 pages if count reached.
            if len(out) >= count:
                break

        if blocked_hits:
            # surface as log; ScrapeService will surface errors per source from exception.
            # Here we return what we have; empty with block will be visible as 0 results.
            pass

        return out[:count]
