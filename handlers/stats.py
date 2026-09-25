"""/stats — overall collection statistics."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from db.database import Database
from handlers.common import BACK_TO_MENU, kb, safe_edit

router = Router()


def _pct(part: int, total: int) -> int:
    return round(part * 100 / total) if total else 0


async def _format_stats(db: Database) -> str:
    s = await db.get_stats()
    total = s["total_orgs"]
    lines = [
        "Общая статистика:\n",
        f"  Всего сессий: {s['total_sessions']}",
        f"  Уникальных организаций: {total}",
    ]
    if total:
        lines += [
            f"  С телефоном: {s['with_phone']} ({_pct(s['with_phone'], total)}%)",
            f"  С email: {s['with_email']} ({_pct(s['with_email'], total)}%)",
            f"  С сайтом: {s['with_website']} ({_pct(s['with_website'], total)}%)",
            f"  Без сайта: {s['without_website']} ({_pct(s['without_website'], total)}%)",
        ]
    lines += [
        f"  Лидов из Telegram: {s['tg_leads']}",
        f"  Заказов просмотрено: {s['orders_seen']}, подходящих: {s['orders_matched']}",
    ]
    if s["top_niches"]:
        lines.append("\nТоп ниши:")
        for i, n in enumerate(s["top_niches"], 1):
            lines.append(f"  {i}. {n['niche']} — {n['count']} шт.")
    return "\n".join(lines)


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database) -> None:
    await message.answer(await _format_stats(db))


@router.callback_query(F.data == "menu:stats")
async def on_menu_stats(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    await safe_edit(callback, await _format_stats(db), kb([BACK_TO_MENU]))
