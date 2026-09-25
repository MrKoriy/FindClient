"""/start, /help, /cancel and the main menu."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from handlers.common import MAIN_MENU, safe_edit

router = Router()

WELCOME = (
    "<b>FindClient — клиенты на сайты и лендинги</b>\n\n"
    "🔎 <b>Компании с карт</b> — любая ниша и любой город, 2GIS + Яндекс Карты, "
    "фильтр «без сайта», скоринг, выгрузка в Excel/CSV.\n"
    "💰 <b>Денежные ниши</b> — каталог ниш с оборотом в миллионы в месяц и где их искать.\n"
    "👷 <b>Telegram-лиды</b> — строители, прорабы, дизайнеры, риелторы из профильных чатов.\n"
    "💼 <b>Заказы с бирж</b> — Kwork, FL.ru, Freelance.ru, FreelanceJob и Telegram "
    "присылаются автоматически.\n\n"
    "Быстрый поиск: <code>/find стоматология | Казань | 100</code>"
)

HELP = (
    "<b>Команды</b>\n"
    "/scrape — поиск компаний через меню\n"
    "/find ниша | город | кол-во — быстрый поиск (без сайта, с телефоном)\n"
    "/niches — денежные ниши\n"
    "/tg — лиды из Telegram-чатов\n"
    "/orders — автопоиск заказов\n"
    "/history — история и повторная выгрузка\n"
    "/stats — статистика\n"
    "/cancel — сбросить текущий шаг"
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(WELCOME, reply_markup=MAIN_MENU, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP, parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=MAIN_MENU)


@router.callback_query(F.data == "menu:home")
async def on_home(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await safe_edit(callback, WELCOME, MAIN_MENU, parse_mode="HTML")
