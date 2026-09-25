"""Tests for CRM storage, reply classifier and the outreach engine (fake Telethon client)."""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from telethon import errors

from db.database import Database
from services.classifier import classify_reply, classify_rules
from services.crm import CRM
from services.outreach import OutreachLimits, OutreachService, leads_from_orgs, leads_from_tg, render

# Wednesday 12:00 Moscow (09:00 UTC) — inside working hours.
WORK = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc)
SATURDAY = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)

VARIANTS = [
    {"name": "A", "first_message": "Здравствуйте, {name}! Сделал черновик сайта для {company}: {demo_url}\nЕсли неактуально — напишите.",
     "followup_1": "Напомню про сайт для {company}.", "followup_2": "Последнее сообщение."},
    {"name": "B", "first_message": "Добрый день! Вопрос по {company} в {city}.", "followup_1": "B1", "followup_2": "B2"},
]


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeClient:
    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail

    async def get_entity(self, ref):
        if self.fail:
            raise self.fail
        return FakeUser(abs(hash(ref)) % 10**9)

    async def send_message(self, entity, text, **kw):
        self.sent.append((getattr(entity, "id", entity), text))


class FakeAccount:
    def __init__(self, client, enabled=True):
        self.client = client
        self.enabled = enabled
        self.error = ""


@pytest_asyncio.fixture
async def crm(tmp_path):
    db = Database(str(tmp_path / "crm.db"))
    await db.connect()
    c = CRM(db)
    await c.init()
    yield c
    await db.close()


async def _campaign(crm, **kw):
    cid = await crm.create_campaign(1, "Тест", "стройка", "оффер", VARIANTS, delays=[3, 5], personalize=False, **kw)
    await crm.update_campaign(cid, status="active")
    return cid


def _lead(i, **kw):
    base = {"source": "tg", "ref_id": f"tg:{i}", "name": f"Иван{i}", "company": f"Ромашка{i}",
            "city": "Казань", "tg_username": f"user{i}"}
    base.update(kw)
    return base


class TestHelpers:

    def test_render_fills_and_drops_missing_demo_line(self):
        text = render(VARIANTS[0]["first_message"], name="Иван", company="Ромашка", demo_url="")
        assert text == "Здравствуйте, Иван!\nЕсли неактуально — напишите."
        full = render(VARIANTS[0]["first_message"], name="Иван", company="Ромашка", demo_url="http://x/d/a")
        assert "Ромашка: http://x/d/a" in full
        assert "{" not in render("Привет, {name}! {unknown}", name="")

    def test_leads_from_orgs(self):
        leads = leads_from_orgs([
            {"id": "1", "name": "A", "phone": "+79001112233, +7495", "socials": "https://t.me/firm_a, https://vk.com/a"},
            {"id": "2", "name": "B", "phone": "", "socials": "https://t.me/+79005556677"},
            {"id": "3", "name": "C", "phone": "", "socials": "https://vk.com/c"},
        ])
        assert [l["ref_id"] for l in leads] == ["org:1", "org:2"]
        assert leads[0]["tg_username"] == "firm_a" and leads[0]["phone"] == "+79001112233"
        assert leads[1]["phone"] == "+79005556677" and leads[1]["tg_username"] == ""

    def test_leads_from_tg(self):
        leads = leads_from_tg([{"user_id": 5, "username": "ivan", "name": "Иван"}, {"user_id": 0, "username": ""}])
        assert len(leads) == 1 and leads[0]["tg_user_id"] == 5


