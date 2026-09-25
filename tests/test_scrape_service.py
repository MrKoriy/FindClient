"""Tests for services/scrape_service.py -- orchestration, dedup, and CSV."""

import csv
import io
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from db.database import Database
from models.organization import Organization
from services.scrape_service import ScrapeService, _generate_csv


@pytest_asyncio.fixture
async def db(tmp_path):
    d = Database(path=str(tmp_path / "test.db"))
    await d.connect()
    yield d
    await d.close()


def _make_orgs(count: int, start: int = 0) -> list[Organization]:
    return [
        Organization(
            id=str(i),
            name=f"Org {i}",
            phone=f"+7{i:010d}" if i % 2 == 0 else "",
            email=f"org{i}@example.com" if i % 3 == 0 else "",
            website=f"org{i}.ru" if i % 4 == 0 else "",
            address=f"Addr {i}",
            rating=float(i % 5),
        )
        for i in range(start, start + count)
    ]


class TestGenerateCSV:

    def test_csv_has_russian_headers(self):
        orgs = [Organization(id="1", name="Test")]
        data = _generate_csv(orgs)
        text = data.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))
        header = next(reader)
        assert "Название" in header
        assert "Телефон" in header
        assert "Email" in header

    def test_csv_has_correct_data(self):
        orgs = [Organization(id="1", name="Org A", phone="+71111", email="a@b.com")]
        data = _generate_csv(orgs)
        text = data.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))
        next(reader)  # skip header
        row = next(reader)
        assert "Org A" in row
        assert "+71111" in row
        assert "a@b.com" in row

    def test_csv_empty_fields_are_empty_strings(self):
        orgs = [Organization(id="1", name="X")]
        data = _generate_csv(orgs)
        text = data.decode("utf-8-sig")
        assert "None" not in text
        assert "null" not in text

    def test_csv_utf8sig_bom(self):
        orgs = [Organization(id="1", name="Тест")]
        data = _generate_csv(orgs)
        # UTF-8 BOM
        assert data[:3] == b"\xef\xbb\xbf"

    def test_csv_empty_list(self):
        data = _generate_csv([])
        text = data.decode("utf-8-sig")
        lines = text.strip().split("\n")
        assert len(lines) == 1  # header only


def _mock_scrape_context(orgs):
    """Context manager that patches aiohttp + TwoGISClient to return given orgs."""
    mock_session_cls = patch("services.scrape_service.aiohttp.ClientSession")
    mock_client_cls = patch("services.scrape_service.TwoGISClient")

    class _Ctx:
        def __enter__(self):
            self.s = mock_session_cls.__enter__()
            self.c = mock_client_cls.__enter__()
            ms = AsyncMock()
            self.s.return_value.__aenter__ = AsyncMock(return_value=ms)
            self.s.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client = AsyncMock()
            mock_client.search.return_value = orgs
            self.c.return_value = mock_client
            return self

        def __exit__(self, *args):
            mock_client_cls.__exit__(*args)
            mock_session_cls.__exit__(*args)

    return _Ctx()


