"""Handler for /scrape command -- niche selection, count input, scraping, CSV delivery."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.scrape_service import ScrapeService

router = Router()

# Preset niches for quick selection.
_NICHES = [
    ("Рестораны", "рестораны"),
    ("Салоны красоты", "салоны красоты"),
    ("Автосервисы", "автосервис"),
    ("Стоматологии", "стоматология"),
    ("Фитнес-клубы", "фитнес клуб"),
    ("Юристы", "юридические услуги"),
]


class ScrapeStates(StatesGroup):
    choosing_niche = State()
    entering_count = State()
    scraping = State()


def _niche_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for label, query in _NICHES:
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"niche:{query}")])
    buttons.append([InlineKeyboardButton(text="Свой вариант...", callback_data="niche:custom")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("scrape"))
async def cmd_scrape(message: Message, state: FSMContext) -> None:
    """Start the scraping flow -- show niche selection."""
    await state.set_state(ScrapeStates.choosing_niche)
    await message.answer(
        "Выберите нишу для сбора контактов:",
        reply_markup=_niche_keyboard(),
    )


@router.callback_query(F.data == "menu:scrape")
async def on_menu_scrape(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle scrape button from /start menu."""
    await callback.answer()
    await state.set_state(ScrapeStates.choosing_niche)
    await callback.message.edit_text(
        "Выберите нишу для сбора контактов:",
        reply_markup=_niche_keyboard(),
    )


@router.callback_query(F.data.startswith("niche:"), ScrapeStates.choosing_niche)
async def on_niche_selected(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle niche selection from inline keyboard."""
    await callback.answer()
    niche = callback.data.split(":", 1)[1]

    if niche == "custom":
        await callback.message.edit_text("Введите нишу (например: автошколы, пиццерии):")
        return

    await state.update_data(niche=niche)
    await state.set_state(ScrapeStates.entering_count)
    await callback.message.edit_text(
        f"Ниша: {niche}\n\nСколько контактов собрать? (число от 1 до 200, по умолчанию 50)"
    )


@router.message(ScrapeStates.choosing_niche)
async def on_custom_niche(message: Message, state: FSMContext) -> None:
    """Handle custom niche text input."""
    niche = message.text.strip()
    if not niche:
        await message.answer("Введите название ниши:")
        return

    await state.update_data(niche=niche)
    await state.set_state(ScrapeStates.entering_count)
    await message.answer(
        f"Ниша: {niche}\n\nСколько контактов собрать? (число от 1 до 200, по умолчанию 50)"
    )


@router.message(ScrapeStates.entering_count)
async def on_count_entered(
    message: Message, state: FSMContext, scrape_service: ScrapeService
) -> None:
    """Handle lead count input, then run the scrape."""
    text = message.text.strip()

    if text == "" or text.lower() in ("50", ""):
        count = 50
    else:
        try:
            count = int(text)
        except ValueError:
            await message.answer("Введите число от 1 до 200:")
            return

    if count < 1 or count > 200:
        await message.answer("Введите число от 1 до 200:")
        return

    data = await state.get_data()
    niche = data["niche"]
    await state.set_state(ScrapeStates.scraping)

    progress_msg = await message.answer(f"Ищу «{niche}»... Это может занять некоторое время.")

    async def _on_progress(done: int, total: int) -> None:
        try:
            await progress_msg.edit_text(
                f"Ищу «{niche}»... Обработано {done}/{total}"
            )
        except Exception:
            pass  # Telegram rate limits or message unchanged

    try:
        result = await scrape_service.scrape(
            niche=niche, count=count, on_progress=_on_progress,
        )
    except Exception:
        await progress_msg.edit_text(
            "Произошла ошибка при сборе данных. Попробуйте позже."
        )
        await state.clear()
        return

    await state.clear()

    if not result.organizations:
        if result.already_in_db > 0:
            await progress_msg.edit_text(
                f"По запросу «{niche}» новых компаний не найдено.\n\n"
                f"В базе уже {result.already_in_db} компаний — "
                f"похоже, все результаты 2GIS уже собраны."
            )
        else:
            await progress_msg.edit_text(
                f"По запросу «{niche}» ничего не найдено."
            )
        return

    # Stats summary.
    total = len(result.organizations)
    lines = [
        f"Результаты по запросу «{niche}»:\n",
        f"Найдено новых: {total}",
    ]
    if result.already_in_db > 0:
        lines.append(f"Уже в базе: {result.already_in_db}")
    lines.extend([
        f"С телефоном: {result.with_phone}",
        f"С email: {result.with_email}",
        f"С сайтом: {result.with_website}",
        f"С соцсетями: {result.with_socials}",
    ])
    stats = "\n".join(lines)

    await progress_msg.edit_text(stats)

    # Send CSV file.
    filename = f"{niche.replace(' ', '_')}_контакты.csv"
    doc = BufferedInputFile(result.csv_bytes, filename=filename)
    await message.answer_document(doc, caption=f"{total} контактов")
