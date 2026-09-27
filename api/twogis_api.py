"""2GIS catalog JSON API — the same signed API the 2gis.ru web app uses.

The public web key and signing salt are read from 2gis.ru at runtime (they
change with deploys), so no personal API key is needed.

Not every host can reach 2gis.ru: its origin addresses live in 91.236.48.0/22,
where TCP 443 never opens. Two detours keep the source usable from there —
the landing page is fetched through a public reader, and the catalog API is
dialled on 2GIS's own DDoS-Guard edge, which answers for the same hostname.
Both are transparent fallbacks: on a healthy network the direct route is used
and nothing changes.
"""

import asyncio
import json
import logging
import os
import random
import re
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp

from api.common import USER_AGENTS, lead_score
from api.contacts import split_contacts
from models.organization import Organization

log = logging.getLogger(__name__)

_API = "https://catalog.api.2gis.ru"
_API_HOST = "catalog.api.2gis.ru"
_PAGE = "https://2gis.ru/moscow"
# 2GIS's DDoS-Guard edge: same API, an address outside the unreachable range.
_EDGE_HOST = "ddos-guard.2gis.ru"
# Public reader, used only to fetch the page when 2gis.ru itself is unreachable.
_READER = "https://r.jina.ai/"
# The reader sits behind a bot filter that answers a browser User-Agent with a
# Cloudflare challenge and serves a plain client normally — so ask as one.
_READER_UA = "curl/8.5.0"
_KEY_RE = re.compile(r'"webApiKey":"([^"]+)"')
_BUNDLE_RE = re.compile(r'src="(https://[^"]+/app\.[0-9a-f]+\.js)"')
_SECRET_RE = re.compile(r'this\.KEY=\w+\.webApiKey,this\.a="([^"]+)"')
_FIELDS = ",".join((
    "items.contact_groups", "items.rubrics", "items.reviews", "items.org",
    "items.point", "items.name_ex", "items.address", "items.adm_div",
))
_PAGE_SIZE = 50  # API maximum
_CRED_TTL = 6 * 3600
_EDGE_TTL = 3600.0
# The reader rate-limits bursts, so a failed refresh is retried sooner than the TTL.
_READER_TRIES = 3
_READER_BACKOFF = 5.0
_RETRY_AFTER = 900.0
# A filtered address swallows the SYN, so the direct attempt gets a short leash
# and the edge — which is reachable — gets the full budget.
_DIRECT_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=6)
_EDGE_TIMEOUT = aiohttp.ClientTimeout(total=60)
class TwoGISError(RuntimeError):
    pass


def _default_creds_path() -> Path:
    """Sidecar next to the database, so a restart keeps a working key."""
    return Path(os.environ.get("DB_PATH", "scraper.db")).parent / "twogis_creds.json"


def _load_creds(path: Path) -> tuple[str, str] | None:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        return saved["key"], saved["salt"]
    except (OSError, ValueError, KeyError):
        return None


def _save_creds(path: Path, key: str, secret: str) -> None:
    try:
        path.write_text(json.dumps({"key": key, "salt": secret}), encoding="utf-8")
    except OSError as exc:
        log.debug("Не удалось сохранить ключ 2GIS: %s", exc)


def _env_creds() -> tuple[str, str] | None:
    """Explicit override for a host that can reach neither 2gis.ru nor the reader."""
    key = os.environ.get("TWOGIS_WEB_KEY", "").strip()
    secret = os.environ.get("TWOGIS_SIGN_SALT", "").strip()
    return (key, secret) if key and secret else None


def sign(path: str, params: dict[str, Any], secret: str) -> int:
    """djb2 over path + param values (sorted by key) + salt — mirrors the 2gis.ru bundle."""
    def js(v: Any) -> str:
        return "true" if v is True else "false" if v is False else str(v)

    h = 5381
    for ch in path + "".join(js(params[k]) for k in sorted(params)) + secret:
        h = (h * 33 + ord(ch)) & 0xFFFFFFFF
    return h