class TestClassifier:

    @pytest.mark.parametrize("text,label", [
        ("Сколько стоит такой сайт?", "price"),
        ("Не пишите мне больше", "stop"),
        ("Спасибо, не актуально", "not_interested"),
        ("Давайте созвонимся завтра", "interested"),
        ("Напишите через месяц", "later"),
        ("У нас уже есть сайт", "has_site"),
        ("ок", "other"),
    ])
    def test_rules(self, text, label):
        assert classify_rules(text).label == label

    @pytest.mark.asyncio
    async def test_jev_backend_preferred(self):
        class L:
            enabled = jev_enabled = True

            async def jev(self, state, questions):
                assert set(questions) == {"intent", "hot"}
                assert questions["intent"]["type"] == "choice"
                return {"intent": {"choice": "price", "confidence": 0.93}, "hot": {"noul": 0.88}}

        cls = await classify_reply(L(), "почём?", "наше")
        assert (cls.label, cls.backend, cls.hot) == ("price", "jev", 0.88)

    @pytest.mark.asyncio
    async def test_falls_back_to_deepseek_then_rules(self):
        class L:
            enabled, jev_enabled = True, True

            async def jev(self, *a):
                raise RuntimeError("down")

            async def chat_json(self, *a, **k):
                return {"label": "interested", "hot": 0.9, "confidence": 0.8}

        assert (await classify_reply(L(), "да")).backend == "deepseek"
        assert (await classify_reply(None, "сколько?")).backend == "rules"


class TestCRM:

    @pytest.mark.asyncio
    async def test_add_leads_dedup_and_stoplist(self, crm):
        cid = await _campaign(crm)
        await crm.add_stop(["u:user2"])
        added, skipped = await crm.add_leads(cid, [_lead(1), _lead(1), _lead(2), {"source": "tg", "name": "no contact"}])
        assert (added, skipped) == (1, 3)

    @pytest.mark.asyncio
    async def test_not_contacted_twice_across_campaigns(self, crm):
        c1 = await _campaign(crm)
        await crm.add_leads(c1, [_lead(1)])
        lead = (await crm.list_leads(c1))[0]
        await crm.update_lead(lead["id"], status="sent")
        c2 = await _campaign(crm)
        assert await crm.add_leads(c2, [_lead(1)]) == (0, 1)


