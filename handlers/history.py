"""Handler for /history command -- shows past scrape sessions."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from db.database import Database

router = Router()


async def _format_history(db: Database) -> str:
    sessions = await db.get_history(limit=20)
    if not sessions:
        return "История пуста. Используйте /scrape для сбора контактов."
    lines = ["Последние сессии сбора:\n"]
    for s in sessions:
        lines.append(f"  {s['date']} — {s['niche']} ({s['count']} шт.)")
    return "\n".join(lines)


@router.message(Command("history"))
async def cmd_history(message: Message, db: Database) -> None:
    """Show list of past scrape sessions."""
    await message.answer(await _format_history(db))


@router.callback_query(F.data == "menu:history")
async def on_menu_history(callback: CallbackQuery, db: Database) -> None:
    """Handle history button from /start menu."""
    await callback.answer()
    await callback.message.edit_text(await _format_history(db))
