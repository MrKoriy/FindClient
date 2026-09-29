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


def _split_bbox(ll: str, spn: str, parts: int = 4) -> list[tuple[str, str]]:
    """Разбить bbox на квадранты.

    ll="lon,lat" (центр), spn="dLon,dLat" (размер). parts=4 -> 2x2,
    parts=2 -> север/юг (fallback когда bbox узкий).
    Возвращает список (ll_i, spn_i) для каждого квадранта.
    """
    try:
        lon, lat = map(float, ll.split(","))
        dlon, dlat = map(float, spn.split(","))
    except Exception:
        return [(ll, spn)]
    if parts == 2:
        # north / south split
        hlat = dlat / 2
        q_lat = dlat / 4
        return [
            (f"{lon:.6f},{lat + q_lat:.6f}", f"{dlon:.6f},{hlat:.6f}"),
            (f"{lon:.6f},{lat - q_lat:.6f}", f"{dlon:.6f},{hlat:.6f}"),
        ]
    # parts == 4 default -> 2x2
    hlon, hlat = dlon / 2, dlat / 2
    q_lon, q_lat = dlon / 4, dlat / 4
    return [
        (f"{lon - q_lon:.6f},{lat + q_lat:.6f}", f"{hlon:.6f},{hlat:.6f}"),  # NW
        (f"{lon + q_lon:.6f},{lat + q_lat:.6f}", f"{hlon:.6f},{hlat:.6f}"),  # NE
        (f"{lon - q_lon:.6f},{lat - q_lat:.6f}", f"{hlon:.6f},{hlat:.6f}"),  # SW
        (f"{lon + q_lon:.6f},{lat - q_lat:.6f}", f"{hlon:.6f},{hlat:.6f}"),  # SE
    ]


