"""Freelance orders: subscription, sources, keywords, manual check and export."""

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from api.orders import DEFAULT_KEYWORDS, DEFAULT_TG_CHANNELS
from db.database import Database
from handlers.common import BACK_TO_MENU, check, document, kb, plural, safe_edit
from services.export import ORDER_COLUMNS, export
from services.orders_service import (
    ALL_SOURCES,
    ENABLED,
    KEYWORDS,
    MINUS,
    SOURCES_KEY,
    TG_CHANNELS,
    OrdersService,
    format_order,
)

router = Router()


class OrdersStates(StatesGroup):
    keywords = State()
    minus = State()
    channels = State()


async def _panel(chat_id: int, orders_service: OrdersService, db: Database) -> tuple[str, object]:
    cfg = await orders_service.chat_config(chat_id)
    counts = await db.count_orders_by_source()
    lines = [
        "<b>💼 Автоматический поиск заказов</b>",
        f"Статус: {'🟢 включён' if cfg['enabled'] else '⚪️ выключен'} "
        f"(проверка каждые {orders_service.interval // 60} мин)",
        "",
        "Источники: " + ", ".join(ALL_SOURCES[s] for s in cfg["sources"] if s in ALL_SOURCES),
        f"Ключевые слова ({len(cfg['keywords'])}): {html.escape(', '.join(cfg['keywords'][:12]))}"
        + ("…" if len(cfg["keywords"]) > 12 else ""),
        f"Минус-слова: {html.escape(', '.join(cfg['minus'])) or '—'}",
        f"Telegram-каналы: {html.escape(', '.join('@' + c for c in cfg['tg_channels']))}",
    ]
    if counts:
        lines.append("Просмотрено заказов: " + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    if "tg_groups" in cfg["sources"] and not orders_service.tg_groups_fetcher:
        lines.append("\nℹ️ Telegram-чаты строителей и др. отслеживаются только с аккаунтом (TG_SESSION).")
    for src, err in orders_service.last_errors.items():
        lines.append(f"⚠️ {src}: {html.escape(err[:120])}")
    lines.append(
        "\n<i>Avito и Хабр Фриланс автоматически читать нельзя: Хабр закрыт, "
        "Avito блокирует серверные запросы, а его API не ищет чужие объявления.</i>"
    )
    markup = kb([
        [("⏸ Выключить" if cfg["enabled"] else "▶️ Включить", "or:toggle")],
        [(f"{check(s in cfg['sources'])} {label}", f"or:src:{s}") for s, label in list(ALL_SOURCES.items())[:3]],
        [(f"{check(s in cfg['sources'])} {label}", f"or:src:{s}") for s, label in list(ALL_SOURCES.items())[3:]],
        [("🔑 Ключевые слова", "or:kw"), ("🚫 Минус-слова", "or:minus")],
        [("📣 Telegram-каналы", "or:channels")],
        [("🔄 Проверить сейчас", "or:check"), ("📥 В таблицу", "or:export")],
        BACK_TO_MENU,
    ])
    return "\n".join(lines), markup


@router.message(Command("orders"))
async def cmd_orders(message: Message, orders_service: OrdersService, db: Database) -> None:
    text, markup = await _panel(message.chat.id, orders_service, db)
    await message.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data == "menu:orders")
