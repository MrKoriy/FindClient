"""Catalog of high-value niches with jump-offs to maps search and Telegram leads."""

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from data.niches import CATEGORIES, NICHES, get_niche, niches_in
from handlers.common import BACK_TO_MENU, document, grid, kb, safe_edit
from handlers.scrape import start_with_niche
from services.export import export

router = Router()

_PRESENCE = {"yes": "хорошо", "partly": "частично", "no": "почти нет — ищите в Telegram"}

_INTRO = (
    "<b>Ниши, где у бизнеса есть деньги на сайт</b>\n"
    "Средний чек от сотен тысяч ₽, оборот — миллионы в месяц, продажи идут через заявки.\n"
    "Где ниши плохо представлены на картах (бригады, прорабы, дизайнеры), используйте Telegram-лиды."
)


def _categories_kb():
    items = [(c, f"ni:cat:{i}") for i, c in enumerate(CATEGORIES)]
    return kb(grid(items, 2) + [[("📥 Весь каталог в таблицу", "ni:export")], BACK_TO_MENU])


@router.message(Command("niches"))
async def cmd_niches(message: Message) -> None:
    await message.answer(_INTRO, reply_markup=_categories_kb(), parse_mode="HTML")


@router.callback_query(F.data == "menu:niches")
async def on_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await safe_edit(callback, _INTRO, _categories_kb(), parse_mode="HTML")


@router.callback_query(F.data.startswith("ni:cat:"))
async def on_category(callback: CallbackQuery) -> None:
    await callback.answer()
    category = CATEGORIES[int(callback.data.split(":")[2])]
    items = [(f"{n.label} · {n.turnover}", f"ni:n:{n.id}") for n in niches_in(category)]
    await safe_edit(callback, f"<b>{html.escape(category)}</b> (оборот типичной компании в месяц):",
                    kb(grid(items, 1) + [[("⬅️ Категории", "menu:niches")]]), parse_mode="HTML")


@router.callback_query(F.data.startswith("ni:n:"))
async def on_niche(callback: CallbackQuery) -> None:
    await callback.answer()
    n = get_niche(callback.data.split(":", 2)[2])
    if not n:
        return
    chats = ", ".join(f"@{c}" for c in n.tg_chats[:6]) or "—"
    text = (
        f"<b>{html.escape(n.label)}</b>\n\n"
        f"💵 Средний чек: {html.escape(n.avg_check)} ₽\n"
        f"📈 Оборот в месяц: {html.escape(n.turnover)} ₽\n"
        f"🎯 Зачем сайт: {html.escape(n.why)}\n"
        f"🗺 На картах: {_PRESENCE.get(n.map_presence, n.map_presence)}\n"
        f"🔍 Запросы: {html.escape('; '.join(n.queries))}\n"
        f"💬 Telegram-чаты: {html.escape(chats)}"
    )
    rows = [[("🔎 Искать на картах", f"ni:maps:{n.id}")]]
    if n.tg_chats or n.tg_keywords:
        rows.append([("👷 Лиды из Telegram", f"tg:niche:{n.id}")])
    rows.append([("⬅️ Назад", f"ni:cat:{CATEGORIES.index(n.category)}")])
    await safe_edit(callback, text, kb(rows), parse_mode="HTML")


@router.callback_query(F.data.startswith("ni:maps:"))
async def on_maps(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    n = get_niche(callback.data.split(":", 2)[2])
    if n:
        await start_with_niche(callback, state, n.label, list(n.queries))


@router.callback_query(F.data == "ni:export")
async def on_export(callback: CallbackQuery) -> None:
    await callback.answer()
    rows = [
        {"category": n.category, "label": n.label, "avg_check": n.avg_check, "turnover": n.turnover,
         "why": n.why, "maps": _PRESENCE.get(n.map_presence, n.map_presence),
         "queries": "; ".join(n.queries), "tg": ", ".join(f"@{c}" for c in n.tg_chats)}
        for n in NICHES
    ]
    cols = [("category", "Категория", 20), ("label", "Ниша", 34), ("avg_check", "Средний чек, ₽", 22),
            ("turnover", "Оборот/мес, ₽", 16), ("why", "Зачем сайт", 60), ("maps", "На картах", 22),
            ("queries", "Запросы для карт", 50), ("tg", "Telegram-чаты", 50)]
    data, ext = export(rows, cols, title="Ниши")
    await callback.message.answer_document(document(data, f"денежные_ниши.{ext}"), caption=f"{len(NICHES)} ниш")
