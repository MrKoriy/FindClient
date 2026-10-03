"""Общий дедуп первого контакта между рассылками бота и CRM-панелью."""

import sqlite3

import pytest

from crm import db as crm_db
from crm import sender
from services.recipient_guard import contacted_by_bot, contacted_by_panel


def _bot_db(path, status="sent", stop=False):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE crm_leads (id INTEGER PRIMARY KEY, tg_username TEXT, status TEXT);
        CREATE TABLE stoplist (key TEXT PRIMARY KEY, reason TEXT);
    """)
    conn.execute("INSERT INTO crm_leads (tg_username, status) VALUES ('ivan', ?)", (status,))
    if stop:
        conn.execute("INSERT INTO stoplist (key, reason) VALUES ('u:petr', 'просил не писать')")
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_bot_side_detection(tmp_path):
    path = str(tmp_path / "scraper.db")
    _bot_db(path, stop=True)
    assert await contacted_by_bot("@Ivan", path)
    assert await contacted_by_bot("petr", path)
    assert not await contacted_by_bot("nobody", path)
    assert not await contacted_by_bot("ivan", str(tmp_path / "missing.db"))


@pytest.mark.asyncio
async def test_panel_side_detection(tmp_path):
    path = str(tmp_path / "crm.db")
    await crm_db.init_db(path)
    mid = await crm_db.queue_message(None, "@Maria", "текст", crm_db=path)
    assert not await contacted_by_panel("maria", path), "только в очереди - ещё не писали"
    await crm_db.mark_message(mid, "sent", crm_db=path)
    assert await contacted_by_panel("maria", path)


@pytest.mark.asyncio
async def test_panel_worker_skips_people_the_bot_already_wrote(tmp_path, monkeypatch):
    bot_path = str(tmp_path / "scraper.db")
    _bot_db(bot_path)
    monkeypatch.setattr(crm_db, "DEFAULT_SCRAPER_DB", bot_path)
    path = str(tmp_path / "crm.db")
    await crm_db.init_db(path)
    await crm_db.set_settings({"enabled": "1", "dry_run": "0", "work_from": "0", "work_to": "24",
                               "min_delay": "0", "max_delay": "0"}, path)
    await crm_db.queue_message(None, "ivan", "текст", crm_db=path)

    class Client:
        sent = []

        async def get_entity(self, name):
            return name

        async def send_message(self, entity, body):
            self.sent.append(entity)

    client = Client()
    assert await sender.run_once(client, path) is True
    assert client.sent == []
    assert (await crm_db.list_messages(crm_db=path))[0]["status"] == "skipped"
