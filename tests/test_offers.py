"""Tests for services/offers.py -- offer library, extraction, generation, templates (no network)."""

import pytest
import pytest_asyncio

from db.database import Database
from data.niches import NICHES
from services import offers
from services.llm import LLMError
from services.offers import (
    OPT_OUT, OfferLibrary, OfferSourceError, extract_techniques, fetch_source, generate_offer,
    render_template, tg_channel, youtube_id,
)


class FakeLLM:
    def __init__(self, responses, enabled=True):
        self.responses = list(responses)
        self.enabled = enabled
        self.calls = []

    async def chat_json(self, system, user, **kw):
        self.calls.append((system, user))
        resp = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest_asyncio.fixture
async def lib(tmp_path):
    db = Database(path=str(tmp_path / "test.db"))
    await db.connect()
    library = OfferLibrary(db)
    await library.init()
    await library.init()  # idempotent
    yield library
    await db.close()


# ---------- extraction ----------

@pytest.mark.asyncio
async def test_extract_merges_chunks_and_dedupes():
    llm = FakeLLM([
        {"techniques": [
            {"kind": "hook", "title": "Демо до продажи", "description": "Кратко", "example": ""},
            {"kind": "WEIRD", "title": "Вопрос в конце", "description": "Один вопрос да/нет", "example": "Прислать?"},
            {"kind": "pain", "title": "", "description": "no title -> dropped"},
            "garbage",
        ]},
        [  # bare list is accepted too
            {"kind": "hook", "title": "демо  до продажи!", "description": "Показать готовый макет до разговора о деньгах",
             "example": "Сделал набросок вашего сайта"},
            {"kind": "guarantee", "title": "Оплата после результата", "description": "Снимает риск"},
        ],
    ])
    text = ("Абзац про холодные продажи. " * 400 + "\n") * 2  # > 12k chars -> 2 chunks
    result = await extract_techniques(llm, "Видео", text)
    assert len(llm.calls) == 2
    assert "часть 1/2" in llm.calls[0][1]
    by_title = {t["title"]: t for t in result}
    assert len(result) == 3
    demo = by_title["Демо до продажи"]
    assert demo["description"] == "Показать готовый макет до разговора о деньгах"  # longer wins
    assert demo["example"] == "Сделал набросок вашего сайта"
    assert by_title["Вопрос в конце"]["kind"] == "other"


@pytest.mark.asyncio
async def test_extract_requires_llm():
    with pytest.raises(LLMError):
        await extract_techniques(FakeLLM([{}], enabled=False), "t", "text")


@pytest.mark.asyncio
async def test_extract_raises_when_all_chunks_fail():
    with pytest.raises(LLMError):
        await extract_techniques(FakeLLM([LLMError("down")]), "t", "text")


def test_chunk_text_respects_size():
    text = "Предложение номер один. " * 2000
    chunks = offers.chunk_text(text, 12_000)
    assert all(len(c) <= 12_001 for c in chunks)
    assert sum(len(c) for c in chunks) >= len(text.strip()) - len(chunks) * 2


# ---------- generation ----------

@pytest.mark.asyncio
async def test_generate_offer_validates_and_fills_missing_variants():
    niche = NICHES[0]
    llm = FakeLLM([{
        "offer": "Лендинг за 7 дней с оплатой после результата.",
        "variants": [
            {"name": "X", "first_message": "Здравствуйте, {name}! Посмотрите https://spam.example 🔥🔥 демо!!! " + "а" * 600,
             "followup_1": "Напомню", "followup_2": "Ещё раз"},
            {"name": "B", "first_message": ""},  # empty -> dropped
        ],
        "angles": ["Потеря заявок", "", 5],
    }])
    techniques = [{"kind": "hook", "title": "Демо", "description": "Показать макет", "example": "Сделал набросок"}]
    result = await generate_offer(llm, niche.label, niche, techniques, my_profile="Делаю сайты на Tilda")

    system, user = llm.calls[0]
    assert "не побеспокою" in system and "{demo_url}" in system
    assert niche.avg_check in user and "Tilda" in user and "Сделал набросок" in user

    assert result["offer"].startswith("Лендинг")
    assert [v["name"] for v in result["variants"]] == ["A", "B", "C"]
    first = result["variants"][0]
    assert len(first["first_message"]) <= offers.MAX_FIRST_MESSAGE
    assert "http" not in first["first_message"] and "🔥" not in first["first_message"]
    assert "!!!" not in first["first_message"]
    assert first["followup_2"].endswith(OPT_OUT)
    for v in result["variants"][1:]:  # filled from templates
        assert v["first_message"] and v["followup_1"] and "не побеспокою" in v["followup_2"]
    assert result["angles"] == ["Потеря заявок", "5"]


