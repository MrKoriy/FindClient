"""Защиты воркера CRM: пауза после флуда в базе, временный лимит, аренда, drip без получателя."""

import time

import pytest
import pytest_asyncio
from telethon import errors

from crm import db as crm_db
from crm import sender


@pytest_asyncio.fixture
async def crm_path(tmp_path):
    path = str(tmp_path / "crm.db")
    await crm_db.init_db(path)
    await crm_db.set_settings(
        {"enabled": "1", "dry_run": "0", "work_from": "0", "work_to": "24",
         "daily_cap": "10", "min_delay": "0", "max_delay": "0"}, path
    )
    return path


class Client:
    def __init__(self, fail=None):
        self.fail = fail
        self.sent = []

    async def get_entity(self, name):
        return name

    async def send_message(self, entity, body):
        if self.fail:
            raise self.fail
        self.sent.append((entity, body))
        return type("M", (), {"id": 1})()


def _flood_wait(seconds):
    exc = errors.FloodWaitError(request=None, capture=seconds)
    exc.seconds = seconds
    return exc


@pytest.mark.asyncio
async def test_peer_flood_pauses_worker_and_survives_restart(crm_path):
    for i in range(3):
        await crm_db.queue_message(None, f"u{i}", "текст", crm_db=crm_path)
    assert await sender.run_once(Client(fail=errors.PeerFloodError(request=None)), crm_path) is True

    s = await crm_db.get_settings(crm_path)
    assert crm_db.blocked_until(s) > time.time() + 23 * 3600
    # «рестарт»: новый клиент, та же база - ничего не уходит
    ok = Client()
    assert await sender.run_once(ok, crm_path) is False
    assert ok.sent == []
    assert (await crm_db.summary(crm_path))["blocked_until"] > 0


@pytest.mark.asyncio
async def test_long_flood_wait_does_not_hammer(crm_path):
    await crm_db.queue_message(None, "u1", "текст", crm_db=crm_path)
    assert await sender.run_once(Client(fail=_flood_wait(7200)), crm_path) is True
    msgs = await crm_db.list_messages(crm_db=crm_path)
    assert msgs[0]["status"] == "queued", "сообщение остаётся в очереди"
    s = await crm_db.get_settings(crm_path)
    assert crm_db.blocked_until(s) >= time.time() + 7200
    ok = Client()
    assert await sender.run_once(ok, crm_path) is False and ok.sent == []


@pytest.mark.asyncio
async def test_throttle_never_overwrites_base_cap_and_expires(crm_path):
    await crm_db.set_settings({"daily_cap": "3"}, crm_path)
    for _ in range(4):
        await sender._adaptive_throttle(crm_path, peer_flood=True)
    s = await crm_db.get_settings(crm_path)
    assert s["daily_cap"] == "3", "базовый лимит не трогаем"
    assert crm_db.effective_daily_cap(s) == 1
    assert crm_db.throttle_active(s) == 1
    # срок истёк - лимит снова базовый
    await crm_db.set_settings({"throttle_until": str(int(time.time()) - 1)}, crm_path)
    s = await crm_db.get_settings(crm_path)
    assert crm_db.effective_daily_cap(s) == 3


@pytest.mark.asyncio
async def test_small_cap_is_never_raised(crm_path):
    await crm_db.set_settings({"daily_cap": "1"}, crm_path)
    await sender._adaptive_throttle(crm_path, wait=900)
    assert crm_db.effective_daily_cap(await crm_db.get_settings(crm_path)) == 1


@pytest.mark.asyncio
async def test_expired_lease_is_not_resent(crm_path):
    """Сбой между отправкой и записью результата: не повторяем автоматически."""
    await crm_db.queue_message(None, "u1", "текст", crm_db=crm_path)
    claimed = await crm_db.claim_next("crashed", lease_ms=1, crm_db=crm_path)
    assert claimed and claimed["status"] == "sending"
    time.sleep(1.1)
    assert await crm_db.release_expired_leases(crm_path) == 1
    msg = (await crm_db.list_messages(crm_db=crm_path))[0]
    assert msg["status"] == "failed" and "неизвестен" in msg["error"]
    client = Client()
    assert await sender.run_once(client, crm_path) is False
    assert client.sent == []


@pytest.mark.asyncio
async def test_claim_returns_exact_row(crm_path):
    a = await crm_db.queue_message(None, "a", "1", crm_db=crm_path)
    b = await crm_db.queue_message(None, "b", "2", crm_db=crm_path)
    first = await crm_db.claim_next("w", crm_db=crm_path)
    second = await crm_db.claim_next("w", crm_db=crm_path)
    assert (first["id"], second["id"]) == (a, b)
    assert await crm_db.claim_next("w", crm_db=crm_path) is None


@pytest.mark.asyncio
async def test_drip_step_without_username_is_cancelled(crm_path):
    await crm_db.seed_demo_targets(crm_path)
    target = (await crm_db.list_targets(crm_db=crm_path))[0]
    async with crm_db.connect(crm_path) as conn:
        await conn.execute("UPDATE targets SET username = '' WHERE id = ?", (target["id"],))
    await crm_db.create_sequence(target["id"], [{"body": "шаг", "due_at": "2000-01-01 00:00:00"}], crm_db=crm_path)
    await crm_db.set_settings({"drip_enabled": "1"}, crm_path)
    await sender.run_once(Client(), crm_path)
    seqs = await crm_db.get_target_sequences(target["id"], crm_db=crm_path)
    assert [s["status"] for s in seqs] == ["cancelled"]


@pytest.mark.asyncio
async def test_heartbeat_marks_worker_alive(crm_path):
    assert (await crm_db.summary(crm_path))["worker_alive"] is False
    await sender.run_once(Client(), crm_path)
    assert (await crm_db.summary(crm_path))["worker_alive"] is True
