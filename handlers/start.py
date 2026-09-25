"""Handler for /start command."""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router()

_MENU_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Собрать контакты", callback_data="menu:scrape")],
    [InlineKeyboardButton(text="История сборов", callback_data="menu:history")],
    [InlineKeyboardButton(text="Статистика", callback_data="menu:stats")],
])


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Handle /start command with a welcome message and inline menu."""
    await message.answer(
        "Привет! Я бот для сбора контактов бизнесов из 2GIS.\n\n"
        "Я могу найти телефоны, email, сайты и соцсети\n"
        "компаний по любой нише в Москве и отправить CSV-файл.\n\n"
        "Выберите действие:",
        reply_markup=_MENU_KB,
    )
