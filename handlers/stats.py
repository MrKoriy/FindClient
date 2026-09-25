"""Handler for /stats command -- overall collection statistics."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from db.database import Database

router = Router()


def _pct(part: int, total: int) -> int:
    return round(part * 100 / total) if total else 0


async def _format_stats(db: Database) -> str:
    s = await db.get_stats()

    if s["total_orgs"] == 0:
        return "Статистика пуста. Используйте /scrape для сбора контактов."

    total = s["total_orgs"]
    lines = [
        "Общая статистика:\n",
        f"  Всего сессий: {s['total_sessions']}",
        f"  Уникальных организаций: {total}",
        f"  С телефоном: {s['with_phone']} ({_pct(s['with_phone'], total)}%)",
        f"  С email: {s['with_email']} ({_pct(s['with_email'], total)}%)",
        f"  С сайтом: {s['with_website']} ({_pct(s['with_website'], total)}%)",
    ]

    if s["top_niches"]:
        lines.append("\nТоп ниши:")
        for i, n in enumerate(s["top_niches"], 1):
            lines.append(f"  {i}. {n['niche']} — {n['count']} шт.")

    return "\n".join(lines)


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database) -> None:
    """Show overall collection statistics."""
    await message.answer(await _format_stats(db))


@router.callback_query(F.data == "menu:stats")
async def on_menu_stats(callback: CallbackQuery, db: Database) -> None:
    """Handle stats button from /start menu."""
    await callback.answer()
    await callback.message.edit_text(await _format_stats(db))
