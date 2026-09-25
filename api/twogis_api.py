"""2GIS catalog JSON API — the same signed API the 2gis.ru web app uses.

The public web key and signing salt are read from 2gis.ru at runtime (they
change with deploys), so no personal API key is needed.
"""

import asyncio
import logging
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from api.common import USER_AGENTS, clean_social, clean_url, is_social_url, lead_score
from models.organization import Organization

log = logging.getLogger(__name__)

_API = "https://catalog.api.2gis.ru"
_KEY_RE = re.compile(r'"webApiKey":"([^"]+)"')
_BUNDLE_RE = re.compile(r'src="(https://[^"]+/app\.[0-9a-f]+\.js)"')
_SECRET_RE = re.compile(r'this\.KEY=\w+\.webApiKey,this\.a="([^"]+)"')
_FIELDS = ",".join((
    "items.contact_groups", "items.rubrics", "items.reviews", "items.org",
    "items.point", "items.name_ex", "items.address", "items.adm_div",
))
_PAGE_SIZE = 50  # API maximum
_CRED_TTL = 6 * 3600
_MESSENGER_TYPES = frozenset((
    "vkontakte", "vk", "instagram", "facebook", "twitter", "youtube", "telegram",
    "whatsapp", "viber", "odnoklassniki", "ok", "max", "skype", "icq",
))


class TwoGISError(RuntimeError):
    pass


def sign(path: str, params: dict[str, Any], secret: str) -> int:
    """djb2 over path + param values (sorted by key) + salt — mirrors the 2gis.ru bundle."""
    def js(v: Any) -> str:
        return "true" if v is True else "false" if v is False else str(v)

    h = 5381
    for ch in path + "".join(js(params[k]) for k in sorted(params)) + secret:
        h = (h * 33 + ord(ch)) & 0xFFFFFFFF
    return h


class TwoGISApi:
    _creds: tuple[str, str, float] | None = None  # (key, secret, fetched_at), shared per process
    _regions: dict[str, tuple[str, str, str]] = {}

    def __init__(self, session: aiohttp.ClientSession, request_delay: float = 0.3) -> None:
        self.session = session
        self.request_delay = request_delay
        self.ua = random.choice(USER_AGENTS[:3])

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    async def _credentials(self, force: bool = False) -> tuple[str, str]:
        cached = TwoGISApi._creds
        if cached and not force and time.time() - cached[2] < _CRED_TTL:
            return cached[0], cached[1]

        headers = {"User-Agent": self.ua, "Accept-Language": "ru-RU,ru;q=0.9"}
        cookies = {"dg5_museum_accept": "true"}
        async with self.session.get("https://2gis.ru/moscow", headers=headers, cookies=cookies) as r:
            html = await r.text()
        key_m, bundle_m = _KEY_RE.search(html), _BUNDLE_RE.search(html)
        if not key_m or not bundle_m:
            raise TwoGISError("Не удалось получить ключ 2GIS со страницы 2gis.ru")
        async with self.session.get(bundle_m.group(1), headers=headers) as r:
            js = await r.text()
        secret_m = _SECRET_RE.search(js)
        if not secret_m:
            raise TwoGISError("Не удалось найти подпись запросов в бандле 2GIS")

        TwoGISApi._creds = (key_m.group(1), secret_m.group(1), time.time())
        return key_m.group(1), secret_m.group(1)

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
                async with self.session.get(_API + path, params=p, headers=headers) as r:
                    data = await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
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
        if k in TwoGISApi._regions:
            return TwoGISApi._regions[k]
        data = await self._call(
            "/2.0/region/search", {"q": city, "fields": "items.code"}, signed=False
        )
        items = (data.get("result") or {}).get("items") or []
        found = ("", "", city)
        if items and str(items[0].get("id", "0")) != "0":
            it = items[0]
            found = (str(it["id"]), it.get("code", ""), it.get("name", city))
        TwoGISApi._regions[k] = found
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

    phones, emails, sites, socials = [], [], [], []
    for group in item.get("contact_groups") or []:
        for c in group.get("contacts") or []:
            ctype, value = c.get("type", ""), c.get("value", "")
            if ctype == "phone" and value not in phones:
                phones.append(value)
            elif ctype == "email" and value not in emails:
                emails.append(value)
            elif ctype == "website":
                url = c.get("url") or value.split("?", 1)[-1]
                if is_social_url(url):
                    url, bucket = clean_social(url), socials
                else:
                    url, bucket = clean_url(url), sites
                if url not in bucket:
                    bucket.append(url)
            elif ctype in _MESSENGER_TYPES:
                link = clean_social(c.get("url") or value)
                if link and link not in socials:
                    socials.append(link)

    reviews = item.get("reviews") or {}
    org_info = item.get("org") or {}
    rubrics = [r.get("name", "") for r in item.get("rubrics") or [] if r.get("kind") == "primary"]
    rating = float(reviews.get("general_rating") or 0)
    review_count = int(reviews.get("general_review_count") or 0)
    branches = int(org_info.get("branch_count") or 0)
    phone = ", ".join(phones)

    org = Organization(
        id=firm_id,
        name=f"{name}, {ext}" if ext and ext.lower() not in name.lower() else name,
        phone=phone,
        email=", ".join(emails),
        website=", ".join(sites),
        address=item.get("address_name", ""),
        rating=rating,
        socials=", ".join(socials),
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
