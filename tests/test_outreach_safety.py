"""Защиты рассылок бота: явный стоп, стабильные слоты аккаунтов, пауза после рестарта, дедуп."""

from datetime import UTC, datetime

import pytest
from telethon import errors

from services.outreach import OutreachLimits, OutreachService, is_explicit_stop
from tests.test_outreach import WORK, FakeAccount, FakeClient, _campaign, _lead, crm  # noqa: F401


class IdClient(FakeClient):
    """Клиент с фиксированным Telegram id аккаунта и фиксированным id получателя."""

    def __init__(self, me_id, peer_id=None, **kw):
        super().__init__(**kw)
        self.me_id = me_id
        self.peer_id = peer_id

    async def get_me(self):
        return type("Me", (), {"id": self.me_id})()

    async def __call__(self, request):  # ImportContactsRequest (поиск по телефону)
        self.imported = getattr(self, "imported", 0) + 1
        return type("R", (), {"users": [type("U", (), {"id": self.peer_id or 1})()]})()

    async def get_entity(self, ref):
        if self.peer_id:
            return type("U", (), {"id": self.peer_id})()
        return await super().get_entity(ref)


@pytest.mark.parametrize("text", [
    "Не пишите мне больше", "НЕ ПИШИТЕ", "стоп", "Stop!", "удалите меня из рассылки",
    "это спам", "как отписаться?", "больше не беспокойте", "unsubscribe please",
])
def test_explicit_stop_detected(text):
    assert is_explicit_stop(text)


@pytest.mark.parametrize("text", [
    "не надо долго ждать, давайте созвонимся", "отпишитесь мне по цене", "stopka документов готова",
    "не интересно сейчас, но через месяц напишите", "сколько стоит?", "",
])
def test_ordinary_replies_are_not_permanent_stop(text):
    assert not is_explicit_stop(text)


@pytest.mark.asyncio
async def test_slots_follow_telegram_id_not_list_order(crm):  # noqa: F811
    cid = await _campaign(crm)
    await crm.add_leads(cid, [_lead(1)])
    a, b = IdClient(111), IdClient(222)
    svc = OutreachService(crm, [FakeAccount(a), FakeAccount(b)], limits=OutreachLimits(min_delay=0, max_delay=0))
    await svc.restore_state()
    await svc.tick(WORK)
    lead = (await crm.list_leads(cid))[0]
    first_slot = lead["account"]
    sender_client = a if a.sent else b

    # перезапуск с обратным порядком TG_SESSION_N: follow-up уходит с того же аккаунта
    a2, b2 = IdClient(222), IdClient(111)
    svc2 = OutreachService(crm, [FakeAccount(a2), FakeAccount(b2)], limits=OutreachLimits(min_delay=0, max_delay=0))
    await svc2.restore_state()
    later = datetime(2026, 9, 28, 9, tzinfo=UTC)
    await svc2.tick(later)
    same = b2 if sender_client is a else a2
    other = a2 if same is b2 else b2
    assert same.sent and not other.sent
    assert (await crm.list_leads(cid))[0]["account"] == first_slot


@pytest.mark.asyncio
async def test_peer_flood_pause_survives_restart(crm):  # noqa: F811
    cid = await _campaign(crm)
    await crm.add_leads(cid, [_lead(1), _lead(2)])
    flood = IdClient(111, fail=errors.PeerFloodError(request=None))
    svc = OutreachService(crm, [FakeAccount(flood)], limits=OutreachLimits(min_delay=0, max_delay=0))
    await svc.restore_state()
    assert await svc.tick(WORK) == 0

    fresh = IdClient(111)
    svc2 = OutreachService(crm, [FakeAccount(fresh)], limits=OutreachLimits(min_delay=0, max_delay=0))
    await svc2.restore_state()
    assert svc2.paused_until[0] > WORK
    assert await svc2.tick(WORK) == 0 and fresh.sent == []


@pytest.mark.asyncio
async def test_dedup_failure_is_fail_closed(crm, monkeypatch):  # noqa: F811
    cid = await _campaign(crm)
    await crm.add_leads(cid, [_lead(1)])

    async def boom(*a, **k):
        raise RuntimeError("db is locked")

    monkeypatch.setattr(crm, "is_recipient_contacted", boom)
    client = FakeClient()
    svc = OutreachService(crm, [FakeAccount(client)], limits=OutreachLimits(min_delay=0, max_delay=0))
    assert await svc.tick(WORK) == 0
    assert client.sent == []


@pytest.mark.asyncio
async def test_same_person_via_phone_and_username_gets_one_message(crm):  # noqa: F811
    c1 = await _campaign(crm)
    c2 = await _campaign(crm)
    await crm.add_leads(c1, [_lead(1, tg_username="ivan_site")])
    await crm.add_leads(c2, [_lead(2, tg_username="", phone="+79990001122")])
    client = IdClient(111, peer_id=4242)
    svc = OutreachService(crm, [FakeAccount(client)], limits=OutreachLimits(min_delay=0, max_delay=0))
    for _ in range(3):
        svc.next_send.clear()
        await svc.tick(WORK)
    assert len(client.sent) == 1
    assert client.imported == 1, "второй лид дошёл до поиска по телефону и был отсечён по Telegram id"
    statuses = sorted([lead["status"] for lead in await crm.list_leads(c1)] +
                      [lead["status"] for lead in await crm.list_leads(c2)])
    assert statuses == ["failed", "sent"]