@pytest.mark.asyncio
async def test_generate_offer_garbage_output_falls_back():
    result = await generate_offer(FakeLLM([["not", "a", "dict"]]), "Стоматологии", {"avg_check": "50 000"}, [])
    assert "50 000" in result["offer"]
    assert len(result["variants"]) == 3 and result["angles"] == []


@pytest.mark.asyncio
async def test_generate_offer_requires_llm():
    with pytest.raises(LLMError):
        await generate_offer(FakeLLM([{}], enabled=False), "n", {}, [])


# ---------- templates ----------

def test_render_template_substitutes_and_tidies():
    tpl = "Здравствуйте, {name}! Видел {company} в {city}. Демо: {demo_url}. Ниша: {niche}."
    out = render_template(tpl, name="Иван", company="СтройДом", city="Казань", demo_url="https://x.ru/d", niche="стройка")
    assert out == "Здравствуйте, Иван! Видел СтройДом в Казань. Демо: https://x.ru/d. Ниша: стройка."
    assert render_template("Здравствуйте, {name}! Как дела?") == "Здравствуйте! Как дела?"


def test_render_template_is_injection_safe():
    assert render_template("{unknown} {name.__class__} {0} {{x}}", name="A") == "{name.__class__} {}"
    # Values are inserted verbatim and not re-expanded.
    assert render_template("Hi {name}", name="{company} {0.__class__}", company="X") == "Hi {company} {0.__class__}"
    assert render_template("{name}", name=None) == ""


# ---------- storage ----------

@pytest.mark.asyncio
async def test_library_crud(lib):
    sid = await lib.add_source("https://youtu.be/abcdefghij1", "Видео про офферы")
    techs = [
        {"kind": "hook", "title": "Демо до продажи", "description": "d1", "example": "e1"},
        {"kind": "cta", "title": "Вопрос да/нет", "description": "d2"},
        {"kind": "cta", "title": "вопрос да нет", "description": "dup"},
    ]
    assert await lib.add_techniques(sid, techs) == 2
    sid2 = await lib.add_source("@channel", "Telegram @channel")
    assert await lib.add_techniques(sid2, [{"kind": "hook", "title": "ДЕМО до продажи", "description": "x"},
                                           {"kind": "pain", "title": "Потеря заявок", "description": "y"}]) == 1

    items = await lib.list_techniques()
    assert len(items) == 3 and items[0]["title"] == "Потеря заявок"
    assert items[-1]["source_title"] == "Видео про офферы" and items[-1]["example"] == "e1"
    assert await lib.count_by_kind() == {"hook": 1, "cta": 1, "pain": 1}
    assert len(await lib.list_techniques(limit=1)) == 1
    sources = await lib.list_sources()
    assert [s["techniques"] for s in sources] == [1, 2]

    variants = [{"name": "A", "first_message": "Привет", "followup_1": "", "followup_2": ""}]
    oid = await lib.save_offer("house_construction", "Оффер 1", variants, ["угол"])
    oid2 = await lib.save_offer("house_construction", "Оффер 2", variants)
    await lib.save_offer("dentistry", "Оффер 3", [])
    got = await lib.get_offer(oid)
    assert got["offer"] == "Оффер 1" and got["variants"] == variants and got["angles"] == ["угол"]
    assert (await lib.latest_offer("house_construction"))["id"] == oid2
    assert await lib.latest_offer("nope") is None
    assert await lib.get_offer(999) is None
    assert [o["offer"] for o in await lib.list_offers()] == ["Оффер 3", "Оффер 2", "Оффер 1"]


