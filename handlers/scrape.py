"""Company search flow: niche -> city -> options (sources, "без сайта", count, format) -> table."""

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from data.niches import CATEGORIES, get_niche, niches_in
from handlers.common import BACK_TO_MENU, CITIES, Progress, check, document, grid, kb, plural, safe_edit
from services.scrape_service import SOURCE_LABELS, ScrapeRequest, ScrapeResult, ScrapeService

log = logging.getLogger(__name__)
router = Router()

COUNTS = (25, 50, 100, 200, 500)
DEFAULTS = {
    "city": "Москва", "count": 50, "sources": ["2gis", "yandex"],
    "no_site": True, "phone": True, "fmt": "xlsx",
}


class ScrapeStates(StatesGroup):
    choosing_niche = State()
    entering_city = State()
    options = State()
    entering_count = State()
    scraping = State()


def _categories_kb():
    items = [(c, f"sc:cat:{i}") for i, c in enumerate(CATEGORIES)]
    return kb(grid(items, 2) + [[("✍️ Своя ниша (любая)", "sc:custom")], BACK_TO_MENU])


def _city_kb():
    items = [(c, f"sc:city:{i}") for i, c in enumerate(CITIES)]
    return kb(grid(items, 3) + [[("🏙 Другой город…", "sc:city:other")], [("⬅️ Назад", "menu:scrape")]])


def _options_text(d: dict) -> str:
    src = ", ".join(SOURCE_LABELS[s] for s in d["sources"]) or "—"
    queries = "; ".join(d["queries"])
    return (
        f"<b>Параметры поиска</b>\n\n"
        f"Ниша: <b>{html.escape(d['label'])}</b>\n"
        f"Запросы: {html.escape(queries)}\n"
        f"Город: <b>{html.escape(d['city'])}</b>\n"
        f"Источники: {src}\n"
        f"Только без сайта: {'да' if d['no_site'] else 'нет'}\n"
        f"Только с телефоном: {'да' if d['phone'] else 'нет'}\n"
        f"Количество: {d['count']}\n"
        f"Формат: {d['fmt'].upper()}\n\n"
        "Уже собранные ранее компании автоматически пропускаются."
    )


def _options_kb(d: dict):
    return kb([
        [(f"{check('2gis' in d['sources'])} 2GIS", "sc:opt:2gis"),
         (f"{check('yandex' in d['sources'])} Яндекс Карты", "sc:opt:yandex")],
        [(f"{check(d['no_site'])} Без сайта", "sc:opt:nosite"),
         (f"{check(d['phone'])} С телефоном", "sc:opt:phone")],
        [(f"🔢 {d['count']} шт.", "sc:opt:count"), ("✏️ Своё число", "sc:opt:countin"),
         (f"📄 {d['fmt'].upper()}", "sc:opt:fmt")],
        [(f"🏙 Город: {d['city']}", "sc:opt:city")],
        [("🚀 Запустить поиск", "sc:go")],
        BACK_TO_MENU,
    ])


async def _show_options(target: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ScrapeStates.options)
    d = await state.get_data()
    if isinstance(target, CallbackQuery):
        await safe_edit(target, _options_text(d), _options_kb(d), parse_mode="HTML")
    else:
        await target.answer(_options_text(d), reply_markup=_options_kb(d), parse_mode="HTML")


async def start_with_niche(target: Message | CallbackQuery, state: FSMContext, label: str, queries: list[str]) -> None:
    """Entry point used by the niche catalog too."""
    data = await state.get_data()
    keep = {k: data.get(k, v) for k, v in DEFAULTS.items()}
    await state.set_data({**keep, "label": label, "queries": queries})
    await state.set_state(ScrapeStates.entering_city)
    text = f"Ниша: <b>{html.escape(label)}</b>\n\nВыберите город:"
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, _city_kb(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=_city_kb(), parse_mode="HTML")


@router.message(Command("scrape"))
async def cmd_scrape(message: Message, state: FSMContext) -> None:
    await state.set_state(ScrapeStates.choosing_niche)
    await message.answer("Выберите категорию или введите свою нишу:", reply_markup=_categories_kb())


