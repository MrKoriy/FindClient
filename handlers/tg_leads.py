"""Telegram leads: collect people from niche chats (builders, realtors...), find new chats, watch requests."""

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from data.niches import NICHES, get_niche
from db.database import Database
from handlers.common import BACK_TO_MENU, Progress, check, document, grid, kb, safe_edit
from services.export import TG_LEAD_COLUMNS, export
from services.telegram_service import TelegramUserService, harvest_channels

log = logging.getLogger(__name__)
router = Router()

WATCH_KEY = "tg_watch_chats"
_CHAT_COLUMNS = [("username", "Username", 26), ("title", "Название", 50), ("type", "Тип", 10),
                 ("members", "Участников", 12), ("link", "Ссылка", 30)]


class TgStates(StatesGroup):
    custom_chats = State()
    search_keywords = State()


def _account_line(tg: TelegramUserService) -> str:
    if tg.enabled:
        return "🟢 Аккаунт Telegram подключён: читаю группы, ищу чаты, собираю авторов сообщений."
    return (
        "⚪️ Аккаунт не подключён — доступен только сбор контактов из публичных <b>каналов</b>.\n"
        f"Причина: {html.escape(tg.error or 'не настроен')}.\n"
        "Чтобы читать группы строителей (главный источник), задайте TG_API_ID, TG_API_HASH "
        "и TG_SESSION (см. README, scripts/tg_login.py)."
    )


async def _panel(tg: TelegramUserService, db: Database, chat_id: int):
    watch = await db.get_setting(chat_id, WATCH_KEY, [])
    text = (
        "<b>👷 Лиды из Telegram</b>\n"
        "Строители, прорабы, отделочники, дизайнеры, риелторы почти не видны на картах, "
        "зато активно пишут в Telegram-чатах. Бот собирает авторов объявлений "
        "(имя, @username, телефон из текста, био, пример сообщения) в таблицу со скорингом.\n\n"
        f"{_account_line(tg)}\n\n"
        f"Под наблюдением на «нужен сайт»: {len(watch)} чатов"
    )
    niche_items = [(n.label, f"tg:niche:{n.id}") for n in NICHES if n.tg_chats][:8]
    rows = grid(niche_items, 2) + [
        [("📋 Свои чаты/каналы", "tg:custom"), ("🔍 Найти чаты по словам", "tg:search")],
        [("👀 Наблюдение «нужен сайт»", "tg:watch")],
        BACK_TO_MENU,
    ]
    return text, kb(rows)


@router.message(Command("tg"))
async def cmd_tg(message: Message, tg_service: TelegramUserService, db: Database) -> None:
    text, markup = await _panel(tg_service, db, message.chat.id)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "menu:tg")
