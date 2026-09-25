"""Company search orchestration: sources (2GIS, Yandex) -> merge -> filter -> dedup -> save -> table."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field

import aiohttp

from api.common import name_key, phone_key
from api.twogis_api import TwoGISApi
from api.twogis_client import TwoGISClient
from api.yandex_client import YandexMapsClient
from db.database import Database
from models.organization import Organization
from services.export import ORG_COLUMNS, export, to_csv

log = logging.getLogger(__name__)

SOURCE_LABELS = {"2gis": "2GIS", "yandex": "Яндекс Карты"}

Progress = Callable[[str], Awaitable[None]]


@dataclass
class ScrapeRequest:
    queries: tuple[str, ...]
    city: str = "Москва"
    count: int = 50
    sources: tuple[str, ...] = ("2gis", "yandex")
    only_without_site: bool = False
    only_with_phone: bool = False
    label: str = ""  # history key; defaults to the first query

    @property
    def niche(self) -> str:
        return self.label or self.queries[0]

    @property
    def filters(self) -> str:
        parts = []
        if self.only_without_site:
            parts.append("без сайта")
        if self.only_with_phone:
            parts.append("с телефоном")
        return ", ".join(parts)


@dataclass
class ScrapeResult:
    organizations: list[Organization]
    total_scraped: int = 0
    duplicates_removed: int = 0
    already_in_db: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    session_id: int = 0

    @property
    def with_phone(self) -> int:
        return sum(1 for o in self.organizations if o.phone)

    @property
    def with_email(self) -> int:
        return sum(1 for o in self.organizations if o.email)

    @property
    def with_website(self) -> int:
        return sum(1 for o in self.organizations if o.website)

    @property
    def without_website(self) -> int:
        return sum(1 for o in self.organizations if not o.website)

    @property
    def with_socials(self) -> int:
        return sum(1 for o in self.organizations if o.socials)

    @property
    def csv_bytes(self) -> bytes:
        return to_csv(self.organizations, ORG_COLUMNS)

    def file(self, fmt: str = "xlsx") -> tuple[bytes, str]:
        return export(self.organizations, ORG_COLUMNS, fmt=fmt)


def _merge_into(base: Organization, other: Organization) -> None:
    """Fill empty fields of base from the same company found in another source."""
    for f in ("phone", "email", "website", "address", "socials", "category"):
        if not getattr(base, f) and getattr(other, f):
            setattr(base, f, getattr(other, f))
    base.rating = base.rating or other.rating
    base.reviews = max(base.reviews, other.reviews)
    base.branches = max(base.branches, other.branches)
    base.score = max(base.score, other.score)
    if other.source not in base.source.split("+"):
        base.source = f"{base.source}+{other.source}"


def merge_organizations(orgs: list[Organization]) -> tuple[list[Organization], int]:
    """Deduplicate by id, phone and name+address. Returns (unique, merged_count)."""
    unique: list[Organization] = []
    by_key: dict[str, Organization] = {}
    merged = 0
    for org in orgs:
        keys = [f"id:{org.id}"]
        if pk := phone_key(org.phone):
            keys.append(f"ph:{pk}")
        keys.append(f"nm:{name_key(org.name, org.address.split(',')[0])}")
        existing = next((by_key[k] for k in keys if k in by_key), None)
        if existing:
            _merge_into(existing, org)
            merged += 1
            target = existing
        else:
            unique.append(org)
            target = org
        for k in keys:
            by_key.setdefault(k, target)
    return unique, merged


class ScrapeService:
    """Runs a search across map sources with fallbacks, so one failing source never stops the job."""

    def __init__(
        self,
        db: Database,
        request_delay: float = 0.3,
        yandex_api_key: str = "",
        proxy: str = "",
    ) -> None:
        self.db = db
        self.request_delay = request_delay
        self.yandex_api_key = yandex_api_key
        self.proxy = proxy

    async def _search_2gis(self, session, query, req, need, skip, progress) -> list[Organization]:
        api = TwoGISApi(session, request_delay=self.request_delay)

        async def on_page(done: int, total: int) -> None:
            await progress(f"2GIS «{query}»: {done}/{total}")

        try:
            return await api.search(
                query, req.city, need, only_without_site=req.only_without_site,
                skip_ids=skip, on_progress=on_page,
            )
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
            raise  # 2gis.ru itself is unreachable — page scraping would only wait longer
        except Exception as exc:
            log.warning("2GIS API failed, falling back to web pages: %s", exc)

        # Fallback: page scraping (slower; 1 request per company).
        region_id, slug, city_name = "", "moscow", req.city
        try:
            region_id, slug, city_name = await api.resolve_city(req.city)
        except Exception:
            pass
        client = TwoGISClient(
            session=session, request_delay=self.request_delay,
            city_slug=slug or "moscow", city_name=city_name,
        )

        async def on_firm(done: int, total: int) -> None:
            await progress(f"2GIS (веб) «{query}»: карточки {done}/{total}")

        orgs = await client.search(query if region_id else f"{query} {req.city}", need * 2, on_progress=on_firm)
        orgs = [o for o in orgs if o.id not in skip]
        if req.only_without_site:
            orgs = [o for o in orgs if not o.has_website]
        return orgs[:need]

    async def _search_yandex(self, session, query, req, need, skip, progress) -> list[Organization]:
        client = YandexMapsClient(session, api_key=self.yandex_api_key, proxy=self.proxy)

        async def on_page(done: int, total: int) -> None:
            await progress(f"Яндекс «{query}»: {done}/{total}")

        return await client.search(
            query, req.city, need, only_without_site=req.only_without_site,
            skip_ids=skip, on_progress=on_page,
        )

    async def scrape(self, req: ScrapeRequest, on_progress: Progress | None = None) -> ScrapeResult:
        async def progress(text: str) -> None:
            if on_progress:
                await on_progress(text)

        known = await self.db.get_known_org_ids(req.niche, req.city)
        found: list[Organization] = []
        per_source: dict[str, int] = {}
        errors: list[str] = []
        searchers = {"2gis": self._search_2gis, "yandex": self._search_yandex}

        timeout = aiohttp.ClientTimeout(total=60, sock_connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for source in req.sources:
                search = searchers.get(source)
                if not search:
                    continue
                got: list[Organization] = []
                for query in req.queries:
                    need = req.count - len(got)
                    if need <= 0:
                        break
                    skip = known | {o.id for o in got}
                    try:
                        got += await search(session, query, req, need, skip, progress)
                    except Exception as exc:
                        log.exception("%s search failed", source)
                        errors.append(f"{SOURCE_LABELS.get(source, source)}: {exc}")
                        break
                per_source[source] = len(got)
                found += got

        total_scraped = len(found)
        unique, merged = merge_organizations(found)
        known_phones = await self.db.get_known_phone_keys(req.niche, req.city)
        fresh = [o for o in unique if o.id not in known and phone_key(o.phone) not in known_phones]
        if req.only_without_site:
            fresh = [o for o in fresh if not o.has_website]
        if req.only_with_phone:
            fresh = [o for o in fresh if o.phone]
        fresh.sort(key=lambda o: o.score, reverse=True)
        fresh = fresh[: req.count]

        session_id = 0
        if fresh:
            session_id = await self.db.save_session(
                req.niche, [asdict(o) for o in fresh], city=req.city,
                sources=",".join(req.sources), filters=req.filters,
            )

        return ScrapeResult(
            organizations=fresh,
            total_scraped=total_scraped,
            duplicates_removed=total_scraped - len(fresh),
            already_in_db=len(known),
            per_source=per_source,
            errors=errors,
            session_id=session_id,
        )