async def on_menu(callback: CallbackQuery, orders_service: OrdersService, db: Database, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    text, markup = await _panel(callback.message.chat.id, orders_service, db)
    await safe_edit(callback, text, markup, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data == "or:toggle")
async def on_toggle(callback: CallbackQuery, orders_service: OrdersService, db: Database) -> None:
    chat_id = callback.message.chat.id
    enabled = not await db.get_setting(chat_id, ENABLED, False)
    await db.set_setting(chat_id, ENABLED, enabled)
    await callback.answer("Включено — новые заказы будут приходить сюда" if enabled else "Выключено")
    text, markup = await _panel(chat_id, orders_service, db)
    await safe_edit(callback, text, markup, parse_mode="HTML", disable_web_page_preview=True)
    if enabled:
        await orders_service.poll_once(only_chat=chat_id)


@router.callback_query(F.data.startswith("or:src:"))
async def on_source(callback: CallbackQuery, orders_service: OrdersService, db: Database) -> None:
    await callback.answer()
    chat_id = callback.message.chat.id
    src = callback.data.split(":", 2)[2]
    cfg = await orders_service.chat_config(chat_id)
    sources = [s for s in cfg["sources"] if s != src] if src in cfg["sources"] else cfg["sources"] + [src]
    await db.set_setting(chat_id, SOURCES_KEY, sources)
    text, markup = await _panel(chat_id, orders_service, db)
    await safe_edit(callback, text, markup, parse_mode="HTML", disable_web_page_preview=True)


_PROMPTS = {
    "kw": (OrdersStates.keywords, "Пришлите ключевые слова через запятую (заказ подходит, если слово есть в заголовке).\n"
           "Например: <i>сайт, лендинг, tilda, интернет-магазин</i>\nНапишите «сброс» для стандартного списка."),
    "minus": (OrdersStates.minus, "Пришлите минус-слова через запятую (заказы с ними пропускаются).\n"
              "Например: <i>seo, копирайт, 1с</i>\nНапишите «сброс», чтобы очистить."),
    "channels": (OrdersStates.channels, "Пришлите публичные Telegram-каналы с заказами через запятую "
                 "(@name или ссылку t.me/name).\nНапишите «сброс» для стандартного списка."),
}


@router.callback_query(F.data.in_({"or:kw", "or:minus", "or:channels"}))
async def on_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    st, prompt = _PROMPTS[callback.data.split(":")[1]]
    await state.set_state(st)
    await safe_edit(callback, prompt, kb([[("⬅️ Отмена", "menu:orders")]]), parse_mode="HTML")


def _split(text: str) -> list[str]:
    return [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()]


@router.message(OrdersStates.keywords, F.text, ~F.text.startswith("/"))
async def on_keywords(message: Message, state: FSMContext, orders_service: OrdersService, db: Database) -> None:
    words = list(DEFAULT_KEYWORDS) if message.text.strip().lower() == "сброс" else _split(message.text.lower())
    await db.set_setting(message.chat.id, KEYWORDS, words)
    await state.clear()
    await cmd_orders(message, orders_service, db)


@router.message(OrdersStates.minus, F.text, ~F.text.startswith("/"))
async def on_minus(message: Message, state: FSMContext, orders_service: OrdersService, db: Database) -> None:
    words = [] if message.text.strip().lower() == "сброс" else _split(message.text.lower())
    await db.set_setting(message.chat.id, MINUS, words)
    await state.clear()
    await cmd_orders(message, orders_service, db)


@router.message(OrdersStates.channels, F.text, ~F.text.startswith("/"))
async def on_channels(message: Message, state: FSMContext, orders_service: OrdersService, db: Database) -> None:
    if message.text.strip().lower() == "сброс":
        chans = list(DEFAULT_TG_CHANNELS)
    else:
        chans = [c.split("t.me/")[-1].lstrip("@").strip("/") for c in _split(message.text)]
    await db.set_setting(message.chat.id, TG_CHANNELS, [c for c in chans if c])
    await state.clear()
    await cmd_orders(message, orders_service, db)


@router.callback_query(F.data == "or:check")
async def on_check(callback: CallbackQuery, orders_service: OrdersService, db: Database) -> None:
    await callback.answer("Проверяю источники…")
    chat_id = callback.message.chat.id
    status = await callback.message.answer("🔄 Проверяю биржи и каналы…")
    cfg = await orders_service.chat_config(chat_id)
    if not cfg["enabled"]:
        # One-off check without subscribing: show matches without marking the feed consumed.
        from api.orders import matches
        orders = await orders_service.fetch_all(set(cfg["sources"]), set(cfg["tg_channels"]))
        hits = [o for o in orders if matches(o, cfg["keywords"], cfg["minus"])][:10]
        await status.edit_text(f"Найдено подходящих заказов: {len(hits)} (показываю до 10). "
                               "Включите автопоиск, чтобы получать новые автоматически.")
        for o in hits:
            await callback.message.answer(format_order(o), parse_mode="HTML", disable_web_page_preview=True)
        return
    sent = await orders_service.poll_once(only_chat=chat_id)
    n = len(sent.get(chat_id, []))
    await status.edit_text(f"Готово. Новых подходящих заказов: {n}." if n else "Новых подходящих заказов пока нет.")


@router.callback_query(F.data == "or:export")
async def on_export(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    rows = await db.recent_matched_orders(limit=500)
    if not rows:
        await callback.message.answer("Пока нет сохранённых подходящих заказов.")
        return
    data, ext = export(rows, ORDER_COLUMNS, title="Заказы")
    await callback.message.answer_document(document(data, f"заказы.{ext}"), caption=plural(len(rows), "заказ", "заказа", "заказов"))