@router.callback_query(F.data == "menu:scrape")
async def on_menu_scrape(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ScrapeStates.choosing_niche)
    await safe_edit(callback, "Выберите категорию или введите свою нишу:", _categories_kb())


@router.callback_query(F.data.startswith("sc:cat:"))
async def on_category(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    category = CATEGORIES[int(callback.data.split(":")[2])]
    items = [(n.label, f"sc:n:{n.id}") for n in niches_in(category)]
    await state.set_state(ScrapeStates.choosing_niche)
    await safe_edit(callback, f"<b>{html.escape(category)}</b> — выберите нишу:",
                    kb(grid(items, 1) + [[("⬅️ Категории", "menu:scrape")]]), parse_mode="HTML")


@router.callback_query(F.data.startswith("sc:n:"))
async def on_niche(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    niche = get_niche(callback.data.split(":", 2)[2])
    if not niche:
        return
    await start_with_niche(callback, state, niche.label, list(niche.queries))


@router.callback_query(F.data == "sc:custom")
async def on_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ScrapeStates.choosing_niche)
    await safe_edit(callback, "Введите нишу — любую, как искали бы на картах.\n"
                    "Можно несколько запросов через «;», например:\n"
                    "<i>стоматология; имплантация зубов</i>", parse_mode="HTML")


@router.message(ScrapeStates.choosing_niche, F.text, ~F.text.startswith("/"))
async def on_custom_niche(message: Message, state: FSMContext) -> None:
    queries = [q.strip() for q in message.text.split(";") if q.strip()]
    if not queries:
        await message.answer("Введите название ниши:")
        return
    await start_with_niche(message, state, queries[0], queries)


@router.callback_query(F.data.startswith("sc:city:"))
async def on_city(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    value = callback.data.split(":", 2)[2]
    if value == "other":
        await state.set_state(ScrapeStates.entering_city)
        await safe_edit(callback, "Напишите город (любой: «Тверь», «Химки», «Урюпинск»):")
        return
    await state.update_data(city=CITIES[int(value)])
    await _show_options(callback, state)


@router.message(ScrapeStates.entering_city, F.text, ~F.text.startswith("/"))
async def on_city_text(message: Message, state: FSMContext) -> None:
    await state.update_data(city=message.text.strip()[:60])
    await _show_options(message, state)


@router.callback_query(F.data.startswith("sc:opt:"))
async def on_option(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    opt = callback.data.split(":", 2)[2]
    d = await state.get_data()
    if "queries" not in d:
        await on_menu_scrape(callback, state)
        return
    if opt in ("2gis", "yandex"):
        sources = list(d["sources"])
        sources.remove(opt) if opt in sources else sources.append(opt)
        d["sources"] = [s for s in ("2gis", "yandex") if s in sources]
    elif opt == "nosite":
        d["no_site"] = not d["no_site"]
    elif opt == "phone":
        d["phone"] = not d["phone"]
    elif opt == "count":
        d["count"] = COUNTS[(COUNTS.index(d["count"]) + 1) % len(COUNTS)] if d["count"] in COUNTS else COUNTS[0]
    elif opt == "countin":
        await state.set_state(ScrapeStates.entering_count)
        await safe_edit(callback, "Сколько компаний собрать? (1–1000)")
        return
    elif opt == "fmt":
        d["fmt"] = "csv" if d["fmt"] == "xlsx" else "xlsx"
    elif opt == "city":
        await state.set_state(ScrapeStates.entering_city)
        await safe_edit(callback, "Выберите город:", _city_kb())
        return
    await state.set_data(d)
    await _show_options(callback, state)


@router.message(ScrapeStates.entering_count, F.text, ~F.text.startswith("/"))
async def on_count(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= 1000:
        await message.answer("Введите число от 1 до 1000:")
        return
    await state.update_data(count=int(text))
    await _show_options(message, state)


def summary_text(req: ScrapeRequest, result: ScrapeResult) -> str:
    total = len(result.organizations)
    lines = [f"<b>«{html.escape(req.niche)}», {html.escape(req.city)}</b>", f"Новых компаний: <b>{total}</b>"]
    if req.filters:
        lines.append(f"Фильтр: {req.filters}")
    per = ", ".join(f"{SOURCE_LABELS.get(k, k)}: {v}" for k, v in result.per_source.items())
    if per:
        lines.append(f"Найдено в источниках: {per}")
    if result.already_in_db:
        lines.append(f"Пропущено (собраны ранее): {result.already_in_db}")
    lines += [
        f"С телефоном: {result.with_phone} · email: {result.with_email} · соцсети: {result.with_socials}",
        f"Без сайта: {result.without_website} · с сайтом: {result.with_website}",
    ]
    if result.organizations:
        lines.append("\n<b>Топ по скорингу:</b>")
        for o in result.organizations[:7]:
            site = "без сайта" if not o.website else "есть сайт"
            lines.append(f"• {html.escape(o.name[:50])} — {html.escape(o.phone.split(',')[0] or 'нет тел.')} ({site}, {o.score})")
    for err in result.errors:
        lines.append(f"\n⚠️ {html.escape(err[:200])}")
    return "\n".join(lines)


async def run_search(message: Message, req: ScrapeRequest, fmt: str, scrape_service: ScrapeService) -> None:
    status = await message.answer(f"🔎 Ищу «{req.niche}» в городе {req.city}…")
    progress = Progress(status, f"🔎 «{req.niche}», {req.city}")
    try:
        result = await scrape_service.scrape(req, on_progress=progress)
    except Exception:
        log.exception("scrape failed")
        await status.edit_text("Произошла ошибка при сборе данных. Попробуйте позже или смените источник.")
        return

    if not result.organizations:
        text = f"По запросу «{req.niche}» ({req.city}) новых компаний не найдено."
        if result.already_in_db:
            text += f"\nВ базе уже {result.already_in_db} компаний этой ниши — попробуйте другие запросы или город."
        if req.only_without_site:
            text += "\nМожно отключить фильтр «Без сайта»."
        for err in result.errors:
            text += f"\n⚠️ {err[:200]}"
        await status.edit_text(text)
        return

    await status.edit_text(summary_text(req, result), parse_mode="HTML")
    data, ext = result.file(fmt)
    name = f"{req.niche}_{req.city}_{len(result.organizations)}.{ext}"
    await message.answer_document(
        document(data, name), caption=plural(len(result.organizations), "компания", "компании", "компаний"),
        reply_markup=kb([[("📨 Добавить в рассылку", f"out:pick:{result.session_id}")]]) if result.session_id else None,
    )


@router.callback_query(F.data == "sc:go")
async def on_go(callback: CallbackQuery, state: FSMContext, scrape_service: ScrapeService) -> None:
    d = await state.get_data()
    if "queries" not in d:
        await callback.answer()
        await on_menu_scrape(callback, state)
        return
    if not d["sources"]:
        await callback.answer("Выберите хотя бы один источник", show_alert=True)
        return
    await callback.answer("Запускаю…")
    await state.set_state(ScrapeStates.scraping)
    req = ScrapeRequest(
        queries=tuple(d["queries"]), city=d["city"], count=d["count"], sources=tuple(d["sources"]),
        only_without_site=d["no_site"], only_with_phone=d["phone"], label=d["label"],
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await run_search(callback.message, req, d["fmt"], scrape_service)
    await state.set_state(ScrapeStates.options)
    await callback.message.answer("Повторить с другими параметрами?", reply_markup=_options_kb(d))


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject, scrape_service: ScrapeService) -> None:
    """/find ниша | город | количество — quick search without the menus."""
    if not command.args:
        await message.answer(
            "Быстрый поиск: <code>/find стоматология | Казань | 100</code>\n"
            "По умолчанию: Москва, 50 шт., только без сайта и с телефоном, 2GIS + Яндекс.\n"
            "Добавьте <code>| все</code>, чтобы не фильтровать по сайту.",
            parse_mode="HTML",
        )
        return
    parts = [p.strip() for p in command.args.split("|")]
    queries = tuple(q.strip() for q in parts[0].split(";") if q.strip())
    city = parts[1] if len(parts) > 1 and parts[1] else "Москва"
    count = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 50
    all_sites = any(p.lower() in ("все", "all") for p in parts[1:])
    req = ScrapeRequest(
        queries=queries, city=city, count=min(count, 1000),
        only_without_site=not all_sites, only_with_phone=True,
    )
    await run_search(message, req, "xlsx", scrape_service)
