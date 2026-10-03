from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from config import Settings
from crm import auth as crm_auth
from handlers.common import MAIN_MENU, safe_edit

router = Router()

WELCOME = (
    "<b>FindClient - клиенты на сайты и лендинги</b>\n\n"
    "🔎 <b>Компании с карт</b> — любая ниша и любой город, 2GIS + Яндекс Карты, "
    "фильтр «без сайта», скоринг, выгрузка в Excel/CSV.\n"
    "💰 <b>Денежные ниши</b> — каталог ниш с оборотом в миллионы в месяц и где их искать.\n"
    "👷 <b>Telegram-лиды</b> — строители, прорабы, дизайнеры, риелторы из профильных чатов.\n"
    "💼 <b>Заказы с бирж</b> — Kwork, FL.ru, Freelance.ru, FreelanceJob и Telegram "
    "присылаются автоматически.\n"
    "📨 <b>Рассылки + CRM</b> — цепочки сообщений в Telegram, воронка, стоп-лист.\n"
    "🧠 <b>Офферы</b> — библиотека приёмов продаж с поиском по нише/триггеру.\n\n"
    "Быстрый поиск: <code>/find стоматология | Казань | 100</code>"
)

HELP = (
    "<b>Команды</b>\n"
    "/scrape — поиск компаний через меню\n"
    "/find ниша | город | кол-во — быстрый поиск (без сайта, с телефоном)\n"
    "/niches — денежные ниши\n"
    "/tg — лиды из Telegram-чатов\n"
    "/orders — автопоиск заказов\n"
    "/outreach — рассылки и CRM\n"
    "/offers — библиотека офферов\n"
    "/crm — вход в CRM-панель (в один клик)\n"
    "/history — история и повторная выгрузка\n"
    "/stats — статистика\n"
    "/cancel — сбросить текущий шаг"
)


def make_crm_auth_view(user_id: int, crm_url: str) -> tuple[str, InlineKeyboardMarkup]:
    home = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Главное меню", callback_data="menu:home")]]
    )
    if not crm_url.startswith("https://"):
        return ("CRM-панель не настроена: укажите <code>CRM_URL=https://...</code> в .env бота.", home)
    try:
        token = crm_auth.generate_magic_token(user_id)
    except crm_auth.AuthConfigError:
        return ("CRM-панель не настроена: задайте <code>CRM_SECRET_KEY</code> (одинаковый в .env и crm.env).", home)
    login_url = f"{crm_url.rstrip('/')}/auth?token={token}"

    text = (
        "<b>Панель FindClient CRM</b>\n\n"
        "Одноразовая ссылка для входа создана.\n"
        "Срок действия: 15 минут, сработает только один раз.\n"
        "Сессия в браузере живёт 7 дней; «Выйти на всех устройствах» в настройках панели отзывает её."
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Открыть CRM в Telegram", web_app=WebAppInfo(url=login_url)),
            InlineKeyboardButton(text="Открыть в браузере", url=login_url),
        ],
        [
            InlineKeyboardButton(text="Главное меню", callback_data="menu:home"),
        ],
    ])
    return text, markup


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


@router.message(Command("crm"))
async def cmd_crm(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    uid = message.from_user.id if message.from_user else 0
    text, markup = make_crm_auth_view(uid, settings.CRM_URL)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "menu:crm")
async def on_crm_menu(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await callback.answer()
    await state.clear()
    uid = callback.from_user.id if callback.from_user else 0
    text, markup = make_crm_auth_view(uid, settings.CRM_URL)
    await safe_edit(callback, text, markup, parse_mode="HTML")


@router.callback_query(F.data == "menu:home")
async def on_home(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await safe_edit(callback, WELCOME, MAIN_MENU, parse_mode="HTML")
