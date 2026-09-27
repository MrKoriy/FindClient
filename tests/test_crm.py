"""Тесты CRM: схема, шаблоны, лимиты, API.

Отдельно проверяется то, что уже один раз сломалось на живых данных:
согласование числительных и поведение при пустых полях компании.
"""

import importlib.util
import sqlite3

import pytest
import pytest_asyncio

from crm import db as crm_db
from crm import templates as tpl

# Тесты, которые доходят до реальной отправки, требуют Telethon. Где его нет -
# они пропускаются, а не падают: иначе отсутствие зависимости выглядит как
# поломка логики. Там, где Telethon стоит (прод), они действительно прогоняются.
HAS_TELETHON = importlib.util.find_spec("telethon") is not None
needs_telethon = pytest.mark.skipif(
    not HAS_TELETHON, reason="telethon не установлен - проверка отправки пропущена"
)


@pytest_asyncio.fixture
async def crm_path(tmp_path):
    path = str(tmp_path / "crm.db")
    await crm_db.init_db(path)
    return path


@pytest.fixture()
def scraper_path(tmp_path):
    """Мини-копия основной базы бота - только нужная таблица."""
    path = str(tmp_path / "scraper.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE organizations (
            org_id TEXT NOT NULL, session_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '', rating REAL NOT NULL DEFAULT 0.0,
            socials TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
            reviews INTEGER NOT NULL DEFAULT 0, branches INTEGER NOT NULL DEFAULT 0,
            url TEXT NOT NULL DEFAULT '', score INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (org_id, session_id));
    """)
    conn.executemany(
        "INSERT INTO organizations (org_id, session_id, name, phone, website, city,"
        " category, rating, reviews) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("1", 1, "Без сайта", "+78120000001", "", "Санкт-Петербург", "юристы", 5.0, 91),
            ("2", 1, "С сайтом", "+78120000002", "http://x.ru", "Санкт-Петербург", "юристы", 4.0, 10),
            ("3", 1, "Тоже без сайта", "+78120000003", "", "Москва", "стоматология", 4.7, 33),
        ],
    )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------- шаблоны

def test_plural_agrees_with_number():
    assert tpl.plural(1, "отзыв", "отзыва", "отзывов") == "отзыв"
    assert tpl.plural(2, "отзыв", "отзыва", "отзывов") == "отзыва"
    assert tpl.plural(5, "отзыв", "отзыва", "отзывов") == "отзывов"
    assert tpl.plural(11, "отзыв", "отзыва", "отзывов") == "отзывов"
    assert tpl.plural(21, "отзыв", "отзыва", "отзывов") == "отзыв"
    assert tpl.plural(91, "отзыв", "отзыва", "отзывов") == "отзыв"
    assert tpl.plural(112, "отзыв", "отзыва", "отзывов") == "отзывов"


def test_facts_handles_missing_halves():
    assert tpl.facts_phrase({"rating": 5.0, "reviews": 91}) == "5,0 и 91 отзыв"
    assert tpl.facts_phrase({"rating": 5.0, "reviews": 0}) == "5,0"
    assert tpl.facts_phrase({"rating": 0, "reviews": 3}) == "3 отзыва"
    assert tpl.facts_phrase({"rating": 0, "reviews": 0}) == ""


def test_facts_hides_low_rating():
    """Хвалить рейтинг 1,0 нельзя - это работает против нас."""
    assert tpl.facts_phrase({"rating": 1.0, "reviews": 2}) == "2 отзыва"
    assert tpl.facts_phrase({"rating": 3.9, "reviews": 10}) == "10 отзывов"
    assert tpl.facts_phrase({"rating": 4.0, "reviews": 10}) == "4,0 и 10 отзывов"
    assert tpl.facts_phrase({"rating": 1.0, "reviews": 0}) == ""


def test_render_with_empty_target_has_no_debris():
    """Пустая карточка не должна давать «Нашёл вас на картах: .» или двойных пробелов."""
    for template in tpl.DEFAULT_TEMPLATES:
        text = tpl.render(template["body"], {}, link="", with_link=False)
        assert "  " not in text, text
        assert not any(x in text for x in (": .", ": ,", " .", " ,", "и .", "{", "}")), text


def test_render_strips_link_sentence():
    body = tpl.DEFAULT_TEMPLATES[2]["body"]
    without = tpl.render(body, {}, link="https://x.ru", with_link=False)
    assert "https://" not in without
    assert "Спасибо, что ответили" in without
    with_link = tpl.render(body, {}, link="https://x.ru", with_link=True)
    assert "https://x.ru" in with_link


def test_default_templates_pass_their_own_check():
    for template in tpl.DEFAULT_TEMPLATES:
        problems = tpl.check(template["body"], with_link="{link}" in template["body"])
        assert problems == [], f"{template['name']}: {problems}"


def test_check_catches_case_mismatch():
    problems = tpl.check("Здравствуйте! Ищу клиентов в {city}.")
    assert any("падеж" in p for p in problems)


def test_check_catches_price_and_questions():
    problems = tpl.check("Здравствуйте! Сайт стоит 30000 руб. Брать будете? Или нет?")
    assert any("прайс" in p for p in problems)
    assert any("вопрос" in p for p in problems)


def test_check_catches_word_limit():
    problems = tpl.check("Здравствуйте! " + "слово " * 60)
    assert any("лимит" in p for p in problems)


def test_check_catches_unknown_placeholder():
    problems = tpl.check("Здравствуйте, {неизвестно}!")
    assert any("неизвестные переменные" in p for p in problems)


def test_check_does_not_flag_hyphenated_words():
    """Дефис в «сайт-квиз» - это не длинное тире, замечание ложно."""
    problems = tpl.check("Здравствуйте! Делаю сайт-квиз за три дня. Взглянете?")
    assert "длинное тире" not in problems


def test_check_flags_long_dashes():
    for dash in ("\u2014", "\u2013"):
        problems = tpl.check(f"Здравствуйте! Мы{dash} делаем сайты. Взглянете?")
        assert "длинное тире" in problems, dash


def test_cleanup_normalizes_long_dashes():
    assert tpl.cleanup("мы \u2014 делаем сайты") == "мы - делаем сайты"
    assert tpl.cleanup("мы \u2013 делаем сайты") == "мы - делаем сайты"
    assert tpl.cleanup("сайт-квиз") == "сайт-квиз"


def test_pick_variant_changes_greeting_deterministically():
    body = tpl.DEFAULT_TEMPLATES[0]["body"]
    variants = {tpl.pick_variant(body, seed) for seed in range(30)}
    assert len(variants) > 1
    assert tpl.pick_variant(body, "x") == tpl.pick_variant(body, "x")


# ---------------------------------------------------------------- БД и цели

@pytest.mark.asyncio
async def test_init_db_is_idempotent(crm_path):
    await crm_db.init_db(crm_path)
    await crm_db.init_db(crm_path)
    async with crm_db.connect(crm_path) as conn:
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in await cur.fetchall()}
    assert {"targets", "messages", "templates", "groups", "settings"} <= tables


@pytest.mark.asyncio
async def test_import_targets_skips_companies_with_website(crm_path, scraper_path):
    added = await crm_db.import_targets(scraper_path, crm_path)
    assert added == 3
    counts = await crm_db.count_targets(crm_path)
    assert counts["new"] == 2, "две компании без сайта должны стать рабочими целями"
    assert counts["skip"] == 1, "компания с сайтом помечается skip"
    assert counts["all"] == 3


@pytest.mark.asyncio
async def test_import_targets_is_idempotent(crm_path, scraper_path):
    await crm_db.import_targets(scraper_path, crm_path)
    again = await crm_db.import_targets(scraper_path, crm_path)
    assert again == 0
    assert (await crm_db.count_targets(crm_path))["all"] == 3


@pytest.mark.asyncio
async def test_import_targets_reports_missing_db(crm_path):
    with pytest.raises(FileNotFoundError):
        await crm_db.import_targets("/nope/scraper.db", crm_path)


@pytest.mark.asyncio
async def test_set_target_status_rejects_unknown(crm_path, scraper_path):
    await crm_db.import_targets(scraper_path, crm_path)
    target = (await crm_db.list_targets(status="new", crm_db=crm_path))[0]
    with pytest.raises(ValueError):
        await crm_db.set_target_status(target["id"], "выдуманный", crm_db=crm_path)


@pytest.mark.asyncio
async def test_target_filters(crm_path, scraper_path):
    await crm_db.import_targets(scraper_path, crm_path)
    spb = await crm_db.list_targets(city="Санкт-Петербург", crm_db=crm_path)
    assert len(spb) == 2
    found = await crm_db.list_targets(search="стоматология", crm_db=crm_path)
    assert len(found) == 1 and found[0]["name"] == "Тоже без сайта"


# ---------------------------------------------------------------- очередь

@pytest.mark.asyncio
async def test_queue_requires_recipient(crm_path, scraper_path):
    """Цель без юзернейма в личку не поставить: Телеграм по телефону не пишет."""
    await crm_db.import_targets(scraper_path, crm_path)
    target = (await crm_db.list_targets(status="new", crm_db=crm_path))[0]
    assert target["username"] == ""

    await crm_db.save_target_username(target["id"], "@someuser", crm_db=crm_path)
    assert (await crm_db.get_target(target["id"], crm_path))["username"] == "someuser"


@pytest.mark.asyncio
async def test_queue_message_moves_target_to_queued(crm_path, scraper_path):
    await crm_db.import_targets(scraper_path, crm_path)
    target = (await crm_db.list_targets(status="new", crm_db=crm_path))[0]
    await crm_db.queue_message(target["id"], "someuser", "Здравствуйте!", crm_db=crm_path)

    assert (await crm_db.get_target(target["id"], crm_path))["status"] == "queued"
    queued = await crm_db.next_queued(crm_path)
    assert queued["chat"] == "someuser"


@pytest.mark.asyncio
async def test_mark_message_sent_updates_target_and_counter(crm_path, scraper_path):
    await crm_db.import_targets(scraper_path, crm_path)
    target = (await crm_db.list_targets(status="new", crm_db=crm_path))[0]
    mid = await crm_db.queue_message(target["id"], "someuser", "Привет", crm_db=crm_path)

    assert await crm_db.sent_today(crm_path) == 0
    await crm_db.mark_message(mid, "sent", tg_id=42, crm_db=crm_path)

    assert await crm_db.sent_today(crm_path) == 1
    assert (await crm_db.get_target(target["id"], crm_path))["status"] == "sent"
    assert await crm_db.next_queued(crm_path) is None


@pytest.mark.asyncio
async def test_mark_message_failed_does_not_count_as_sent(crm_path):
    mid = await crm_db.queue_message(None, "ghost", "текст", crm_db=crm_path)
    await crm_db.mark_message(mid, "failed", "FloodWait 900 с", crm_db=crm_path)
    assert await crm_db.sent_today(crm_path) == 0
    assert (await crm_db.summary(crm_path))["failed"] == 1


@pytest.mark.asyncio
async def test_summary_counts_left_today(crm_path):
    await crm_db.set_settings({"daily_cap": "3"}, crm_path)
    for i in range(2):
        mid = await crm_db.queue_message(None, f"u{i}", "текст", crm_db=crm_path)
        await crm_db.mark_message(mid, "sent", crm_db=crm_path)
    s = await crm_db.summary(crm_path)
    assert s["sent_today"] == 2
    assert s["left_today"] == 1
    assert s["daily_cap"] == 3


@pytest.mark.asyncio
async def test_summary_never_goes_negative(crm_path):
    await crm_db.set_settings({"daily_cap": "1"}, crm_path)
    for i in range(4):
        mid = await crm_db.queue_message(None, f"u{i}", "текст", crm_db=crm_path)
        await crm_db.mark_message(mid, "sent", crm_db=crm_path)
    assert (await crm_db.summary(crm_path))["left_today"] == 0


# ---------------------------------------------------------------- группы

@pytest.mark.asyncio
async def test_import_groups_and_mark_posted(crm_path):
    from data.niches import NICHES

    added = await crm_db.import_groups(crm_path, niches_module=NICHES)
    assert added > 0
    groups = await crm_db.list_groups(crm_db=crm_path)
    assert len(groups) == added

    await crm_db.mark_group_posted(groups[0]["id"], "опубликовано вручную", crm_path)
    posted = [g for g in await crm_db.list_groups(crm_db=crm_path) if g["posted_at"]]
    assert len(posted) == 1
    assert posted[0]["posts"] == 1
    assert (await crm_db.summary(crm_path))["groups_posted"] == 1


@pytest.mark.asyncio
async def test_import_groups_is_idempotent(crm_path):
    from data.niches import NICHES

    first = await crm_db.import_groups(crm_path, niches_module=NICHES)
    second = await crm_db.import_groups(crm_path, niches_module=NICHES)
    assert first > 0 and second == 0


# ---------------------------------------------------------------- настройки

@pytest.mark.asyncio
async def test_settings_defaults_and_override(crm_path):
    settings = await crm_db.get_settings(crm_path)
    assert settings["daily_cap"] == "10"
    assert settings["enabled"] == "0", "отправка по умолчанию выключена"
    assert settings["dry_run"] == "1", "по умолчанию холостой ход"

    await crm_db.set_settings({"daily_cap": "5"}, crm_path)
    assert (await crm_db.get_settings(crm_path))["daily_cap"] == "5"
    assert (await crm_db.get_settings(crm_path))["enabled"] == "0"


@pytest.mark.asyncio
async def test_settings_survive_reinit(crm_path):
    await crm_db.set_settings({"daily_cap": "7"}, crm_path)
    await crm_db.init_db(crm_path)
    assert (await crm_db.get_settings(crm_path))["daily_cap"] == "7"


# ---------------------------------------------------------------- воркер

class FakeClient:
    """Заглушка: если воркер в холостом режиме попробует отправить - тест упадёт."""

    def __init__(self):
        self.sent = []

    async def get_entity(self, name):
        return name

    async def send_message(self, entity, body):
        self.sent.append((entity, body))
        return type("M", (), {"id": 1})()


@pytest.mark.asyncio
async def test_sender_does_nothing_when_disabled(crm_path):

    from crm import sender

    await crm_db.queue_message(None, "ghost", "текст", crm_db=crm_path)
    assert await sender.run_once(FakeClient(), crm_path) is False


@pytest.mark.asyncio
async def test_sender_dry_run_does_not_send(crm_path):

    from crm import sender

    await crm_db.set_settings(
        {"enabled": "1", "dry_run": "1", "work_from": "0", "work_to": "24"}, crm_path
    )
    await crm_db.queue_message(None, "someone", "текст", crm_db=crm_path)

    client = FakeClient()
    assert await sender.run_once(client, crm_path) is True
    assert client.sent == [], "в холостом режиме отправлять нельзя"
    assert await crm_db.sent_today(crm_path) == 0
    assert (await crm_db.list_messages(crm_db=crm_path))[0]["status"] == "skipped"


@needs_telethon
@pytest.mark.asyncio
async def test_sender_respects_daily_cap(crm_path):

    from crm import sender

    await crm_db.set_settings(
        {"enabled": "1", "dry_run": "0", "work_from": "0", "work_to": "24",
         "daily_cap": "2", "min_delay": "0", "max_delay": "0"}, crm_path
    )
    for i in range(5):
        await crm_db.queue_message(None, f"user{i}", "текст", crm_db=crm_path)

    client = FakeClient()
    while await sender.run_once(client, crm_path):
        pass
    assert len(client.sent) == 2, "больше дневного лимита уходить не должно"
    assert await crm_db.sent_today(crm_path) == 2


@pytest.mark.asyncio
async def test_sender_skips_outside_work_hours(crm_path):

    from crm import sender

    # Окно, которое гарантированно не содержит текущий час ни в одном поясе.
    await crm_db.set_settings(
        {"enabled": "1", "dry_run": "0", "work_from": "0", "work_to": "0"}, crm_path
    )
    await crm_db.queue_message(None, "someone", "текст", crm_db=crm_path)
    assert await sender.run_once(FakeClient(), crm_path) is False
    assert await crm_db.sent_today(crm_path) == 0


@needs_telethon
@pytest.mark.asyncio
async def test_sender_marks_failure_and_keeps_going(crm_path):

    from crm import sender

    await crm_db.set_settings(
        {"enabled": "1", "dry_run": "0", "work_from": "0", "work_to": "24",
         "daily_cap": "10", "min_delay": "0", "max_delay": "0"}, crm_path
    )
    await crm_db.queue_message(None, "", "текст", crm_db=crm_path)

    assert await sender.run_once(FakeClient(), crm_path) is True
    message = (await crm_db.list_messages(crm_db=crm_path))[0]
    assert message["status"] == "failed"
    assert "получател" in message["error"]


# ---------------------------------------------------------------- генератор офферов и Jev Judge

def test_offer_strategies_list():
    from crm import offer

    strategies = offer.get_strategies()
    assert len(strategies) >= 4
    ids = {s["id"] for s in strategies}
    assert {"lost_traffic", "social_only", "ready_concept", "conversion_quiz"} <= ids


@pytest.mark.asyncio
async def test_generate_offer_all_strategies_score_well():
    from crm import offer

    sample = {
        "name": "СтройМастер",
        "rating": 4.8,
        "reviews": 42,
        "category": "ремонт квартир",
        "city": "Москва",
    }
    for strat in offer.get_strategies():
        res = await offer.generate_offer(sample, strategy_id=strat["id"])
        text = res["text"]
        cls = res["classification"]
        assert len(text) > 20
        assert cls["score"] >= 80, f"Стратегия {strat['id']} набрала всего {cls['score']}: {cls['recommendations']}"
        assert cls["verdict"] in ("excellent", "good")


@pytest.mark.asyncio
async def test_classify_penalizes_price_and_long_dash():
    from crm import offer

    bad_text = (
        "Здравствуйте! Предлагаем сайт за 45000 руб. со скидкой \u2014 сделаем быстро. "
        "Интересно? Или перезвонить позже?"
    )
    res = await offer.classify_offer(bad_text)
    assert res["score"] < 70
    assert any("цен" in r.lower() or "прайс" in r.lower() for r in res["recommendations"])
    assert any("тире" in r.lower() for r in res["recommendations"])
    assert any("вопрос" in r.lower() for r in res["recommendations"])
    assert res["checks"]["no_em_dash"] is False
    assert res["checks"]["no_price"] is False
    assert res["checks"]["single_question"] is False


@pytest.mark.asyncio
async def test_classify_penalizes_excess_words():
    from crm import offer

    long_text = "Здравствуйте! " + "слово " * 55 + "Есть смысл обсудить?"
    res = await offer.classify_offer(long_text)
    assert any("слов" in r.lower() for r in res["recommendations"])
    assert res["checks"]["under_word_limit"] is False


@pytest.mark.asyncio
async def test_auto_improve_offer_fixes_flaws():
    from crm import offer

    flawed = "Здравствуйте! Сайт стоит 30000 руб - сделаем быстро. Показать? Или перезвонить?"
    improved = await offer.auto_improve_offer(flawed)
    # Спейс-дефис - след заменённого длинного тире; дефис внутри «демо-версию» законен.
    assert " - " not in improved
    assert "30000" not in improved
    assert improved.count("?") == 1
    res = await offer.classify_offer(improved)
    assert res["checks"]["no_em_dash"] is True
    assert res["checks"]["single_question"] is True


@pytest.mark.asyncio
async def test_offer_api_endpoints(crm_path, scraper_path, monkeypatch):
    import base64

    from aiohttp.test_utils import TestClient, TestServer

    from crm import app as crm_app

    monkeypatch.setattr(crm_app, "USER", "admin")
    monkeypatch.setattr(crm_app, "PASSWORD", "secret")
    monkeypatch.setattr(crm_db, "DEFAULT_CRM_DB", crm_path)
    await crm_db.import_targets(scraper_path, crm_path)

    async def _run():
        app = crm_app.create_app()
        auth = base64.b64encode(b"admin:secret").decode("utf-8")
        headers = {"Authorization": f"Basic {auth}"}

        async with TestClient(TestServer(app), headers=headers) as client:
            # 1. Strategies list
            r = await client.get("/api/offer/strategies")
            assert r.status == 200
            data = await r.json()
            assert len(data["items"]) >= 4

            # 2. Generate
            target = (await crm_db.list_targets(crm_db=crm_path))[0]
            r = await client.post("/api/offer/generate", json={
                "target_id": target["id"],
                "strategy_id": "lost_traffic",
            })
            assert r.status == 200
            gen_data = await r.json()
            assert "text" in gen_data
            assert gen_data["classification"]["score"] >= 80

            # 3. Classify
            r = await client.post("/api/offer/classify", json={
                "text": gen_data["text"],
                "target_id": target["id"],
            })
            assert r.status == 200
            cls_data = await r.json()
            assert cls_data["score"] >= 80

            # 4. Improve
            r = await client.post("/api/offer/improve", json={
                "text": "Здравствуйте! Сайт 50000 руб - сделаем быстро. Купите? Или созвонимся?",
                "target_id": target["id"],
            })
            assert r.status == 200
            imp_data = await r.json()
            assert "\u2014" not in imp_data["text"]
            assert imp_data["classification"]["checks"]["no_em_dash"] is True

            # 5. Supported Models
            r = await client.get("/api/models")
            assert r.status == 200
            models_data = await r.json()
            model_ids = [m["id"] for m in models_data.get("items", [])]
            assert "qwen3.8-flash" in model_ids
            assert "deepseek-v4.1-flash" in model_ids

    await _run()


def test_bai_clean_human_output():
    from crm import bai

    raw_quoted = '«Приветствую! Мы разрабатываем сайты \u2014 быстро и качественно. Взглянете?»'
    cleaned = bai.clean_human_output(raw_quoted)
    assert "\u2014" not in cleaned
    assert not cleaned.startswith('«')
    assert not cleaned.endswith('»')
    assert "сайты - быстро" in cleaned

def test_magic_token_and_session_cookie():
    from crm import auth

    secret = "test-secret-key-12345"
    uid = 1432816193

    token = auth.generate_magic_token(uid, secret=secret)
    assert token
    assert auth.verify_magic_token(token, secret=secret) == uid

    # Invalid token or wrong secret
    assert auth.verify_magic_token(token, secret="wrong-secret") is None
    assert auth.verify_magic_token("corrupted.token", secret=secret) is None

    # Session cookie
    cookie = auth.create_session_cookie(uid, secret=secret)
    assert cookie
    assert auth.verify_session_cookie(cookie, secret=secret) == uid
    assert auth.verify_session_cookie(cookie, secret="wrong-secret") is None


@pytest.mark.asyncio
async def test_crm_auth_flow(crm_path, monkeypatch):

    from aiohttp.test_utils import TestClient, TestServer

    from crm import app as crm_app
    from crm import auth

    monkeypatch.setattr(crm_app, "USER", "admin")
    monkeypatch.setattr(crm_app, "PASSWORD", "secret")
    monkeypatch.setattr(crm_db, "DEFAULT_CRM_DB", crm_path)

    async def _run():
        app = crm_app.create_app()
        async with TestClient(TestServer(app)) as client:
            # 1. Without auth -> 401
            r = await client.get("/")
            assert r.status == 401

            # 2. Via /auth with magic token -> 302 Found and cookie set
            token = auth.generate_magic_token(1432816193)
            r = await client.get(f"/auth?token={token}", allow_redirects=False)
            assert r.status == 302
            assert "crm_session" in client.session.cookie_jar.filter_cookies(client.make_url("/"))

            # 3. Subsequent request with session cookie -> 200 OK
            r = await client.get("/api/summary")
            assert r.status == 200
            data = await r.json()
            assert "targets" in data

            # 4. Direct request with ?token=... param -> 200 OK
            client.session.cookie_jar.clear()
            token2 = auth.generate_magic_token(1432816193)
            r = await client.get(f"/?token={token2}")
            assert r.status == 200

            # 5. Request with X-CRM-Token header -> 200 OK
            client.session.cookie_jar.clear()
            token3 = auth.generate_magic_token(1432816193)
            r = await client.get("/api/summary", headers={"X-CRM-Token": token3})
            assert r.status == 200

            # 6. Logout
            r = await client.get("/auth/logout", allow_redirects=False)
            assert r.status == 302

    await _run()




@pytest.mark.asyncio
async def test_panel_responsive_during_slow_ai_call(crm_path, monkeypatch):
    """Панель отвечает на запросы, пока генератор оффера ждёт внешний API.

    Раньше urllib.request из хендлера блокировал цикл на минуты: сводка
    не отвечала, UI зависал. Теперь вызов асинхронный - это и проверяем.
    """
    import asyncio
    import base64

    from aiohttp.test_utils import TestClient, TestServer

    from crm import app as crm_app
    from crm import offer as offer_mod

    monkeypatch.setattr(crm_app, "USER", "admin")
    monkeypatch.setattr(crm_app, "PASSWORD", "secret")
    monkeypatch.setattr(crm_db, "DEFAULT_CRM_DB", crm_path)

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_improve(*args, **kwargs):
        started.set()
        await release.wait()
        return "Здравствуйте! Улучшенный текст без спама. Взглянете?"

    monkeypatch.setattr(offer_mod, "auto_improve_offer", slow_improve)

    auth = base64.b64encode(b"admin:secret").decode("utf-8")
    headers = {"Authorization": f"Basic {auth}"}

    app = crm_app.create_app()
    async with TestClient(TestServer(app), headers=headers) as client:
        improve = asyncio.create_task(
            client.post("/api/offer/improve", json={"text": "текст"})
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        r = await asyncio.wait_for(client.get("/api/summary"), timeout=1)
        assert r.status == 200
        release.set()
        resp = await improve
        assert resp.status == 200
        data = await resp.json()
        assert data["text"].startswith("Здравствуйте!")


@pytest.mark.asyncio
async def test_non_numeric_params_return_400_not_500(crm_path, monkeypatch):
    import base64

    from aiohttp.test_utils import TestClient, TestServer

    from crm import app as crm_app

    monkeypatch.setattr(crm_app, "USER", "admin")
    monkeypatch.setattr(crm_app, "PASSWORD", "secret")
    monkeypatch.setattr(crm_db, "DEFAULT_CRM_DB", crm_path)

    auth = base64.b64encode(b"admin:secret").decode("utf-8")
    headers = {"Authorization": f"Basic {auth}"}
    app = crm_app.create_app()
    async with TestClient(TestServer(app), headers=headers) as client:
        for url in ("/api/targets?limit=abc", "/api/messages?limit=zz", "/api/targets?offset=x"):
            r = await client.get(url)
            assert r.status == 400, url


# ---------------------------------------------------------------- ЛПР в шаблонах

def test_director_address_forms():
    assert tpl.director_address("Овечкина Мария Вячеславовна (директор)") == "Мария Вячеславовна"
    assert tpl.director_address("Иванов Пётр Сидорович") == "Пётр Сидорович"
    assert tpl.director_address("Ахметов Рустам") == "Рустам"
    assert tpl.director_address("") == ""
    assert tpl.director_address(None) == ""


def test_render_personalizes_greeting():
    target = {"name": "Мадин", "reviews": 91, "rating": 5.0, "director": "Овечкина Мария Вячеславовна (директор)"}
    text = tpl.render(tpl.DEFAULT_TEMPLATES[0]["body"], target)
    assert text.startswith("Здравствуйте, Мария Вячеславовна!")
    assert "{" not in text and "Овечкина" not in text


def test_render_degrades_without_director():
    """Нет ЛПР - обычное «Здравствуйте!», без висячей запятой и мусора."""
    text = tpl.render(tpl.DEFAULT_TEMPLATES[0]["body"], {"name": "Мадин", "reviews": 5})
    assert text.startswith("Здравствуйте!")
    assert ", !" not in text and "  " not in text


def test_cleanup_drops_dangling_comma():
    assert tpl.cleanup("Здравствуйте, !") == "Здравствуйте!"
    assert tpl.cleanup("Привет, .") == "Привет."
    # живой текст с обращением не трогаем
    assert tpl.cleanup("Здравствуйте, Мария!") == "Здравствуйте, Мария!"


def test_pick_variant_keeps_comma_greeting():
    body = "Здравствуйте, {director}! Текст сообщения. Есть смысл?"
    v1 = tpl.pick_variant(body, "a")
    v2 = tpl.pick_variant(body, "b")
    assert {v1, v2} == {
        "Здравствуйте, {director}! Текст сообщения. Есть смысл?",
        "Добрый день, {director}! Текст сообщения. Есть смысл?",
    }
