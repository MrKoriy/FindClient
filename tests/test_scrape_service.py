"""Tests for services/scrape_service.py -- multi-source orchestration, merge, filters, dedup."""

from unittest.mock import patch

import pytest

from models.organization import Organization
from services.scrape_service import ScrapeRequest, ScrapeService, merge_organizations


def _org(i, **kw) -> Organization:
    # reviews=5 и мобильный телефон по умолчанию: бизнес-фильтр качества
    # (отзывы >= 2/4, мобильный или мессенджер) в тестах не должен мешать.
    base = dict(
        id=str(i), name=f"Org {i}", address=f"ул. {i}",
        phone=f"+7900{i:07d}", score=i, reviews=5,
    )
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
        data, ext = await r.file("xlsx")
        assert ext == "xlsx" and data[:2] == b"PK"
        data, ext = await r.file("csv")
        assert ext == "csv" and data[:3] == b"\xef\xbb\xbf"


    @pytest.mark.asyncio
    async def test_quality_filters_drop_dead_points(self, db):
        """Отзывов < 2 и точки без мобильного/мессенджера - мусор для охоты."""
        orgs = [
            _org(1, reviews=0),                 # мёртвая карточка
            _org(2, reviews=1),                 # почти мёртвая
            _org(3, phone="+7 495 111-22-33"),  # только городской
            _org(4, phone="88005553535"),       # колл-центр
        ]
        r = await _run(db, ScrapeRequest(queries=("кафе",), city="Казань"), two=orgs)
        assert r.organizations == []

    @pytest.mark.asyncio
    async def test_moscow_has_higher_reviews_bar(self, db):
        dead_in_msk = _org(1, reviews=3)
        r = await _run(db, ScrapeRequest(queries=("кафе",), city="Москва"), two=[dead_in_msk])
        assert r.organizations == []

        alive_in_kzn = _org(1, reviews=3)
        r = await _run(db, ScrapeRequest(queries=("кафе",), city="Казань"), two=[alive_in_kzn])
        assert [o.id for o in r.organizations] == ["1"]

    @pytest.mark.asyncio
    async def test_messenger_survives_without_phone_and_tops_list(self, db):
        from api.common import messenger_link
        orgs = [
            _org(1), _org(2, phone="", socials="https://t.me/owner"),
        ]
        r = await _run(db, ScrapeRequest(queries=("кафе",)), two=orgs)
        assert [o.id for o in r.organizations] == ["2", "1"]
        assert messenger_link(r.organizations[0].socials) == "https://t.me/owner"
        # Премия к скорингу за прямой канал до хозяина (база score=i).
        assert r.organizations[0].score == 12 and r.organizations[1].score == 1

    @pytest.mark.asyncio
    async def test_phone_column_keeps_only_mobiles(self, db):
        orgs = [_org(1, phone="+7 495 111-22-33, +7 900 111-22-33")]
        r = await _run(db, ScrapeRequest(queries=("кафе",)), two=orgs)
        assert r.organizations[0].phone == "+7 900 111-22-33"

    @pytest.mark.asyncio
    async def test_egrul_enrichment_fills_director(self, db, monkeypatch):
        from api import egrul

        async def fake_find(self, name, city=""):
            return {"director": "Иванов Пётр Сидорович", "position": "директор",
                    "inn": "1655000000", "company": 'ООО "ТЕСТ"'}

        monkeypatch.setattr(egrul.EgrulClient, "find_director", fake_find)
        req = ScrapeRequest(queries=("кафе",), city="Казань", enrich_egrul=True)
        r = await _run(db, req, two=[_org(1)])
        assert r.organizations[0].director == "Иванов Пётр Сидорович (директор)"
        assert r.organizations[0].inn == "1655000000"
