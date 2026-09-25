"""Tests for services/scrape_service.py -- multi-source orchestration, merge, filters, dedup."""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from db.database import Database
from models.organization import Organization
from services.scrape_service import ScrapeRequest, ScrapeService, merge_organizations


@pytest_asyncio.fixture
async def db(tmp_path):
    d = Database(path=str(tmp_path / "test.db"))
    await d.connect()
    yield d
    await d.close()


def _org(i, **kw) -> Organization:
    base = dict(id=str(i), name=f"Org {i}", address=f"ул. {i}", phone=f"+7900{i:07d}", score=i)
    base.update(kw)
    return Organization(**base)


def _patch_sources(two=None, ya=None):
    """Patch per-source searchers; a value may be a list (result) or an Exception."""
    def mk(v):
        async def fake(self, session, query, req, need, skip, progress):
            if isinstance(v, Exception):
                raise v
            return [o for o in (v or []) if o.id not in skip][:need]
        return fake
    return (
        patch.object(ScrapeService, "_search_2gis", mk(two)),
        patch.object(ScrapeService, "_search_yandex", mk(ya)),
    )


async def _run(db, req, two=None, ya=None):
    p1, p2 = _patch_sources(two, ya)
    with p1, p2:
        return await ScrapeService(db, request_delay=0).scrape(req)


class TestMerge:

    def test_same_phone_merged_and_fields_filled(self):
        a = _org(1, phone="+7 (495) 111-22-33", website="")
        b = _org(2, name="Другое имя", phone="84951112233", email="a@b.ru", source="yandex", reviews=40)
        unique, merged = merge_organizations([a, b])
        assert len(unique) == 1 and merged == 1
        assert unique[0].email == "a@b.ru"
        assert unique[0].reviews == 40
        assert unique[0].source == "2gis+yandex"

    def test_same_name_address_merged(self):
        a = _org(1, phone="", name="Ромашка", address="Тверская, 1")
        b = _org(2, phone="", name="РОМАШКА", address="Тверская, 1, этаж 2", source="yandex")
        unique, _ = merge_organizations([a, b])
        assert len(unique) == 1

    def test_different_companies_kept(self):
        unique, merged = merge_organizations([_org(1), _org(2)])
        assert len(unique) == 2 and merged == 0


class TestScrapeService:

    @pytest.mark.asyncio
    async def test_combines_sources_and_sorts_by_score(self, db):
        req = ScrapeRequest(queries=("кафе",), count=10)
        r = await _run(db, req, two=[_org(1), _org(2)], ya=[_org(3, id="ya3", source="yandex")])
        assert [o.id for o in r.organizations] == ["ya3", "2", "1"]
        assert r.per_source == {"2gis": 2, "yandex": 1}

    @pytest.mark.asyncio
    async def test_saves_and_dedups_next_run(self, db):
        req = ScrapeRequest(queries=("кафе",), city="Казань", count=10)
        await _run(db, req, two=[_org(1), _org(2)])
        assert await db.get_known_org_ids("кафе", "Казань") == {"1", "2"}

        r2 = await _run(db, req, two=[_org(1), _org(2), _org(3)])
        assert [o.id for o in r2.organizations] == ["3"]
        assert r2.already_in_db == 2

    @pytest.mark.asyncio
    async def test_same_company_from_other_source_is_known_by_phone(self, db):
        req = ScrapeRequest(queries=("кафе",), count=10)
        await _run(db, req, two=[_org(1)])
        r = await _run(db, req, ya=[_org(9, id="ya9", phone=_org(1).phone, source="yandex")])
        assert r.organizations == []

    @pytest.mark.asyncio
    async def test_dedup_scoped_by_city(self, db):
        await _run(db, ScrapeRequest(queries=("кафе",), city="Москва"), two=[_org(1)])
        r = await _run(db, ScrapeRequest(queries=("кафе",), city="Казань"), two=[_org(1)])
        assert len(r.organizations) == 1

    @pytest.mark.asyncio
    async def test_filters_without_site_and_phone(self, db):
        orgs = [_org(1, website="a.ru"), _org(2), _org(3, phone="")]
        req = ScrapeRequest(queries=("кафе",), only_without_site=True, only_with_phone=True)
        r = await _run(db, req, two=orgs)
        assert [o.id for o in r.organizations] == ["2"]

    @pytest.mark.asyncio
    async def test_failing_source_does_not_stop_others(self, db):
        req = ScrapeRequest(queries=("кафе",))
        r = await _run(db, req, two=RuntimeError("blocked"), ya=[_org(5, id="ya5", source="yandex")])
        assert [o.id for o in r.organizations] == ["ya5"]
        assert r.errors and "blocked" in r.errors[0]

    @pytest.mark.asyncio
    async def test_count_limit_and_multiple_queries(self, db):
        req = ScrapeRequest(queries=("кафе", "кофейня"), count=3, sources=("2gis",))
        r = await _run(db, req, two=[_org(i) for i in range(10)])
        assert len(r.organizations) == 3

    @pytest.mark.asyncio
    async def test_empty_result_not_saved(self, db):
        r = await _run(db, ScrapeRequest(queries=("пусто",)), two=[], ya=[])
        assert r.organizations == []
        assert await db.get_history() == []

    @pytest.mark.asyncio
    async def test_result_file_formats(self, db):
        r = await _run(db, ScrapeRequest(queries=("кафе",)), two=[_org(1)])
        data, ext = r.file("xlsx")
        assert ext == "xlsx" and data[:2] == b"PK"
        data, ext = r.file("csv")
        assert ext == "csv" and data[:3] == b"\xef\xbb\xbf"