class TestScrapeService:

    @pytest.mark.asyncio
    async def test_scrape_returns_results_with_csv(self, db):
        orgs = _make_orgs(3)
        service = ScrapeService(db=db, request_delay=0)

        with _mock_scrape_context(orgs):
            result = await service.scrape("кафе", count=3)

        assert len(result.organizations) == 3
        assert result.csv_bytes
        assert result.total_scraped == 3
        assert result.duplicates_removed == 0
        assert result.already_in_db == 0

    @pytest.mark.asyncio
    async def test_scrape_saves_to_db(self, db):
        orgs = _make_orgs(2)
        service = ScrapeService(db=db, request_delay=0)

        with _mock_scrape_context(orgs):
            await service.scrape("кафе", count=2)

        known = await db.get_known_org_ids("кафе")
        assert "0" in known
        assert "1" in known

    @pytest.mark.asyncio
    async def test_scrape_result_stats(self, db):
        orgs = _make_orgs(4)
        service = ScrapeService(db=db, request_delay=0)

        with _mock_scrape_context(orgs):
            result = await service.scrape("кафе", count=4)

        # Even IDs (0, 2) have phones
        assert result.with_phone == 2
        # IDs divisible by 3 (0, 3) have emails
        assert result.with_email == 2
        # IDs divisible by 4 (0) have websites
        assert result.with_website == 1

    @pytest.mark.asyncio
    async def test_dedup_filters_known_orgs(self, db):
        """Second scrape for same niche filters out already-known orgs."""
        # First scrape: save orgs 0-2
        first_orgs = _make_orgs(3)
        service = ScrapeService(db=db, request_delay=0)
        with _mock_scrape_context(first_orgs):
            r1 = await service.scrape("кафе", count=3)
        assert len(r1.organizations) == 3
        assert r1.duplicates_removed == 0

        # Second scrape: API returns orgs 0-4 (3 known + 2 new)
        all_orgs = _make_orgs(5)
        with _mock_scrape_context(all_orgs):
            r2 = await service.scrape("кафе", count=3)

        # Should only contain new orgs (3, 4)
        new_ids = {o.id for o in r2.organizations}
        assert "0" not in new_ids
        assert "1" not in new_ids
        assert "2" not in new_ids
        assert "3" in new_ids
        assert "4" in new_ids
        assert r2.duplicates_removed == 3
        assert r2.total_scraped == 5
        assert r2.already_in_db == 3

    @pytest.mark.asyncio
    async def test_start_page_skips_known(self, db):
        """With many known orgs, search starts from a later page."""
        # Seed DB with 50 known orgs for this niche.
        orgs_50 = _make_orgs(50)
        service = ScrapeService(db=db, request_delay=0)
        with _mock_scrape_context(orgs_50):
            await service.scrape("кафе", count=50)

        # Second scrape — verify start_page > 1 is passed to search.
        new_orgs = _make_orgs(10, start=50)
        with _mock_scrape_context(new_orgs) as ctx:
            await service.scrape("кафе", count=10)
            # start_page should be 50 // 12 = 4 (skip first 3 pages)
            call_kwargs = ctx.c.return_value.search.call_args
            assert call_kwargs.kwargs.get("start_page", call_kwargs[1].get("start_page", 1)) >= 4

    @pytest.mark.asyncio
    async def test_dedup_different_niches_independent(self, db):
        """Orgs from niche A don't affect dedup for niche B."""
        orgs = _make_orgs(2)
        service = ScrapeService(db=db, request_delay=0)

        with _mock_scrape_context(orgs):
            await service.scrape("кафе", count=2)

        # Same orgs but different niche — no dedup
        with _mock_scrape_context(orgs):
            r = await service.scrape("рестораны", count=2)

        assert len(r.organizations) == 2
        assert r.duplicates_removed == 0

    @pytest.mark.asyncio
    async def test_dedup_saves_only_new(self, db):
        """Only new (non-duplicate) orgs are saved to DB."""
        first_orgs = _make_orgs(2)
        service = ScrapeService(db=db, request_delay=0)
        with _mock_scrape_context(first_orgs):
            await service.scrape("кафе", count=2)

        # Return orgs 0-3: 0,1 are known, 2,3 are new
        all_orgs = _make_orgs(4)
        with _mock_scrape_context(all_orgs):
            await service.scrape("кафе", count=2)

        known = await db.get_known_org_ids("кафе")
        assert known == {"0", "1", "2", "3"}

    @pytest.mark.asyncio
    async def test_empty_scrape_no_db_save(self, db):
        """Empty result doesn't create DB records."""
        service = ScrapeService(db=db, request_delay=0)
        with _mock_scrape_context([]):
            result = await service.scrape("пусто", count=10)

        assert len(result.organizations) == 0
        assert result.total_scraped == 0
        assert result.duplicates_removed == 0
        assert result.already_in_db == 0
        known = await db.get_known_org_ids("пусто")
        assert len(known) == 0

    @pytest.mark.asyncio
    async def test_exhausted_niche_reports_already_in_db(self, db):
        """When all results are already known, already_in_db is set."""
        orgs = _make_orgs(5)
        service = ScrapeService(db=db, request_delay=0)
        with _mock_scrape_context(orgs):
            await service.scrape("кафе", count=5)

        # Same orgs returned — all known, 0 new
        with _mock_scrape_context(orgs):
            r = await service.scrape("кафе", count=5)

        assert len(r.organizations) == 0
        assert r.already_in_db == 5
