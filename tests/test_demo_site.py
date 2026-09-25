"""Tests for services/demo_site.py and web/server.py."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from models.organization import Organization
from services.demo_site import (
    DemoSiteStore,
    PALETTES,
    build_demo,
    generate_content,
    make_slug,
    render_html,
)
from services.llm import LLMError
from web.server import make_app


def construction_org(**kw):
    base = dict(id="1", name="СтройМастер", phone="8 (843) 212-45-67", address="ул. Баумана, 15",
                category="Ремонт квартир", city="Казань", rating=4.8, reviews=120,
                socials="https://wa.me/79175551234, https://t.me/stroy, javascript:alert(1)")
    base.update(kw)
    return Organization(**base)


def dental_org():
    return Organization(id="2", name="Улыбка", phone="+7 495 123-45-67", category="Стоматология",
                        city="Москва", address="Ленинский пр., 32")


class NoLLM:
    enabled = False


class FakeLLM:
    enabled = True

    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    async def chat_json(self, system, user, **kw):
        self.calls.append((system, user))
        if self.exc:
            raise self.exc
        return self.result


def assert_shape(c):
    assert c["headline"] and c["subheadline"] and c["about"] and c["cta"]
    assert 4 <= len(c["services"]) <= 6
    assert 3 <= len(c["advantages"]) <= 4
    assert len(c["faq"]) == 3
    assert c["palette"] in PALETTES


@pytest.mark.asyncio
async def test_fallback_construction():
    c = await generate_content(construction_org(), NoLLM())
    assert_shape(c)
    assert "Казани" in c["headline"]
    assert any("Ремонт" in s["title"] for s in c["services"])


@pytest.mark.asyncio
async def test_fallback_dental():
    c = await generate_content(dental_org(), NoLLM())
    assert_shape(c)
    assert any("Имплант" in s["title"] for s in c["services"])
    assert "Москве" in c["headline"]


@pytest.mark.asyncio
async def test_llm_error_falls_back():
    c = await generate_content(dental_org(), FakeLLM(exc=LLMError("down")))
    assert_shape(c)
    assert any("Имплант" in s["title"] for s in c["services"])


@pytest.mark.asyncio
async def test_llm_content_is_clamped_and_escaped():
    evil = '<script>alert("x")</script>'
    llm = FakeLLM(result={
        "headline": "Лучший ремонт <b>города</b>",
        "subheadline": 'a" onmouseover="alert(1)',
        "services": [{"title": "Плитка <script>x</script>", "text": "Кладём плитку"}, {"title": ""}],
        "advantages": ["Быстро", {"title": "Честно"}],
        "about": "О нас " + "очень " * 400,
        "cta": "Звоните",
        "faq": [{"q": "Вопрос?", "a": evil}],
        "palette": "PURPLE",
    })
    org = construction_org(name='ООО <script>alert(1)</script> & "Ко"')
    c = await generate_content(org, llm, offer="акцент на скорость")
    assert llm.calls and "акцент на скорость" in llm.calls[0][1]
    assert_shape(c)
    assert c["headline"] == "Лучший ремонт города"
    assert c["services"][0]["title"] == "Плитка x"
    assert c["advantages"][:2] == ["Быстро", "Честно"]
    assert len(c["about"]) <= 900
    assert c["palette"] == "purple"

    page = render_html(org, c)
    assert "<script" not in page.lower()
    assert "&lt;script&gt;" in page
    assert 'onmouseover="' not in page
    assert "javascript:" not in page


@pytest.mark.asyncio
async def test_llm_garbage_uses_fallback():
    c = await generate_content(construction_org(), FakeLLM(result=["not", "a", "dict"]))
    assert_shape(c)


def test_render_contains_essentials():
    page = render_html(construction_org(), {})
    assert '<meta name="robots" content="noindex' in page
    assert 'href="tel:+78432124567"' in page
    assert "120 отзывов" in page and "4,8" in page
    assert "https://wa.me/79175551234" in page and "WhatsApp" in page
    assert "Демо-версия сайта. Подготовлено для СтройМастер" in page
    assert "<details" in page and "Позвонить" in page


def test_render_without_rating_or_phone():
    page = render_html(Organization(id="3", name="Юрист Петров", category="Юридические услуги"), {})
    assert "отзыв" not in page
    assert "tel:" not in page
    assert 'href="#contacts"' in page


def test_slug_format():
    org = construction_org(name="Стоматология «Улыбка» №1")
    slugs = {make_slug(org) for _ in range(20)}
    assert len(slugs) == 20
    for s in slugs:
        assert s.startswith("stomatologiya-ulybka")
        assert DemoSiteStore.valid_slug(s)
    assert make_slug(Organization(id="x", name="!!!")).startswith("demo-")


@pytest.mark.parametrize("bad", ["../passwd", "..", "a/b", "AB-cd", "ab", "x" * 81, "a.html", "", None, "%2e%2e"])
def test_store_rejects_bad_slugs(tmp_path, bad):
    store = DemoSiteStore(str(tmp_path / "sites"))
    (tmp_path / "passwd.html").write_text("secret")
    assert store.get(bad) is None
    with pytest.raises(ValueError):
        store.save(bad, "<p>x</p>")


def test_store_roundtrip(tmp_path):
    store = DemoSiteStore(str(tmp_path / "sites"))
    path = store.save("abc-123", "<p>hi</p>")
    assert path.endswith("abc-123.html")
    assert store.get("abc-123") == "<p>hi</p>"
    assert store.get("abc-124") is None


@pytest.mark.asyncio
async def test_build_demo_and_serve(tmp_path):
    store = DemoSiteStore(str(tmp_path))
    url = await build_demo(construction_org(), NoLLM(), store, "http://1.2.3.4:8080/")
    assert url.startswith("http://1.2.3.4:8080/d/stroymaster-")
    slug = url.rsplit("/", 1)[1]

    async with TestClient(TestServer(make_app(store))) as client:
        r = await client.get(f"/d/{slug}")
        assert r.status == 200
        assert r.headers["X-Robots-Tag"].startswith("noindex")
        assert "text/html" in r.headers["Content-Type"]
        assert "СтройМастер" in await r.text()

        r = await client.get("/d/nope-000000")
        assert r.status == 404
        assert "404" in await r.text()

        r = await client.get("/d/..%2F..%2Fetc%2Fpasswd")
        assert r.status == 404

        r = await client.get("/health")
        assert r.status == 200 and await r.text() == "ok"
