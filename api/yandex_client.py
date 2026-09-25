"""Yandex Maps organization search.

Two modes:
* official "API Поиска по организациям" when YANDEX_API_KEY is set;
* keyless: the maps web page (embedded state-view JSON, 25 results) plus the
  web app's own paginated /maps/api/search endpoint. Pagination requires a
  Chrome TLS fingerprint, hence curl_cffi; without it only the first page is used.
"""

import asyncio
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp

from api.common import USER_AGENTS, clean_social, clean_url, is_social_url, lead_score
from models.organization import Organization

try:
    from curl_cffi.requests import AsyncSession as CurlSession
except ImportError:  # optional dependency
    CurlSession = None

log = logging.getLogger(__name__)

_STATE_RE = re.compile(r'<script[^>]*class="state-view"[^>]*>(.*?)</script>', re.S)
_OFFICIAL_URL = "https://search-maps.yandex.ru/v1/"
_PAGE = 25
_MAX_SKIP = 900  # the web API returns 500 past ~1000


class YandexError(RuntimeError):
    pass


def _qs(params: dict[str, Any]) -> str:
    return "&".join(
        quote(k, safe="-_.~") + "=" + quote(str(params[k]), safe="-_.~")
        for k in sorted(params, key=str.lower)
    )


def _web_sig(qs: str) -> str:
    """djb2-xor signature the maps web app sends as `s`."""
    h = 5381
    for ch in qs:
        h = ((33 * h) ^ ord(ch)) & 0xFFFFFFFF
    return str(h)


def parse_web_item(item: dict[str, Any], city: str = "") -> Organization | None:
    if item.get("type", "business") != "business" or not item.get("id"):
        return None
    sites, socials = [], []
    for url in item.get("urls") or []:
        if is_social_url(url):
            socials.append(clean_social(url))
        else:
            sites.append(clean_url(url))
    for link in item.get("socialLinks") or []:
        href = clean_social(link.get("href", ""))
        if href and href not in socials:
            socials.append(href)
    phones = [p.get("value") or p.get("number", "") for p in item.get("phones") or []]
    rating = item.get("ratingData") or {}
    org_id = str(item["id"])
    seoname = item.get("seoname") or "org"
    org = Organization(
        id=f"ya{org_id}",  # prefixed so ids never collide with 2GIS ids in the dedup table
        name=item.get("title", ""),
        phone=", ".join(p for p in phones if p),
        website=", ".join(sites),
        address=item.get("address") or item.get("fullAddress", ""),
        rating=float(rating.get("ratingValue") or 0),
        socials=", ".join(socials),
        source="yandex",
        city=city,
        category=", ".join(c.get("name", "") for c in (item.get("categories") or [])[:2]),
        reviews=int(rating.get("reviewCount") or 0),
        url=f"https://yandex.ru/maps/org/{seoname}/{org_id}/",
    )
    org.score = lead_score(
        has_website=org.has_website, phone=org.phone, reviews=org.reviews,
        branches=0, rating=org.rating,
    )
    return org


def parse_official_feature(feature: dict[str, Any], city: str = "") -> Organization | None:
    meta = (feature.get("properties") or {}).get("CompanyMetaData") or {}
    if not meta.get("id"):
        return None
    url = meta.get("url", "")
    site, social = ("", clean_social(url)) if is_social_url(url) else (clean_url(url), "")
    phones = [p.get("formatted", "") for p in meta.get("Phones") or []]
    org = Organization(
        id=f"ya{meta['id']}",
        name=meta.get("name", ""),
        phone=", ".join(p for p in phones if p),
        website=site,
        socials=social,
        address=meta.get("address", ""),
        source="yandex",
        city=city,
        category=", ".join(c.get("name", "") for c in (meta.get("Categories") or [])[:2]),
        url=f"https://yandex.ru/maps/org/{meta['id']}/",
    )
    org.score = lead_score(
        has_website=org.has_website, phone=org.phone, reviews=0, branches=0, rating=0,
    )
    return org


class YandexMapsClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str = "",
        request_delay: float = 2.0,
        proxy: str = "",
    ) -> None:
        self.session = session
        self.api_key = api_key
        self.request_delay = request_delay
        self.proxy = proxy or None

    async def search(
        self,
        query: str,
        city: str,
        count: int,
        only_without_site: bool = False,
        skip_ids: set[str] | None = None,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> list[Organization]:
        text = f"{query} {city}".strip()
        if self.api_key:
            pages = self._official_pages(text, city)
        elif CurlSession is not None:
            pages = self._web_pages_curl(text, city)
        else:
            pages = self._web_first_page_aiohttp(text, city)

        skip_ids = skip_ids or set()
        seen: set[str] = set()
        result: list[Organization] = []
        async for batch in pages:
            for org in batch:
                if org.id in seen or org.id in skip_ids:
                    continue
                seen.add(org.id)
                if only_without_site and org.has_website:
                    continue
                result.append(org)
            if on_progress:
                await on_progress(min(len(result), count), count)
            if len(result) >= count:
                break
        return result[:count]

    # ------------------------------------------------------------------
    # Official API
    # ------------------------------------------------------------------

    async def _official_pages(self, text: str, city: str):
        for skip in range(0, 1000, 50):
            params = {
                "apikey": self.api_key, "text": text, "type": "biz",
                "lang": "ru_RU", "results": 50, "skip": skip,
            }
            async with self.session.get(_OFFICIAL_URL, params=params, proxy=self.proxy) as r:
                if r.status != 200:
                    raise YandexError(f"Yandex API вернул {r.status}: {(await r.text())[:200]}")
                data = await r.json(content_type=None)
            feats = data.get("features") or []
            batch = [o for o in (parse_official_feature(f, city) for f in feats) if o]
            yield batch
            if len(feats) < 50:
                return
            await asyncio.sleep(0.3)

    # ------------------------------------------------------------------
    # Keyless web
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_state(html: str) -> dict[str, Any]:
        m = _STATE_RE.search(html)
        if not m:
            raise YandexError("Яндекс вернул страницу без данных (возможно, капча)")
        return json.loads(m.group(1))

    async def _web_first_page_aiohttp(self, text: str, city: str):
        headers = {"User-Agent": random.choice(USER_AGENTS[:3]), "Accept-Language": "ru-RU,ru;q=0.9"}
        url = "https://yandex.ru/maps/?text=" + quote(text)
        async with self.session.get(url, headers=headers, proxy=self.proxy) as r:
            html = await r.text()
        state = self._parse_state(html)
        items = ((state.get("stack") or [{}])[0].get("results") or {}).get("items") or []
        yield [o for o in (parse_web_item(i, city) for i in items) if o]

    async def _web_pages_curl(self, text: str, city: str):
        async with CurlSession(impersonate="chrome", proxy=self.proxy, timeout=30) as s:
            headers = {"Accept-Language": "ru-RU,ru;q=0.9"}
            r = await s.get("https://yandex.ru/maps/?text=" + quote(text), headers=headers)
            if "showcaptcha" in str(r.url):
                raise YandexError("Яндекс показал капчу — попробуйте позже или задайте YANDEX_API_KEY/HTTP_PROXY")
            state = self._parse_state(r.text)
            results = (state.get("stack") or [{}])[0].get("results") or {}
            items = results.get("items") or []
            yield [o for o in (parse_web_item(i, city) for i in items) if o]
            if len(items) < _PAGE:
                return

            cfg = state.get("config") or {}
            token = cfg.get("csrfToken", "")
            session_id = ((cfg.get("counters") or {}).get("analytics") or {}).get("sessionId", "")
            bounds = results.get("requestBounds") or (cfg.get("mapRegion") or {}).get("bounds")
            if not (token and bounds):
                return
            (x1, y1), (x2, y2) = bounds
            ll = f"{(x1 + x2) / 2:.6f},{(y1 + y2) / 2:.6f}"
            spn = f"{abs(x2 - x1):.6f},{abs(y2 - y1):.6f}"
            parts = urlsplit(str(r.url))
            base = f"{parts.scheme}://{parts.netloc}"
            referer = str(r.url)

            for skip in range(_PAGE, _MAX_SKIP, _PAGE):
                await asyncio.sleep(self.request_delay * random.uniform(0.8, 1.3))
                data = None
                for _ in range(3):
                    params = {
                        "ajax": "1", "csrfToken": token, "sessionId": session_id, "text": text,
                        "lang": "ru_RU", "results": str(_PAGE), "skip": str(skip),
                        "ll": ll, "spn": spn, "origin": "maps-pager",
                    }
                    qs = _qs(params)
                    resp = await s.get(
                        f"{base}/maps/api/search?{qs}&s={_web_sig(qs)}",
                        headers={**headers, "Accept": "*/*", "Referer": referer, "X-Retpath-Y": referer},
                    )
                    try:
                        payload = resp.json()
                    except ValueError:
                        payload = {}
                    if set(payload) == {"csrfToken"}:
                        token = payload["csrfToken"]  # token refresh handshake
                        continue
                    data = payload
                    break
                if not data or data.get("type") == "captcha":
                    log.warning("Yandex pagination stopped at skip=%s", skip)
                    return
                items = (data.get("data") or {}).get("items") or []
                if not items:
                    return
                yield [o for o in (parse_web_item(i, city) for i in items) if o]
