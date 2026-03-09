"""Handler for /start command."""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Handle /start command with a welcome message."""
    await message.answer(
        "Привет! Я бот для сбора контактов бизнесов из 2GIS.\n\n"
        "Доступные команды:\n"
        "/start - Главное меню\n"
        "/scrape - Начать сбор контактов"
    )
