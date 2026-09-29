"""Company search orchestration: sources (2GIS, Yandex) -> merge -> filter -> dedup -> save -> table."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import aiohttp

from api.common import is_mobile_phone, messenger_link, mobile_numbers, name_key, phone_key
from api.twogis_api import TwoGISApi
from api.twogis_client import TwoGISClient
from api.yandex_client import YandexMapsClient
from db.database import Database
from models.organization import Organization
from services.export import ORG_COLUMNS, export_async, to_csv

log = logging.getLogger(__name__)

SOURCE_LABELS = {"2gis": "2GIS", "yandex": "Яндекс Карты", "avito": "Avito"}

# Мёртвые точки выкидываем по отзывам: 0-1 отзыв = карточка-зомби. В Москве
# конкуренция и плотность выше, планка выше.
_MOSCOW = {"москва", "moscow", "msk"}


def min_reviews_for(city: str) -> int:
    return 4 if (city or "").strip().lower().replace("ё", "е") in _MOSCOW else 2

Progress = Callable[[str], Awaitable[None]]


@dataclass
class ScrapeRequest:
    queries: tuple[str, ...]
    city: str = "Москва"
    count: int = 50
    sources: tuple[str, ...] = ("2gis", "yandex")
    only_without_site: bool = False
    only_with_phone: bool = False
    # ФИО руководителя из ЕГРЮЛ: +1-2 запроса на компанию, поэтому опция.
    enrich_egrul: bool = False
    check_site: bool = False  # HEAD+GET проверка website на парковку/заглушку
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
        parts.append(f"отзывов ≥ {min_reviews_for(self.city)}")
        parts.append("мобильный или мессенджер")
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

    async def file(self, fmt: str = "xlsx") -> tuple[bytes, str]:
        """Экспорт уходит в поток, чтобы не блокировать цикл на больших сборах."""
        return await export_async(self.organizations, ORG_COLUMNS, fmt=fmt)


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
        pk = phone_key(org.phone)
        nm_key = f"nm:{name_key(org.name, org.address.split(',')[0])}"
        # ищем по id и phone — всегда надёжно
        existing = by_key.get(f"id:{org.id}")
        if not existing and pk:
            existing = by_key.get(f"ph:{pk}")
        if not existing:
            # по имени — только если телефон тоже совпадает (или один из них пустой)
            cand = by_key.get(nm_key)
            if cand:
                cand_pk = phone_key(cand.phone)
                if not pk or not cand_pk or pk == cand_pk:
                    existing = cand
        if existing:
            _merge_into(existing, org)
            merged += 1
            target = existing
        else:
            unique.append(org)
            target = org
        # регистрируем ключи
        by_key[f"id:{org.id}"] = target
        if pk:
            by_key[f"ph:{pk}"] = target
        # nm ключ — только если ещё не занят (не перетираем чужой)
        by_key.setdefault(nm_key, target)
    return unique, merged


Searcher = Callable[..., Awaitable[list[Organization]]]


class ScrapeService:
    """Runs a search across map sources with fallbacks, so one failing source never stops the job.

    Поисковики можно передать снаружи (searchers=...) - тестам не нужен
    patch.object, а источники собираются параллельно: 2GIS и Яндекс не
    зависят друг от друга.
    """

    def __init__(
        self,
        db: Database,
        request_delay: float = 0.3,
        yandex_api_key: str = "",
        proxy: str = "",
        proxy_pool: list[str] | None = None,
        bbox_split: bool = True,
        searchers: dict[str, Searcher] | None = None,
    ) -> None:
        self.db = db
        self.request_delay = request_delay
        self.yandex_api_key = yandex_api_key
        self.proxy = proxy
        self.proxy_pool = proxy_pool or []
        self.bbox_split = bbox_split
        # Ключи 2GIS лежат рядом с базой: рестарт сохраняет рабочий ключ.
        self.creds_path = Path(db.path).parent / "twogis_creds.json"
        self.searchers = searchers or {  # noqa: E501
            "2gis": self._search_2gis, "yandex": self._search_yandex, "avito": self._search_avito,
        }

    async def _search_avito(self, session, query, req, need, skip, progress) -> list[Organization]:
        from api.avito_client import AvitoClient

        client = AvitoClient(  # noqa: E501
            session=session, request_delay=self.request_delay, proxy=self.proxy, proxy_pool=self.proxy_pool,
        )

        async def on_page(done: int, total: int) -> None:
            await progress(f"Avito «{query}»: {done}/{total}")

        return await client.search(query, req.city, need, skip_ids=skip, on_progress=on_page)

    async def _search_2gis(self, session, query, req, need, skip, progress) -> list[Organization]:
        api = TwoGISApi(
            session, request_delay=self.request_delay, proxy=self.proxy, creds_path=self.creds_path,
        )

        async def on_page(done: int, total: int) -> None:
            await progress(f"2GIS «{query}»: {done}/{total}")

        try:
            try:
                return await api.search(
                    query, req.city, need, only_without_site=req.only_without_site,
                    skip_ids=skip, on_progress=on_page,
                )
            except (TimeoutError, aiohttp.ClientConnectionError):
                raise  # 2gis.ru itself is unreachable — page scraping would only wait longer
            except Exception as exc:
                log.warning("2GIS API failed, falling back to web pages: %s", exc)

            # Fallback: page scraping (slower; 1 request per company).
            region_id, slug, city_name = "", "moscow", req.city
            try:
                region_id, slug, city_name = await api.resolve_city(req.city)
            except Exception as exc:
                log.warning("не удалось определить регион «%s», ищем как в Москве: %s", req.city, exc)
            client = TwoGISClient(
                session=session, request_delay=self.request_delay,
                city_slug=slug or "moscow", city_name=city_name,
            )

            async def on_firm(done: int, total: int) -> None:
                await progress(f"2GIS (веб) «{query}»: карточки {done}/{total}")

            orgs = await client.search(
                query if region_id else f"{query} {req.city}", need * 2,
                on_progress=on_firm, only_without_site=req.only_without_site, skip_ids=skip,
            )
            return orgs[:need]
        finally:
            # The API may have opened its own session for the DDoS-Guard edge.
            await api.close()

    async def _search_yandex(self, session, query, req, need, skip, progress) -> list[Organization]:
        client = YandexMapsClient(
            session, api_key=self.yandex_api_key, proxy=self.proxy,
            proxy_pool=self.proxy_pool, bbox_split=self.bbox_split,
        )

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

        async def collect(source: str) -> tuple[list[Organization], str | None]:
            """Один источник: все запросы параллельно, исключение одного не роняет остальные."""
            search = self.searchers.get(source)
            if not search:
                return [], None
            # Параллельно: каждый query ищет overshoot=count (без меж-запросного дедупа).
            # Дубликаты схлопнет merge_organizations + обрезка до count — быстрее
            # чем последовательно с need=count-len(got) и skip_ids между queries.
            async def one_query(query: str) -> tuple[list[Organization], str | None]:
                try:
                    orgs = await search(session, query, req, req.count, known, progress)
                    return orgs, None
                except Exception as exc:
                    log.exception("%s search failed (query=%s)", source, query)
                    return [], f"{SOURCE_LABELS.get(source, source)} ({query}): {exc}"

            per_query = await asyncio.gather(*(one_query(q) for q in req.queries))
            got: list[Organization] = []
            errs: list[str] = []
            for orgs, err in per_query:  # type: ignore[misc]
                got.extend(orgs)
                if err:
                    errs.append(err)
            combined = "; ".join(errs) if errs else None
            return got, combined

        timeout = aiohttp.ClientTimeout(total=60, sock_connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            results = await asyncio.gather(*(collect(s) for s in req.sources), return_exceptions=True)
        for source, result in zip(req.sources, results, strict=True):
            if isinstance(result, BaseException):
                log.exception("%s gather failed", source)
                errors.append(f"{SOURCE_LABELS.get(source, source)}: {result}")
                per_source[source] = 0
                continue
            got, err = result  # type: ignore[misc]
            per_source[source] = len(got)
            found += got
            if err:
                errors.append(err)

        # Yandex keyless fallback without curl_cffi is limited to 25 results — warn user
        if "yandex" in req.sources and not self.yandex_api_key:
            try:
                from api.yandex_client import CurlSession as _YandexCurl  # type: ignore[import]

                if _YandexCurl is None:
                    warn = "Яндекс Карты: curl_cffi не установлен — ограничен первой страницей (25 результатов)"
                    if warn not in errors:
                        log.warning(warn)
                        errors.append(warn)
            except Exception:
                pass

        total_scraped = len(found)
        unique, merged = merge_organizations(found)
        known_phones = await self.db.get_known_phone_keys(req.niche, req.city)
        fresh = [o for o in unique if o.id not in known and phone_key(o.phone) not in known_phones]
        if req.only_without_site:
            fresh = [o for o in fresh if not o.has_website]

        # Качество лида: Avito проходит без фильтров (там нет отзывов/рейтинга).
        def _is_avito(o: Organization) -> bool:
            return (o.source or "").split("+")[0] == "avito" or o.id.startswith("avito:")

        min_reviews = min_reviews_for(req.city)
        fresh = [o for o in fresh if _is_avito(o) or o.reviews >= min_reviews]
        fresh = [  # noqa: E501
            o for o in fresh if _is_avito(o) or is_mobile_phone(o.phone) or messenger_link(o.socials)
        ]
        # В колонке «Телефон» остаются только мобильные: городские и 8-800
        # для мессенджер-охоты бесполезны.
        for o in fresh:
            o.phone = mobile_numbers(o.phone)
        if req.only_with_phone:
            fresh = [o for o in fresh if o.phone or messenger_link(o.socials)]

        # Мессенджер в карточке - метка хозяина: приоритет и в скоринге, и сверху списка.
        for o in fresh:
            if messenger_link(o.socials):
                o.score = min(100, o.score + 10)
        fresh.sort(key=lambda o: (not messenger_link(o.socials), -o.score))
        fresh = fresh[: req.count]

        if req.enrich_egrul:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await self._enrich_directors(session, fresh, progress)

        # Optional site check: не блокирует выдачу, только отфильтровывает
        # парковки если check_site указан в запросе (или env)
        if getattr(req, "check_site", False) and fresh:
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    await self._enrich_site_check(session, fresh, progress)
            except Exception as exc:
                log.debug("site-check skipped: %s", exc)

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

    async def _enrich_directors(self, session, orgs, progress) -> None:
        """ФИО руководителя из ЕГРЮЛ/ЕГРИП по имени из карточки.

        Мягкий провал: сервис молчит или совпадения нет - колонка ЛПР просто
        остаётся пустой, сбор от этого не ломается. Четыре воркера, чтобы
        очередь в 50 компаний не растягивалась на минуты.
        """
        from api.egrul import EgrulClient

        client = EgrulClient(session)
        sem = asyncio.Semaphore(4)
        done = 0

        async def one(org: Organization) -> None:
            nonlocal done
            async with sem:
                try:
                    info = await client.find_director(org.name, org.city)
                except Exception as exc:
                    log.debug("ЕГРЮЛ «%s»: %s", org.name, exc)
                    info = None
            if info:
                org.director = f"{info['director']} ({info['position']})"
                org.inn = info["inn"]
            done += 1
            if progress and (done % 10 == 0 or done == len(orgs)):
                await progress(f"ЕГРЮЛ: руководители {done}/{len(orgs)}")

        await asyncio.gather(*(one(o) for o in orgs))

    async def _enrich_site_check(self, session, orgs, progress) -> None:
        """HEAD+GET проверка website, парковки/заглушки помечаются.

        Опционально, включается флагом ScrapeRequest.check_site. Мёртвые сайты
        не выкидываем из выдачи — только логируем; фильтрацию можно добавить
        позже когда наберём статистику.
        """
        from api.common import check_site_alive

        sem = asyncio.Semaphore(10)
        done = 0

        async def one(org: Organization) -> None:
            nonlocal done
            if not org.website:
                done += 1
                return
            async with sem:
                try:
                    alive = await check_site_alive(org.website, session)
                except Exception as exc:
                    log.debug("site-check «%s»: %s", org.website, exc)
                    alive = True  # fail-open
            if not alive:
                log.info("site-check: парковка/недоступен %s (%s)", org.website, org.name)
            done += 1
            if progress and (done % 10 == 0 or done == len(orgs)):
                await progress(f"Сайты: проверено {done}/{len(orgs)}")

        await asyncio.gather(*(one(o) for o in orgs))