class _EdgeResolver(aiohttp.abc.AbstractResolver):
    """Dial one host on 2GIS's DDoS-Guard edge instead of its origin address.

    Only the resolved address is swapped — aiohttp still builds SNI and the Host
    header from the URL, so TLS and routing on the 2GIS side stay correct.
    """

    def __init__(self, host: str, edge_host: str, ttl: float = _EDGE_TTL) -> None:
        self._host = host
        self._edge_host = edge_host
        self._ttl = ttl
        self._ip = ""
        self._at = 0.0
        self._fallback: aiohttp.abc.AbstractResolver | None = None

    @property
    def _default(self) -> aiohttp.abc.AbstractResolver:
        # Built on first use: DefaultResolver captures the running loop in __init__.
        if self._fallback is None:
            self._fallback = aiohttp.resolver.DefaultResolver()
        return self._fallback

    async def _edge_ip(self) -> str:
        if self._ip and time.time() - self._at < self._ttl:
            return self._ip
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                self._edge_host, 443, type=socket.SOCK_STREAM
            )
            self._ip = infos[0][4][0]
        except OSError as exc:
            log.warning("Не удалось определить адрес DDoS-Guard 2GIS: %s", exc)
            self._ip = ""
        self._at = time.time()
        return self._ip

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list:
        if host == self._host and (ip := await self._edge_ip()):
            return [{
                "hostname": host, "host": ip, "port": port,
                "family": socket.AF_INET, "proto": socket.IPPROTO_TCP, "flags": 0,
            }]
        return await self._default.resolve(host, port, family)

    async def close(self) -> None:
        if self._fallback is not None:
            await self._fallback.close()


