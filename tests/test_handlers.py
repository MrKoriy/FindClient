"""Smoke-тесты слоя handlers: регистрация роутов и чистые хелперы UI.

Полный флоу aiogram здесь не разыгрывается - цель поймать сломанный импорт,
конфликт роутов и регрессии хелперов, которые видит юзер.
"""

from aiogram import Dispatcher

import handlers
from config import Settings
from handlers.common import document, grid, kb, plural
from handlers.orders import _split
from handlers.start import make_crm_auth_view


def test_all_routers_register():
    dp = Dispatcher()
    handlers.register_routers(dp)
    # каждый из семи роутеров внёс свои хендлеры
    assert len(dp.sub_routers) == 7
    # у роутеров есть наблюдатели (message/callback_query), регистрация не пустая
    handled = [r for r in dp.sub_routers if r.message.handlers or r.callback_query.handlers]
    assert len(handled) == 7


def test_make_crm_auth_view_uses_settings_url():
    text, markup = make_crm_auth_view(1, "https://crm.example/")
    assert "Панель FindClient CRM" in text
    web_button, browser_button = markup.inline_keyboard[0]
    assert web_button.web_app.url.startswith("https://crm.example/auth?token=")
    assert browser_button.url and browser_button.url.startswith("https://crm.example/auth?token=")


def test_grid_shape():
    cells = grid([("a", "1"), ("b", "2"), ("c", "3")], cols=2)
    assert len(cells) == 2 and len(cells[0]) == 2 and len(cells[1]) == 1


def test_plural_ru():
    assert plural(1, "заказ", "заказа", "заказов") == "1 заказ"
    assert plural(3, "заказ", "заказа", "заказов") == "3 заказа"
    assert plural(11, "заказ", "заказа", "заказов") == "11 заказов"


def test_split_words():
    assert _split("сайт, лендинг\nбот") == ["сайт", "лендинг", "бот"]


def test_kb_and_document():
    markup = kb([[("Текст", "data")]])
    assert markup.inline_keyboard[0][0].callback_data == "data"
    doc = document(b"bytes", "файл.xlsx")
    assert doc.filename == "файл.xlsx"


def test_settings_crm_url_default():
    s = Settings(BOT_TOKEN="t")
    assert s.CRM_URL.startswith("https://")
