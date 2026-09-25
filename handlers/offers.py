"""Offer library UI: learn techniques from creators, generate own offer per niche."""

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import Settings
from data.niches import NICHES, get_niche
from db.database import Database
from handlers.common import BACK_TO_MENU, Progress, document, kb, niche_picker_categories, niche_picker_niches, safe_edit
from handlers.outreach import create_campaign_from_offer
from services.crm import CRM
from services.export import export
from services.llm import LLM, LLMError
from services.offers import OfferLibrary, OfferSourceError, extract_techniques, fetch_source, generate_offer

log = logging.getLogger(__name__)
router = Router()

KIND_RU = {
    "offer_structure": "структура оффера", "hook": "зацепки", "pain": "боли", "guarantee": "гарантии",
    "cta": "призыв к действию", "followup": "напоминания", "personalization": "персонализация",
    "objection": "возражения", "other": "прочее",
}
_TECH_COLUMNS = [("kind_ru", "Тип", 18), ("title", "Приём", 36), ("description", "Суть", 60),
                 ("example", "Пример", 60)]


class OfferStates(StatesGroup):
    sources = State()
    profile = State()
    niche = State()


async def _panel(lib: OfferLibrary, llm: LLM, db: Database, chat_id: int):
    counts = await lib.count_by_kind()
    sources = await lib.list_sources()
    offers = await lib.list_offers(limit=5)
    profile = await db.get_setting(chat_id, "my_profile", "")
    lines = [
        "<b>🧠 Офферы</b>",
        "Скормите боту видео и каналы креаторов о продажах и холодных сообщениях. Он вытащит приёмы "
        "(структура оффера, зацепки, гарантии, напоминания) и соберёт из них ваш оффер под нишу.",
        "",
        f"Источников: {len(sources)} · приёмов: {sum(counts.values())}",
    ]
    if counts:
        lines.append(", ".join(f"{KIND_RU.get(k, k)}: {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    lines.append(f"Мой профиль: {'заполнен' if profile else 'не заполнен — цены, сроки, портфолио улучшат оффер'}")
    if not llm.enabled:
        lines.append("\n⚠️ Нужен BAI_API_KEY (DeepSeek) — без него разбор и генерация недоступны.")
    rows = [[("➕ Добавить источники", "of:add"), ("👤 Мой профиль", "of:profile")],
            [("✨ Оффер для ниши", "of:cats"), ("📚 Приёмы в Excel", "of:xls")]]
    for o in offers:
        rows.append([(f"📄 {o['niche'][:30]} · {o['created_at'][:10]}", f"of:o:{o['id']}")])
    rows += [[("📨 Рассылки", "menu:out")], BACK_TO_MENU]
    return "\n".join(lines), kb(rows)


@router.message(Command("offers"))
async def cmd_offers(message: Message, offer_lib: OfferLibrary, llm: LLM, db: Database) -> None:
    text, markup = await _panel(offer_lib, llm, db, message.chat.id)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "menu:offers")
async def on_menu(callback: CallbackQuery, state: FSMContext, offer_lib: OfferLibrary, llm: LLM, db: Database) -> None:
    await callback.answer()
    await state.clear()
    text, markup = await _panel(offer_lib, llm, db, callback.message.chat.id)
    await safe_edit(callback, text, markup, parse_mode="HTML")


@router.callback_query(F.data == "of:add")
async def on_add(callback: CallbackQuery, state: FSMContext, llm: LLM) -> None:
    await callback.answer()
    if not llm.enabled:
        await callback.answer("Нужен BAI_API_KEY", show_alert=True)
        return
    await state.set_state(OfferStates.sources)
    await safe_edit(callback, "Пришлите источники — по одному на строку:\n"
                    "• ссылку на YouTube-видео (нужны субтитры)\n"
                    "• Telegram-канал: @name или t.me/name\n"
                    "• ссылку на статью\n"
                    "• или просто вставьте текст (конспект, скрипт, пост).",
                    kb([[("⬅️ Отмена", "menu:offers")]]))


@router.message(OfferStates.sources, F.text, ~F.text.startswith("/"))
async def on_sources(message: Message, state: FSMContext, offer_lib: OfferLibrary, llm: LLM, db: Database) -> None:
    await state.clear()
    text = message.text.strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    refs = lines if all(l.startswith(("http", "@", "t.me/")) for l in lines) else [text]
    status = await message.answer(f"🧠 Разбираю источников: {len(refs)}…")
    progress = Progress(status, "🧠 Разбор источников")
    report = []
    for i, ref in enumerate(refs[:10], 1):
        await progress(f"{i}/{len(refs)}: {ref[:50]}")
        try:
            title, body = await fetch_source(ref)
            techniques = await extract_techniques(llm, title, body)
        except (OfferSourceError, LLMError) as exc:
            report.append(f"❌ {html.escape(ref[:60])}: {html.escape(str(exc)[:150])}")
            continue
        sid = await offer_lib.add_source(ref[:300], title)
        added = await offer_lib.add_techniques(sid, techniques)
        report.append(f"✅ {html.escape(title[:60])}: приёмов {len(techniques)}, новых {added}")
    await status.edit_text("\n".join(report) or "Нечего разбирать.", parse_mode="HTML")
    text_, markup = await _panel(offer_lib, llm, db, message.chat.id)
    await message.answer(text_, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "of:profile")
async def on_profile(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await callback.answer()
    current = await db.get_setting(callback.message.chat.id, "my_profile", "")
    await state.set_state(OfferStates.profile)
    await safe_edit(callback, "Расскажите о себе для офферов: что делаете, цены и сроки, стек (Tilda/код), "
                    "кейсы и цифры, гарантии.\n\n"
                    f"Сейчас: <i>{html.escape(current[:600]) or 'пусто'}</i>",
                    kb([[("⬅️ Отмена", "menu:offers")]]), parse_mode="HTML")


@router.message(OfferStates.profile, F.text, ~F.text.startswith("/"))
async def on_profile_text(message: Message, state: FSMContext, db: Database, offer_lib: OfferLibrary, llm: LLM) -> None:
    await db.set_setting(message.chat.id, "my_profile", message.text.strip()[:2000])
    await state.clear()
    text, markup = await _panel(offer_lib, llm, db, message.chat.id)
    await message.answer("✅ Профиль сохранён.\n\n" + text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "of:cats")
async def on_cats(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(OfferStates.niche)
    await safe_edit(callback, "Для какой ниши собрать оффер?", niche_picker_categories("of", "menu:offers"))


@router.callback_query(F.data.startswith("of:cat:"))
async def on_cat(callback: CallbackQuery) -> None:
    await callback.answer()
    category, markup = niche_picker_niches("of", int(callback.data.split(":")[2]))
    await safe_edit(callback, f"<b>{html.escape(category)}</b>:", markup, parse_mode="HTML")


@router.callback_query(F.data == "of:custom")
async def on_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(OfferStates.niche)
    await safe_edit(callback, "Напишите нишу:")


def _offer_text(offer: dict) -> str:
    lines = [f"<b>Оффер: {html.escape(offer.get('niche', ''))}</b>", html.escape(offer["offer"]), ""]
    for v in offer["variants"]:
        lines.append(f"<b>Вариант {html.escape(v['name'])}</b>\n{html.escape(v['first_message'])}")
        lines.append(f"<i>Напоминание 1:</i> {html.escape(v['followup_1'])}")
        lines.append(f"<i>Напоминание 2:</i> {html.escape(v['followup_2'])}\n")
    if offer.get("angles"):
        lines.append("<b>Углы для тестов:</b> " + html.escape("; ".join(offer["angles"])))
    return "\n".join(lines)[:4000]


async def _generate(message: Message, label: str, offer_lib: OfferLibrary, llm: LLM, db: Database) -> None:
    niche = next((n for n in NICHES if n.label == label), None)
    status = await message.answer(f"✨ Собираю оффер для «{label}»…")
    try:
        profile = await db.get_setting(message.chat.id, "my_profile", "")
        offer = await generate_offer(llm, label, niche or {}, await offer_lib.list_techniques(), profile)
    except LLMError as exc:
        await status.edit_text(f"Не получилось: {exc}")
        return
    oid = await offer_lib.save_offer(label, offer["offer"], offer["variants"], offer.get("angles"))
    await status.edit_text(_offer_text(await offer_lib.get_offer(oid)), parse_mode="HTML",
                           reply_markup=kb([[("📨 Создать кампанию", f"of:camp:{oid}")],
                                            [("🔄 Ещё вариант", f"of:regen:{oid}"), ("⬅️ Офферы", "menu:offers")]]))


@router.callback_query(F.data.startswith("of:n:"))
async def on_niche(callback: CallbackQuery, state: FSMContext, offer_lib: OfferLibrary, llm: LLM, db: Database) -> None:
    await callback.answer()
    await state.clear()
    niche = get_niche(callback.data.split(":", 2)[2])
    if niche:
        await _generate(callback.message, niche.label, offer_lib, llm, db)


@router.message(OfferStates.niche, F.text, ~F.text.startswith("/"))
async def on_niche_text(message: Message, state: FSMContext, offer_lib: OfferLibrary, llm: LLM, db: Database) -> None:
    await state.clear()
    await _generate(message, message.text.strip()[:60], offer_lib, llm, db)


@router.callback_query(F.data.startswith("of:regen:"))
async def on_regen(callback: CallbackQuery, offer_lib: OfferLibrary, llm: LLM, db: Database) -> None:
    await callback.answer()
    offer = await offer_lib.get_offer(int(callback.data.split(":")[2]))
    if offer:
        await _generate(callback.message, offer["niche"], offer_lib, llm, db)


@router.callback_query(F.data.startswith("of:o:"))
async def on_offer(callback: CallbackQuery, offer_lib: OfferLibrary) -> None:
    await callback.answer()
    offer = await offer_lib.get_offer(int(callback.data.split(":")[2]))
    if offer:
        await callback.message.answer(_offer_text(offer), parse_mode="HTML", reply_markup=kb(
            [[("📨 Создать кампанию", f"of:camp:{offer['id']}")], [("⬅️ Офферы", "menu:offers")]]))


@router.callback_query(F.data.startswith("of:camp:"))
async def on_campaign(callback: CallbackQuery, offer_lib: OfferLibrary, crm: CRM, settings: Settings, llm: LLM) -> None:
    offer = await offer_lib.get_offer(int(callback.data.split(":")[2]))
    if not offer:
        await callback.answer()
        return
    cid = await create_campaign_from_offer(callback.message.chat.id, crm, settings, llm, offer["niche"], offer)
    await callback.answer("Кампания создана")
    await callback.message.answer("✅ Кампания создана — добавьте лидов.",
                                  reply_markup=kb([[("Открыть кампанию", f"out:c:{cid}")]]))


@router.callback_query(F.data == "of:xls")
async def on_xls(callback: CallbackQuery, offer_lib: OfferLibrary) -> None:
    await callback.answer()
    rows = [{**t, "kind_ru": KIND_RU.get(t["kind"], t["kind"])} for t in await offer_lib.list_techniques(limit=1000)]
    if not rows:
        await callback.message.answer("Библиотека пуста — добавьте источники.")
        return
    data, ext = export(rows, _TECH_COLUMNS, title="Приёмы")
    await callback.message.answer_document(document(data, f"приёмы.{ext}"), caption=f"{len(rows)} приёмов")