async def on_menu(callback: CallbackQuery, tg_service: TelegramUserService, db: Database, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    text, markup = await _panel(tg_service, db, callback.message.chat.id)
    await safe_edit(callback, text, markup, parse_mode="HTML")


async def _harvest(message: Message, chats: list[str], label: str, tg: TelegramUserService, db: Database) -> None:
    status = await message.answer(f"👷 Собираю лидов «{label}» из {len(chats)} чатов…")
    progress = Progress(status, f"👷 «{label}»")
    try:
        if tg.enabled:
            known = await db.get_known_tg_user_ids()
            leads = await tg.harvest_authors(chats, skip_user_ids=known, on_progress=progress)
        else:
            leads = await harvest_channels(chats, on_progress=progress)
    except Exception:
        log.exception("harvest failed")
        await status.edit_text("Ошибка при сборе. Попробуйте позже.")
        return
    if not leads:
        hint = "" if tg.enabled else (
            "\nБез аккаунта читаются только каналы с открытым веб-просмотром, а контакты там редки. "
            "Подключите аккаунт Telegram, чтобы собирать авторов из групп."
        )
        await status.edit_text(f"Новых контактов не найдено.{hint}")
        return
    if tg.enabled:
        await db.save_tg_leads([
            {"user_id": l.user_id, "username": l.username, "name": l.name, "chats": l.chats}
            for l in leads if l.user_id
        ], niche=label)
    with_contact = sum(1 for l in leads if l.username or l.phone)
    lines = [f"<b>«{html.escape(label)}»: {len(leads)} контактов</b>",
             f"С @username или телефоном: {with_contact}", "", "<b>Топ:</b>"]
    for l in leads[:8]:
        who = f"@{l.username}" if l.username else (l.phone or l.name)
        lines.append(f"• {html.escape(who)} — {html.escape(l.name[:40])} ({l.messages} сообщ., {l.score})")
    await status.edit_text("\n".join(lines), parse_mode="HTML")
    data, ext = export(leads, TG_LEAD_COLUMNS, title="Telegram-лиды")
    await message.answer_document(document(data, f"tg_{label}_{len(leads)}.{ext}"), caption="Лиды из Telegram")


@router.callback_query(F.data.startswith("tg:niche:"))
async def on_niche(callback: CallbackQuery, tg_service: TelegramUserService, db: Database) -> None:
    await callback.answer()
    n = get_niche(callback.data.split(":", 2)[2])
    if not n:
        return
    chats = list(n.tg_chats)
    if tg_service.enabled and n.tg_keywords:
        status = await callback.message.answer("🔍 Дополняю список чатов через поиск Telegram…")
        found = await tg_service.search_chats(list(n.tg_keywords), modifiers=("", "чат"))
        chats += [c["username"] for c in found if c["type"] == "group" and c["username"] not in chats][:10]
        await status.delete()
    if not chats:
        await callback.message.answer("Для этой ниши нет чатов. Используйте «Найти чаты по словам».")
        return
    await _harvest(callback.message, chats, n.label, tg_service, db)


@router.callback_query(F.data == "tg:custom")
async def on_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(TgStates.custom_chats)
    await safe_edit(callback, "Пришлите чаты/каналы через запятую или с новой строки: @name или t.me/name.",
                    kb([[("⬅️ Отмена", "menu:tg")]]))


def _parse_chats(text: str) -> list[str]:
    out = []
    for part in text.replace("\n", ",").replace(" ", ",").split(","):
        part = part.strip().split("t.me/")[-1].lstrip("@").strip("/")
        if part and part not in out:
            out.append(part)
    return out


@router.message(TgStates.custom_chats, F.text, ~F.text.startswith("/"))
async def on_custom_chats(message: Message, state: FSMContext, tg_service: TelegramUserService, db: Database) -> None:
    await state.clear()
    chats = _parse_chats(message.text)
    if not chats:
        await message.answer("Не нашёл ни одного чата в сообщении.")
        return
    await _harvest(message, chats, "свои чаты", tg_service, db)


@router.callback_query(F.data == "tg:search")
async def on_search(callback: CallbackQuery, state: FSMContext, tg_service: TelegramUserService) -> None:
    await callback.answer()
    if not tg_service.enabled:
        await safe_edit(callback, "Поиск чатов по всему Telegram требует подключённого аккаунта "
                        "(TG_API_ID, TG_API_HASH, TG_SESSION).\n\n"
                        "Пока можно использовать готовые подборки чатов по нишам в меню Telegram-лидов.",
                        kb([[("⬅️ Назад", "menu:tg")]]))
        return
    await state.set_state(TgStates.search_keywords)
    await safe_edit(callback, "Пришлите ключевые слова через запятую, например:\n"
                    "<i>прорабы, кровельщики, стройматериалы, аренда спецтехники</i>",
                    kb([[("⬅️ Отмена", "menu:tg")]]), parse_mode="HTML")


@router.message(TgStates.search_keywords, F.text, ~F.text.startswith("/"))
async def on_search_keywords(message: Message, state: FSMContext, tg_service: TelegramUserService) -> None:
    keywords = [k.strip() for k in message.text.split(",") if k.strip()][:10]
    status = await message.answer("🔍 Ищу чаты…")
    found = await tg_service.search_chats(keywords, on_progress=Progress(status, "🔍 Поиск чатов"))
    groups = [c for c in found if c["type"] == "group"]
    await state.set_data({"found_chats": [c["username"] for c in groups][:30]})
    await state.set_state(None)
    lines = [f"<b>Найдено чатов: {len(found)}</b> (групп: {len(groups)})", ""]
    for c in found[:15]:
        lines.append(f"• @{html.escape(c['username'])} — {html.escape(c['title'][:40])} ({c['members']}, {c['type']})")
    await status.edit_text("\n".join(lines), parse_mode="HTML",
                           reply_markup=kb([[("👷 Собрать лидов из найденных групп", "tg:harvest_found")],
                                            [("⬅️ Назад", "menu:tg")]]))
    if found:
        rows = [{**c, "link": f"https://t.me/{c['username']}"} for c in found]
        data, ext = export(rows, _CHAT_COLUMNS, title="Чаты")
        await message.answer_document(document(data, f"чаты_{len(found)}.{ext}"))


@router.callback_query(F.data == "tg:harvest_found")
async def on_harvest_found(callback: CallbackQuery, state: FSMContext, tg_service: TelegramUserService, db: Database) -> None:
    await callback.answer()
    chats = (await state.get_data()).get("found_chats") or []
    if not chats:
        await callback.message.answer("Сначала выполните поиск чатов.")
        return
    await _harvest(callback.message, chats, "найденные чаты", tg_service, db)


@router.callback_query(F.data == "tg:watch")
async def on_watch(callback: CallbackQuery, tg_service: TelegramUserService, db: Database) -> None:
    await callback.answer()
    chat_id = callback.message.chat.id
    watch = await db.get_setting(chat_id, WATCH_KEY, [])
    segments = [n for n in NICHES if n.tg_chats]
    rows = [[(f"{check(all(c in watch for c in n.tg_chats))} {n.label}", f"tg:w:{n.id}")] for n in segments]
    text = (
        "<b>Наблюдение «нужен сайт»</b>\n"
        "Бот следит за выбранными чатами и присылает сообщения вида «нужен сайт», «кто сделает лендинг» "
        "в ленту заказов (источник «Telegram-чаты»). Нужен подключённый аккаунт и включённые заказы.\n\n"
        f"Сейчас: {len(watch)} чатов."
    )
    if not tg_service.enabled:
        text += "\n\n⚠️ Аккаунт не подключён — наблюдение заработает после настройки TG_SESSION."
    await safe_edit(callback, text, kb(rows + [[("⬅️ Назад", "menu:tg")]]), parse_mode="HTML")


@router.callback_query(F.data.startswith("tg:w:"))
async def on_watch_toggle(callback: CallbackQuery, tg_service: TelegramUserService, db: Database) -> None:
    n = get_niche(callback.data.split(":", 2)[2])
    chat_id = callback.message.chat.id
    watch = await db.get_setting(chat_id, WATCH_KEY, [])
    if n:
        if all(c in watch for c in n.tg_chats):
            watch = [c for c in watch if c not in n.tg_chats]
        else:
            watch += [c for c in n.tg_chats if c not in watch]
        await db.set_setting(chat_id, WATCH_KEY, watch)
    await on_watch(callback, tg_service, db)