# alias для совместимости с ТЗ (оба имени работают)
def _bbox_quadrants(ll: str, spn: str) -> list[tuple[str, str]]:
    return _split_bbox(ll, spn, parts=4)


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
        proxy_pool: list[str] | None = None,
        bbox_split: bool = True,
    ) -> None:
        self.session = session
        self.api_key = api_key
        self.request_delay = request_delay
        self.proxy = proxy or None
        self.proxy_pool = [p for p in (proxy_pool or []) if p]
        self.bbox_split = bbox_split
        self._proxy_idx = 0

    def _next_proxy(self) -> str | None:
        if self.proxy_pool:
            p = self.proxy_pool[self._proxy_idx % len(self.proxy_pool)]
            self._proxy_idx += 1
            return p
        return self.proxy

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
            log.warning("curl_cffi не установлен — Яндекс ограничен первой страницей (25 результатов)")
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
        # bbox 2x2: если не набрали count и bbox_split включён — добираем квадрантами
        if self.bbox_split and len(result) < count and CurlSession is not None and not self.api_key:
            more = await self._bbox_extra(text, city, count - len(result), seen | skip_ids)
            for org in more:
                if org.id in seen or org.id in skip_ids:
                    continue
                seen.add(org.id)
                if only_without_site and org.has_website:
                    continue
                result.append(org)
                if len(result) >= count:
                    break
        return result[:count]

    async def _bbox_extra(
        self, text: str, city: str, need: int, skip_ids: set[str]
    ) -> list[Organization]:
        """Добрать результаты разбивкой исходного bbox на квадранты 2x2.

        Берёт исходные ll/spn из записи карты, разбивает на 4 квадранта
        (новый ll_i + половинный spn_i) и пагинирует каждый.
        Дедуплицирует по id; ротирует proxy на каждый квадрант если задан pool.
        При ошибке квадранта (капча/сеть) пропускает его.
        """
        if need <= 0 or CurlSession is None:
            return []
        # получить исходный bbox из первичной страницы (дешёвый повторный GET)
        orig_ll, orig_spn, token, session_id, base, referer = await self._fetch_bbox_meta(text)
        if not (orig_ll and orig_spn and token):
            return []
        quadrants = _split_bbox(orig_ll, orig_spn, parts=4)
        out: list[Organization] = []
        seen: set[str] = set(skip_ids)
        for q_ll, q_spn in quadrants:
            if len(out) >= need:
                break
            proxy = self._next_proxy()
            try:
                async for batch in self._paginate_curl(
                    text, city, q_ll, q_spn, token, session_id, base, referer, proxy=proxy
                ):
                    for org in batch:
                        if org.id in seen:
                            continue
                        seen.add(org.id)
                        out.append(org)
                        if len(out) >= need:
                            break
                    if len(out) >= need:
                        break
            except Exception as exc:
                log.warning("Yandex bbox quadrant %s failed: %s", q_ll, exc)
                continue
        if out:
            log.info("Yandex bbox 2x2: +%d extra (need=%d)", len(out), need)
        return out

    async def _fetch_bbox_meta(
        self, text: str
    ) -> tuple[str, str, str, str, str, str]:
        """GET /maps/?text= — достать ll/spn/token для разбивки. Возвращает (ll, spn, token, sid, base, referer)."""
        proxy = self._next_proxy()
        async with CurlSession(impersonate="chrome", proxy=proxy, timeout=30) as s:
            headers = {"Accept-Language": "ru-RU,ru;q=0.9"}
            r = await s.get("https://yandex.ru/maps/?text=" + quote(text), headers=headers)
            if "showcaptcha" in str(r.url):
                return "", "", "", "", "", ""
            try:
                state = self._parse_state(r.text)
            except YandexError:
                return "", "", "", "", "", ""
            results = (state.get("stack") or [{}])[0].get("results") or {}
            cfg = state.get("config") or {}
            token = cfg.get("csrfToken", "")
            session_id = ((cfg.get("counters") or {}).get("analytics") or {}).get("sessionId", "")
            bounds = results.get("requestBounds") or (cfg.get("mapRegion") or {}).get("bounds")
            if not bounds:
                return "", "", token, session_id, "", str(r.url)
            (x1, y1), (x2, y2) = bounds
            ll = f"{(x1 + x2) / 2:.6f},{(y1 + y2) / 2:.6f}"
            spn = f"{abs(x2 - x1):.6f},{abs(y2 - y1):.6f}"
            parts = urlsplit(str(r.url))
            base = f"{parts.scheme}://{parts.netloc}"
            return ll, spn, token, session_id, base, str(r.url)

    async def _paginate_curl(
        self,
        text: str,
        city: str,
        ll: str,
        spn: str,
        token: str,
        session_id: str,
        base: str,
        referer: str,
        proxy: str | None = None,
    ):
        """Пагинация одного квадранта: skip 0..900, yield batches. Proxy per-quadrant уже выбран."""
        if not (token and base):
            return
        eff_proxy = proxy if proxy is not None else self._next_proxy()
        async with CurlSession(impersonate="chrome", proxy=eff_proxy, timeout=30) as s:
            headers = {"Accept-Language": "ru-RU,ru;q=0.9"}
            for skip in range(0, _MAX_SKIP, _PAGE):
                await asyncio.sleep(self.request_delay * random.uniform(0.8, 1.3))
                data = None
                cur_token = token
                for _ in range(3):
                    params = {
                        "ajax": "1", "csrfToken": cur_token, "sessionId": session_id,
                        "text": text, "lang": "ru_RU", "results": str(_PAGE),
                        "skip": str(skip), "ll": ll, "spn": spn, "origin": "maps-pager",
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
                        cur_token = payload["csrfToken"]
                        continue
                    data = payload
                    break
                if not data or data.get("type") == "captcha":
                    return
                items = (data.get("data") or {}).get("items") or []
                if not items:
                    return
                yield [o for o in (parse_web_item(i, city) for i in items) if o]

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
        async with CurlSession(impersonate="chrome", proxy=self._next_proxy(), timeout=30) as s:
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