# ---------- sources ----------

def test_ref_detection():
    assert youtube_id("https://www.youtube.com/watch?v=t_Urg7RZwdU&t=10") == "t_Urg7RZwdU"
    assert youtube_id("https://youtu.be/YLnVoQ0dm4o") == "YLnVoQ0dm4o"
    assert youtube_id("https://youtube.com/shorts/WPDzM-d2NLc") == "WPDzM-d2NLc"
    assert youtube_id("t_Urg7RZwdU") == "t_Urg7RZwdU"
    assert youtube_id("Приветствую") == "" and youtube_id("abcdefghijk") == ""
    assert tg_channel("@vsem_podryad") == "vsem_podryad"
    assert tg_channel("https://t.me/s/vsem_podryad") == "vsem_podryad"
    assert tg_channel("t.me/vsem_podryad/123") == "vsem_podryad"
    assert tg_channel("https://t.me/+AbCdEf") == ""


@pytest.mark.asyncio
async def test_fetch_source_plain_text():
    assert await fetch_source("  Пишите коротко и по делу  ") == ("Текст", "Пишите коротко и по делу")
    with pytest.raises(OfferSourceError):
        await fetch_source("   ")


ARTICLE = (
    "<html><head><title>Как писать &laquo;оффер&raquo;</title><style>.a{}</style></head>"
    "<body><script>var x = 'secret';</script><h1>Оффер</h1><p>Первый абзац &amp; суть.</p>"
    "<div>" + "Текст статьи. " * 50 + "</div><!-- comment --></body></html>"
)


@pytest.mark.asyncio
async def test_fetch_source_url(monkeypatch):
    seen = []

    async def fake_get(url, timeout=20):
        seen.append(url)
        return ARTICLE

    monkeypatch.setattr(offers, "_http_get", fake_get)
    title, text = await fetch_source("https://example.com/article")
    assert seen == ["https://example.com/article"]
    assert title == "Как писать «оффер»"
    assert "Первый абзац & суть." in text and "secret" not in text and "comment" not in text
    assert "<" not in text and ".a{}" not in text

    async def short_page(url, timeout=20):
        return "<html><body><div id=app></div></body></html>"

    monkeypatch.setattr(offers, "_http_get", short_page)
    with pytest.raises(OfferSourceError):
        await fetch_source("https://example.com/spa")


@pytest.mark.asyncio
async def test_fetch_source_tg_paginates(monkeypatch):
    calls = []

    async def fake_posts(session, channel):
        calls.append(channel)
        if "before=" not in channel:
            return [{"id": str(i), "text": f"пост {i}"} for i in range(81, 101)]
        if channel.endswith("before=81"):
            return [{"id": str(i), "text": f"пост {i}" if i % 2 else ""} for i in range(61, 81)]
        return []

    monkeypatch.setattr(offers, "fetch_tg_channel_posts", fake_posts)
    monkeypatch.setattr(offers.random, "uniform", lambda a, b: 0)
    title, text = await fetch_source("https://t.me/somechannel")
    assert title == "Telegram @somechannel"
    assert calls == ["somechannel", "somechannel?before=81", "somechannel?before=61"]
    assert text.startswith("пост 100") and "пост 61" in text and text.count("---") == 29


@pytest.mark.asyncio
async def test_fetch_source_youtube_error_is_user_facing(monkeypatch):
    def blocked(video_id):
        raise OfferSourceError("YouTube заблокировал запросы")

    async def no_title(video_id):
        return ""

    monkeypatch.setattr(offers, "_youtube_transcript_sync", blocked)
    monkeypatch.setattr(offers, "_youtube_title", no_title)
    with pytest.raises(OfferSourceError, match="заблокировал"):
        await fetch_source("https://youtu.be/t_Urg7RZwdU")

    monkeypatch.setattr(offers, "_youtube_transcript_sync", lambda vid: "расшифровка")
    assert await fetch_source("t_Urg7RZwdU") == ("YouTube t_Urg7RZwdU", "расшифровка")