class TwoGISApi:
    # Маршрут до 2GIS - свойство хоста, а не экземпляра: прямая дорога либо
    # доступна всему процессу, либо фильтруется всему процессу.
    # None = direct route untried, False = it is filtered, so stop paying for it.
    _direct_ok: bool | None = None

    def __init__(
        self,
        session: aiohttp.ClientSession,
        request_delay: float = 0.3,
        proxy: str = "",
        creds_path: Path | None = None,
    ) -> None:
        self.session = session
        self.request_delay = request_delay
        self.proxy = proxy or None
        self.ua = random.choice(USER_AGENTS[:3])
        self._edge_session: aiohttp.ClientSession | None = None
        # (key, secret, fetched_at) на экземпляр: параллельные поиски не
        # дерутся за общий кеш. Переживает рестарт через sidecar-файл.
        self._creds: tuple[str, str, float] | None = None
        self._regions: dict[str, tuple[str, str, str]] = {}
        self.creds_path = creds_path or _default_creds_path()

    async def close(self) -> None:
        """Release the edge session, if one was opened."""
        if self._edge_session and not self._edge_session.closed:
            await self._edge_session.close()
        self._edge_session = None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _get(
        self, url: str, params: dict[str, Any] | None, headers: dict[str, str], via_edge: bool
    ) -> Any:
        if not via_edge:
            return self.session.get(
                url, params=params, headers=headers,
                proxy=self.proxy, timeout=_DIRECT_TIMEOUT,
            )
        if self._edge_session is None or self._edge_session.closed:
            self._edge_session = aiohttp.ClientSession(
                timeout=_EDGE_TIMEOUT,
                connector=aiohttp.TCPConnector(
                    resolver=_EdgeResolver(_API_HOST, _EDGE_HOST)
                ),
            )
        return self._edge_session.get(url, params=params, headers=headers)

    @staticmethod
    def _routes() -> tuple[bool, ...]:
        """Direct first while the network allows it, edge-only once direct has failed."""
        return (True,) if TwoGISApi._direct_ok is False else (False, True)

    async def _fetch(self, path: str, params: dict[str, Any], headers: dict[str, str]) -> dict:
        last: Exception | None = None
        for via_edge in self._routes():
            try:
                async with self._get(_API + path, params, headers, via_edge) as r:
                    data = await r.json(content_type=None)
                if not via_edge:
                    TwoGISApi._direct_ok = True
                return data
            except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
                last = exc
                if not via_edge:
                    TwoGISApi._direct_ok = False
                log.debug("2GIS %s (%s) недоступен: %s",
                          path, "edge" if via_edge else "direct", exc)
        raise last or TwoGISError(f"2GIS API {path} недоступен")

    async def _page(self) -> str:
        """HTML of the 2gis.ru landing page — it carries the web key and the bundle URL."""
        headers = {"User-Agent": self.ua, "Accept-Language": "ru-RU,ru;q=0.9"}
        try:
            async with self.session.get(
                _PAGE, headers=headers, cookies={"dg5_museum_accept": "true"},
                proxy=self.proxy, timeout=_DIRECT_TIMEOUT,
            ) as r:
                html = await r.text()
            if _KEY_RE.search(html):
                return html
        except (TimeoutError, aiohttp.ClientError) as exc:
            log.debug("2gis.ru напрямую недоступен: %s", exc)

        log.info("2gis.ru недоступен напрямую — беру страницу через %s", _READER.rstrip("/"))
        status = "нет ответа"
        for attempt in range(_READER_TRIES):
            if attempt:
                await asyncio.sleep(_READER_BACKOFF * attempt)
            async with self.session.get(
                _READER + _PAGE,
                headers={"User-Agent": _READER_UA, "X-Return-Format": "html"},
            ) as r:
                status, html = f"HTTP {r.status}", await r.text()
            if _KEY_RE.search(html):
                return html
            log.debug("Читатель вернул %s", status)
        raise TwoGISError(
            f"Страница 2gis.ru недоступна напрямую и через {_READER.rstrip('/')} ({status})"
        )

    async def _fetch_creds(self) -> tuple[str, str]:
        html = await self._page()
        key_m, bundle_m = _KEY_RE.search(html), _BUNDLE_RE.search(html)
        if not key_m or not bundle_m:
            raise TwoGISError("Не удалось получить ключ 2GIS со страницы 2gis.ru")
        # The bundle sits on the asset CDN, which stays reachable where 2gis.ru does not.
        headers = {"User-Agent": self.ua, "Accept-Language": "ru-RU,ru;q=0.9"}
        async with self.session.get(bundle_m.group(1), headers=headers, proxy=self.proxy) as r:
            js = await r.text()
        secret_m = _SECRET_RE.search(js)
        if not secret_m:
            raise TwoGISError("Не удалось найти подпись запросов в бандле 2GIS")
        return key_m.group(1), secret_m.group(1)

    async def _credentials(self, force: bool = False) -> tuple[str, str]:
        cached = self._creds
        if cached and not force and time.time() - cached[2] < _CRED_TTL:
            return cached[0], cached[1]

        # A working pair is worth keeping: the reader rate-limits bursts, and losing
        # the key over one bad response would take the source down until it recovers.
        # So refresh when it is cheap, and fall back to what we already have when it
        # is not. TWOGIS_WEB_KEY/TWOGIS_SIGN_SALT in .env are the last resort.
        fallback = cached[:2] if cached else (_load_creds(self.creds_path) or _env_creds())
        try:
            pair = await self._fetch_creds()
        except Exception as exc:
            if force or not fallback:
                raise TwoGISError(f"Не удалось получить ключ 2GIS: {exc}") from exc
            log.warning("Не удалось обновить ключ 2GIS (%s) — работаю на прежнем", exc)
            # Stale enough to be retried in _RETRY_AFTER, not in six hours.
            self._creds = (*fallback, time.time() - _CRED_TTL + _RETRY_AFTER)
            return fallback

        self._creds = (*pair, time.time())
        _save_creds(self.creds_path, *pair)
        return pair

    async def _call(self, path: str, params: dict[str, Any], signed: bool = True) -> dict:
        for attempt in range(3):
            key, secret = await self._credentials(force=attempt > 0)
            p = {**params, "key": key}
            if signed:
                p["stat[sid]"] = str(uuid.uuid4())
                p["r"] = sign(path, p, secret)
            headers = {
                "User-Agent": self.ua,
                "Referer": "https://2gis.ru/",
                "Origin": "https://2gis.ru",
                "Accept": "application/json",
            }
            try:
                data = await self._fetch(path, p, headers)
            except (TimeoutError, aiohttp.ClientError, ValueError, TwoGISError) as exc:
                log.warning("2GIS API %s failed: %s", path, exc)
                await asyncio.sleep(2 ** attempt)
                continue
            code = (data.get("meta") or {}).get("code")
            if code in (401, 403):
                continue  # key/salt rotated — refetch
            if code in (429, 503):
                await asyncio.sleep(2 ** attempt + random.random())
                continue
            return data
        raise TwoGISError(f"2GIS API {path} недоступен")

    # ------------------------------------------------------------------
    # Regions
    # ------------------------------------------------------------------

    async def resolve_city(self, city: str) -> tuple[str, str, str]:
        """City name -> (region_id, slug, canonical name). region_id '' if unknown to 2GIS."""
        k = city.strip().lower()
        if k in self._regions:
            return self._regions[k]
        data = await self._call(
            "/2.0/region/search", {"q": city, "fields": "items.code"}, signed=False
        )
        items = (data.get("result") or {}).get("items") or []
        found = ("", "", city)
        if items and str(items[0].get("id", "0")) != "0":
            it = items[0]
            found = (str(it["id"]), it.get("code", ""), it.get("name", city))
        self._regions[k] = found
        return found

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        city: str,
        count: int,
        only_without_site: bool = False,
        skip_ids: set[str] | None = None,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
        max_pages: int = 40,
    ) -> list[Organization]:
        region_id, slug, city_name = await self.resolve_city(city)
        params: dict[str, Any] = {
            "type": "branch",
            "page_size": _PAGE_SIZE,
            "locale": "ru_RU",
            "fields": _FIELDS,
        }
        if region_id:
            params.update(q=query, region_id=region_id)
        else:
            # Small towns outside 2GIS regions: put the city into the query text.
            params["q"] = f"{query} {city}"

        skip_ids = skip_ids or set()
        result: list[Organization] = []
        seen: set[str] = set()
        total = None
        for page in range(1, max_pages + 1):
            data = await self._call("/3.0/items", {**params, "page": page})
            code = (data.get("meta") or {}).get("code")
            if code == 404:
                break  # past the last page
            res = data.get("result") or {}
            items = res.get("items") or []
            total = res.get("total", total)
            if not items:
                break
            for item in items:
                org = parse_item(item, city_name, slug)
                if not org or org.id in seen or org.id in skip_ids:
                    continue
                seen.add(org.id)
                if only_without_site and org.has_website:
                    continue
                result.append(org)
            if on_progress:
                await on_progress(min(len(result), count), count)
            if len(result) >= count or (total and page * _PAGE_SIZE >= total):
                break
            if self.request_delay:
                await asyncio.sleep(self.request_delay * random.uniform(0.7, 1.4))
        return result[:count]


