"""/history — past searches with re-download."""

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from db.database import Database
from handlers.common import BACK_TO_MENU, document, kb, plural, safe_edit
from models.organization import Organization
from services.export import ORG_COLUMNS, export_async

router = Router()


async def _history(db: Database):
    sessions = await db.get_history(limit=15)
    if not sessions:
        return "История пуста. Используйте /scrape для сбора контактов.", kb([BACK_TO_MENU])
    lines = ["<b>Последние сборы</b> (нажмите, чтобы скачать снова):\n"]
    rows = []
    for s in sessions:
        city = f", {s['city']}" if s.get("city") else ""
        lines.append(f"• {s['date'][:16]} — {html.escape(s['niche'])}{html.escape(city)} ({s['count']} шт.)")
        rows.append([(f"📥 {s['niche'][:28]}{city[:16]} · {s['count']}", f"hi:get:{s['id']}")])
    return "\n".join(lines), kb(rows + [BACK_TO_MENU])


@router.message(Command("history"))
async def cmd_history(message: Message, db: Database) -> None:
    text, markup = await _history(db)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "menu:history")
async def on_menu_history(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    text, markup = await _history(db)
    await safe_edit(callback, text, markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("hi:get:"))
async def on_download(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    sid = callback.data.split(":")[2]
    rows = await db.get_session_orgs(int(sid))
    if not rows:
        await callback.message.answer("В этой сессии нет компаний.")
        return
    orgs = [Organization(**r) for r in rows]
    orgs.sort(key=lambda o: o.score, reverse=True)
    data, ext = await export_async(orgs, ORG_COLUMNS)
    await callback.message.answer_document(document(data, f"сбор_{sid}.{ext}"),
                                           caption=plural(len(orgs), "компания", "компании", "компаний"),
                                           reply_markup=kb([[("📨 Добавить в рассылку", f"out:pick:{sid}")]]))