class TestOutreach:

    @pytest.mark.asyncio
    async def test_sequence_and_quota(self, crm):
        cid = await _campaign(crm)
        await crm.add_leads(cid, [_lead(i) for i in range(8)])
        client = FakeClient()
        svc = OutreachService(crm, [FakeAccount(client)], limits=OutreachLimits(min_delay=0, max_delay=0))

        assert await svc.tick(NIGHT) == 0 and await svc.tick(SATURDAY) == 0
        for _ in range(10):
            await svc.tick(WORK)
        # Warm-up: first day limit is warmup_start=5.
        assert len(client.sent) == 5
        leads = await crm.list_leads(cid)
        sent = [l for l in leads if l["status"] == "sent"]
        assert len(sent) == 5 and all(l["step"] == 1 and l["next_at"] for l in sent)
        assert {l["variant"] for l in sent} == {"A", "B"}
        assert "{" not in client.sent[0][1]

        # 3 days later the follow-up is due and does not consume the new-dialog quota.
        later = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)  # Monday
        for _ in range(20):
            await svc.tick(later)
        texts = [t for _, t in client.sent]
        followups = [t for t in texts if t.startswith("Напомню") or t == "B1"]
        assert len(followups) == 5
        # Day 2 of activity: warm-up limit grows to 5 + 3 = 8, so the remaining 3 new leads go out.
        assert len(texts) == 5 + 5 + 3

    @pytest.mark.asyncio
    async def test_reply_stops_sequence_and_stoplist(self, crm):
        cid = await _campaign(crm)
        await crm.add_leads(cid, [_lead(1)])
        client = FakeClient()
        got = []

        async def on_reply(lead, text, cls):
            got.append((lead["status"], cls.label))

        svc = OutreachService(crm, [FakeAccount(client)], limits=OutreachLimits(min_delay=0, max_delay=0),
                              on_reply=on_reply)
        await svc.tick(WORK)
        lead = (await crm.list_leads(cid))[0]
        res = await svc.handle_incoming(0, lead["tg_user_id"], "Не пишите мне больше")
        assert res and res[1].label == "stop"
        assert got == [("stopped", "stop")]
        assert "u:user1" in await crm.stopped_keys()
        assert await crm.due_leads(datetime(2026, 10, 30, 9, tzinfo=timezone.utc)) == []
        assert await svc.handle_incoming(0, 999, "привет") is None

    @pytest.mark.asyncio
    async def test_peer_flood_pauses_account(self, crm):
        cid = await _campaign(crm)
        await crm.add_leads(cid, [_lead(1)])
        notes = []

        async def notify(text):
            notes.append(text)

        svc = OutreachService(crm, [FakeAccount(FakeClient(fail=errors.PeerFloodError(request=None)))],
                              notify=notify, limits=OutreachLimits(min_delay=0, max_delay=0))
        assert await svc.tick(WORK) == 0
        assert svc.paused_until[0] > WORK and "PeerFlood" in notes[0]
        assert (await crm.list_leads(cid))[0]["status"] == "new"  # retried after the pause

    @pytest.mark.asyncio
    async def test_privacy_error_marks_failed(self, crm):
        cid = await _campaign(crm)
        await crm.add_leads(cid, [_lead(1)])
        svc = OutreachService(crm, [FakeAccount(FakeClient(fail=errors.UsernameNotOccupiedError(request=None)))],
                              limits=OutreachLimits(min_delay=0, max_delay=0))
        await svc.tick(WORK)
        assert (await crm.list_leads(cid))[0]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_unexpected_error_does_not_block_queue(self, crm):
        cid = await _campaign(crm)
        await crm.add_leads(cid, [_lead(1), _lead(2)])

        class Flaky(FakeClient):
            async def get_entity(self, ref):
                if ref == "user1":
                    raise RuntimeError("boom")
                return await super().get_entity(ref)

        client = Flaky()
        svc = OutreachService(crm, [FakeAccount(client)], limits=OutreachLimits(min_delay=0, max_delay=0))
        await svc.tick(WORK)
        await svc.tick(WORK)
        assert len(client.sent) == 1  # lead 2 went out despite lead 1 failing
        l1 = next(l for l in await crm.list_leads(cid) if l["tg_username"] == "user1")
        assert l1["status"] == "new" and l1["next_at"] and l1["extra"]["attempts"] == 1

    @pytest.mark.asyncio
    async def test_demo_builder_used_for_maps_leads(self, crm):
        cid = await _campaign(crm, use_demo=True)
        await crm.add_leads(cid, [_lead(1, source="maps", ref_id="org:1")])
        client = FakeClient()

        async def build(lead, campaign):
            return "http://1.2.3.4:8080/d/romashka-abc123"

        svc = OutreachService(crm, [FakeAccount(client)], demo_builder=build,
                              limits=OutreachLimits(min_delay=0, max_delay=0))
        await svc.tick(WORK)
        lead = (await crm.list_leads(cid))[0]
        if lead["variant"] == "A":  # only variant A uses {demo_url}
            assert lead["demo_url"].endswith("abc123") and "romashka-abc123" in client.sent[0][1]
        else:
            assert lead["demo_url"] == ""

    @pytest.mark.asyncio
    async def test_city_locative(self, crm):
        cid = await _campaign(crm)
        await crm.add_leads(cid, [_lead(1)])  # id 1 -> variant B: "в {city}"
        svc = OutreachService(crm, [FakeAccount(FakeClient())])
        lead = (await crm.list_leads(cid))[0]
        text = await svc.compose(lead, await crm.get_campaign(cid))
        assert "в Казани" in text

    @pytest.mark.asyncio
    async def test_stats(self, crm):
        cid = await _campaign(crm)
        await crm.add_leads(cid, [_lead(i) for i in range(4)])
        svc = OutreachService(crm, [FakeAccount(FakeClient())], limits=OutreachLimits(min_delay=0, max_delay=0))
        for _ in range(4):
            await svc.tick(WORK)
        lead = (await crm.list_leads(cid))[0]
        await svc.handle_incoming(0, lead["tg_user_id"], "Сколько стоит?")
        stats = await crm.campaign_stats(cid)
        assert stats["contacted"] == 4 and stats["replied"] == 1
        assert sum(v["positive"] for v in stats["variants"]) == 1