def parse_item(item: dict[str, Any], city: str = "", slug: str = "") -> Organization | None:
    raw_id = str(item.get("id", ""))
    if not raw_id:
        return None
    firm_id = raw_id.split("_", 1)[0]
    name = (item.get("name_ex") or {}).get("primary") or item.get("name", "")
    ext = (item.get("name_ex") or {}).get("extension", "")
    contacts = split_contacts(item.get("contact_groups"))

    reviews = item.get("reviews") or {}
    org_info = item.get("org") or {}
    rubrics = [r.get("name", "") for r in item.get("rubrics") or [] if r.get("kind") == "primary"]
    rating = float(reviews.get("general_rating") or 0)
    review_count = int(reviews.get("general_review_count") or 0)
    branches = int(org_info.get("branch_count") or 0)
    phone = ", ".join(contacts["phone"])

    org = Organization(
        id=firm_id,
        name=f"{name}, {ext}" if ext and ext.lower() not in name.lower() else name,
        phone=phone,
        email=", ".join(contacts["email"]),
        website=", ".join(contacts["website"]),
        address=item.get("address_name", ""),
        rating=rating,
        socials=", ".join(contacts["socials"]),
        source="2gis",
        city=city,
        category=", ".join(rubrics),
        reviews=review_count,
        branches=branches,
        url=f"https://2gis.ru/{slug or 'moscow'}/firm/{firm_id}",
    )
    org.score = lead_score(
        has_website=org.has_website, phone=phone, reviews=review_count,
        branches=branches, rating=rating,
    )
    return org
