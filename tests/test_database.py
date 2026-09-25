"""Tests for db/database.py -- SQLite persistence and dedup."""

import pytest
import pytest_asyncio

from db.database import Database


@pytest_asyncio.fixture
async def db(tmp_path):
    """Create a temporary database for each test."""
    d = Database(path=str(tmp_path / "test.db"))
    await d.connect()
    yield d
    await d.close()


class TestDatabase:

    @pytest.mark.asyncio
    async def test_save_and_retrieve_history(self, db):
        orgs = [
            {"id": "1", "name": "Org A", "phone": "+7 111", "email": "", "website": "", "address": "", "rating": 4.0, "socials": ""},
            {"id": "2", "name": "Org B", "phone": "", "email": "b@b.com", "website": "", "address": "", "rating": 3.0, "socials": ""},
        ]
        session_id = await db.save_session("рестораны", orgs)
        assert session_id is not None

        history = await db.get_history()
        assert len(history) == 1
        assert history[0]["niche"] == "рестораны"
        assert history[0]["count"] == 2

    @pytest.mark.asyncio
    async def test_known_org_ids_dedup(self, db):
        orgs = [{"id": "100", "name": "Test", "phone": "", "email": "", "website": "", "address": "", "rating": 0, "socials": ""}]
        await db.save_session("кафе", orgs)

        known = await db.get_known_org_ids("кафе")
        assert "100" in known

    @pytest.mark.asyncio
    async def test_known_ids_scoped_by_niche(self, db):
        await db.save_session("кафе", [{"id": "1", "name": "A", "phone": "", "email": "", "website": "", "address": "", "rating": 0, "socials": ""}])
        await db.save_session("авто", [{"id": "2", "name": "B", "phone": "", "email": "", "website": "", "address": "", "rating": 0, "socials": ""}])

        cafe_ids = await db.get_known_org_ids("кафе")
        auto_ids = await db.get_known_org_ids("авто")

        assert cafe_ids == {"1"}
        assert auto_ids == {"2"}

    @pytest.mark.asyncio
    async def test_empty_history(self, db):
        history = await db.get_history()
        assert history == []

    @pytest.mark.asyncio
    async def test_empty_known_ids(self, db):
        known = await db.get_known_org_ids("ничего")
        assert known == set()

    @pytest.mark.asyncio
    async def test_database_persists(self, tmp_path):
        path = str(tmp_path / "persist.db")
        db1 = Database(path=path)
        await db1.connect()
        await db1.save_session("тест", [{"id": "x", "name": "X", "phone": "", "email": "", "website": "", "address": "", "rating": 0, "socials": ""}])
        await db1.close()

        db2 = Database(path=path)
        await db2.connect()
        known = await db2.get_known_org_ids("тест")
        assert "x" in known
        await db2.close()
