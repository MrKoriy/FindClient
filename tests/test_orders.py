"""Tests for freelance order parsers, matching and the polling service."""

import pytest
import pytest_asyncio

from api.orders import (
    matches,
    parse_fl_rss,
    parse_freelance_ru,
    parse_freelancejob_rss,
    parse_kwork_wants,
    parse_tg_channel,
)
from db.database import Database
from models.order import Order
from services.orders_service import ENABLED, OrdersService, format_order

FL_RSS = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<item><title><![CDATA[Лендинг для школы (Бюджет: 15 000 &#8381;)]]></title>
<link>https://www.fl.ru/projects/5523338/landing.html</link>
<description><![CDATA[Нужен <b>лендинг</b> на Tilda]]></description>
<pubDate>Fri, 25 Sep 2026 10:16:28 GMT</pubDate></item>
<item><title>bad</title><link>https://www.fl.ru/users/1</link></item>
</channel></rss>"""

FREELANCE_RU = """<article class="task-card"><a class="task-card__title-link" href="/task/view/11274" title="Сделать сайт">x</a>
<p class="task-card__desc">Сайт-визитка для бригады</p><span class="task-chip task-chip--cat">Веб-разработка и IT</span>
<span class="task-card__foot-item" title="25.09.2026 19:28"></span>
<div class="task-card__budget">
<span class="bold">3 500 &#8381;</span></div></article>"""

FREELANCEJOB = """<?xml version='1.0' encoding='windows-1251' ?><rss><channel>
<item><title>Сайт-визитка</title><link>https://www.freelancejob.ru/vacancy/77148/</link>
<description>Нужен сайт</description><dc:date>2026-09-24 08:53:05</dc:date></item></channel></rss>"""

TG_HTML = """<div class="tgme_widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="webfrl/4105">
<div class="tgme_widget_message_text js-message_text" dir="auto">Нужен сайт на Tilda<br/>Бюджет 30к <a href="https://t.me/client_one">@client_one</a></div>
<a class="tgme_widget_message_date" href="https://t.me/webfrl/4105"><time datetime="2026-09-17T10:01:01+00:00">x</time></a>
</div></div>"""


class TestParsers:

    def test_kwork(self):
        o = parse_kwork_wants([{"id": 3259146, "name": "Создать лендинг", "description": "<p>Тест</p>",
                                "priceLimit": "25000.00", "possiblePriceLimit": 50000,
                                "date_create": "2026-09-25 16:46:07"}])[0]
        assert o.uid == "kwork:3259146"
        assert o.url == "https://kwork.ru/projects/3259146/view"
        assert o.budget == "до 25 000 ₽ (допустимо 50 000 ₽)"
        assert o.description == "Тест"

    def test_fl(self):
        orders = parse_fl_rss(FL_RSS)
        assert len(orders) == 1
        o = orders[0]
        assert (o.id, o.title, o.budget) == ("5523338", "Лендинг для школы", "15 000 ₽")
        assert o.description == "Нужен лендинг на Tilda"

    def test_freelance_ru(self):
        o = parse_freelance_ru(FREELANCE_RU)[0]
        assert (o.id, o.title, o.budget, o.published) == ("11274", "Сделать сайт", "3 500 ₽", "25.09.2026 19:28")
        assert o.description.startswith("[Веб-разработка и IT]")

    def test_freelancejob(self):
        o = parse_freelancejob_rss(FREELANCEJOB)[0]
        assert (o.id, o.title, o.published) == ("77148", "Сайт-визитка", "2026-09-24 08:53:05")

    def test_tg_channel(self):
        p = parse_tg_channel(TG_HTML)[0]
        assert p["id"] == "4105" and p["url"] == "https://t.me/webfrl/4105"
        assert p["text"].startswith("Нужен сайт на Tilda\nБюджет 30к")
        assert p["links"] == ["https://t.me/client_one"]


class TestMatching:

    def _o(self, title, desc=""):
        return Order(source="x", id="1", title=title, url="u", description=desc)

    def test_title_keyword(self):
        assert matches(self._o("Сделать лендинг для кафе"))

    def test_description_requires_explicit_request(self):
        assert matches(self._o("Задача", "Нужно создать новый сайт компании"))
        assert not matches(self._o("Написать тексты", "Тексты для нашего сайта про мебель"))

    def test_vacancies_and_minus_words_rejected(self):
        assert not matches(self._o("Вакансия: верстальщик сайтов в штат"))
        assert not matches(self._o("Лендинг на Tilda"), minus=["tilda"])
        assert not matches(self._o("Делаю сайты на Tilda недорого"))

    def test_custom_keywords(self):
        assert matches(self._o("Бот для записи", "телеграм бот"), keywords=["бот"])


@pytest_asyncio.fixture
async def db(tmp_path):
    d = Database(path=str(tmp_path / "o.db"))
    await d.connect()
    yield d
    await d.close()


class TestOrdersService:

    @pytest.mark.asyncio
    async def test_poll_sends_only_new_matching(self, db, monkeypatch):
        batches = [
            [Order("kwork", "1", "Создать сайт", "u1"), Order("kwork", "2", "Логотип", "u2")],
            [Order("kwork", "1", "Создать сайт", "u1"), Order("kwork", "3", "Лендинг", "u3")],
        ]
        sent: list[tuple[int, str]] = []

        async def sender(chat_id, text):
            sent.append((chat_id, text))

        svc = OrdersService(db, sender=sender)

        async def fake_fetch_all(sources, channels):
            return batches.pop(0)

        monkeypatch.setattr(svc, "fetch_all", fake_fetch_all)
        await db.set_setting(7, ENABLED, True)

        first = await svc.poll_once()
        assert [o.id for o in first[7]] == ["1"]
        second = await svc.poll_once()
        assert [o.id for o in second[7]] == ["3"]
        assert len(sent) == 2
        assert (await db.get_stats())["orders_seen"] == 3
        assert len(await db.recent_matched_orders()) == 2

    @pytest.mark.asyncio
    async def test_no_subscribers_no_fetch(self, db, monkeypatch):
        svc = OrdersService(db)

        async def boom(*a):
            raise AssertionError("should not fetch")

        monkeypatch.setattr(svc, "fetch_all", boom)
        assert await svc.poll_once() == {}

    def test_format_escapes_html(self):
        text = format_order(Order("kwork", "1", "<b>Сайт</b> & лендинг", "https://k.ru/1", budget="до 5 000 ₽"))
        assert "&lt;b&gt;Сайт&lt;/b&gt; &amp; лендинг" in text and 'href="https://k.ru/1"' in text
