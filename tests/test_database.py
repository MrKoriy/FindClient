"""Tests for db/database.py -- SQLite persistence and dedup."""

import pytest

from db.database import Database


def org(org_id: str, name: str = "A", **extra) -> dict:
    """Организация с пустыми полями: длинные dict-литералы в тестах не нужны."""
    base = {
        "id": org_id, "name": name, "phone": "", "email": "", "website": "",
        "address": "", "rating": 0, "socials": "",
    }
    base.update(extra)
    return base


class TestDatabase:

    @pytest.mark.asyncio
    async def test_save_and_retrieve_history(self, db):
        orgs = [
            org("1", "Org A", phone="+7 111", rating=4.0),
            org("2", "Org B", email="b@b.com", rating=3.0),
        ]
        session_id = await db.save_session("рестораны", orgs)
        assert session_id is not None

        history = await db.get_history()
        assert len(history) == 1
        assert history[0]["niche"] == "рестораны"
        assert history[0]["count"] == 2

    @pytest.mark.asyncio
    async def test_known_org_ids_dedup(self, db):
        await db.save_session("кафе", [org("100", "Test")])

        known = await db.get_known_org_ids("кафе")
        assert "100" in known

    @pytest.mark.asyncio
    async def test_known_ids_scoped_by_niche(self, db):
        await db.save_session("кафе", [org("1")])
        await db.save_session("авто", [org("2", "B")])

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
        await db1.save_session("тест", [org("x", "X")])
        await db1.close()

        db2 = Database(path=path)
        await db2.connect()
        known = await db2.get_known_org_ids("тест")
        assert "x" in known
        await db2.close()
