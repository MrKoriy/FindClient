"""Outreach CRM UI: campaigns, leads import, previews, inbox, replies."""

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from config import Settings
from data.niches import NICHES, get_niche
from db.database import Database
from handlers.common import (
    BACK_TO_MENU, check, document, kb, niche_picker_categories, niche_picker_niches, safe_edit,
)
from services.classifier import LABELS_RU
from services.crm import CRM, STATUS_RU, stop_keys
from services.export import export
from services.llm import LLM, LLMError
from services.offers import OfferLibrary, generate_offer
from services.outreach import OutreachService, funnel_text, leads_from_orgs, leads_from_tg

log = logging.getLogger(__name__)
router = Router()

_CRM_COLUMNS = [
    ("company", "Компания", 30), ("name", "Имя", 20), ("city", "Город", 14), ("phone", "Телефон", 16),
    ("tg", "Telegram", 20), ("status_ru", "Статус", 14), ("variant", "Вариант", 8), ("step", "Шаг", 6),
    ("label_ru", "Последний ответ", 18), ("demo_url", "Демо-сайт", 36), ("note", "Заметка", 30),
]


class OutStates(StatesGroup):
    niche = State()
    own_message = State()
    reply = State()


# ----------------------------------------------------------------------
# Panel
# ----------------------------------------------------------------------

async def _panel(crm: CRM, outreach: OutreachService, llm: LLM, settings: Settings):
    lines = ["<b>📨 Рассылки в Telegram</b>"]
    for a in await outreach.account_status():
        if not a["enabled"]:
            lines.append(f"Аккаунт #{a['account'] + 1}: ⚪️ не подключён ({html.escape(a['error'] or 'нет сессии')})")
            continue
        pause = f" · ⏸ пауза до {a['paused_until']:%H:%M} UTC" if a["paused_until"] else ""
        lines.append(f"Аккаунт #{a['account'] + 1}: 🟢 новых сегодня {a['new_sent']}/{a['limit']}, "
                     f"напоминаний {a['followups']}{pause}")
    if not outreach.accounts:
        lines.append("Аккаунты не подключены: задайте TG_SESSION (и TG_SESSION_2) — см. README.")
    lines.append(f"AI: DeepSeek {'✅' if llm.enabled else '❌ (BAI_API_KEY)'} · "
                 f"Jev {'✅' if llm.jev_enabled else '— (классификация через DeepSeek/правила)'} · "
                 f"демо-сайты {'✅' if settings.DEMO_BASE_URL else '❌ (DEMO_BASE_URL)'}")
    lim = outreach.limits
    lines.append(f"Окно отправки: {lim.work_start}:00–{lim.work_end}:00 МСК, будни; прогрев "
                 f"{lim.warmup_start} → {lim.daily_new_max} новых диалогов в день на аккаунт.")
    rows = []
    for c in (await crm.list_campaigns())[:8]:
        st = await crm.campaign_stats(c["id"])
        icon = "▶️" if c["status"] == "active" else "⏸"
        rows.append([(f"{icon} {c['name'][:28]} · {st['contacted']}/{st['total']} · ответов {st['replied']}",
                      f"out:c:{c['id']}")])
    rows += [[("➕ Новая кампания", "out:new")], [("💬 Ответы", "out:inbox"), ("🧠 Офферы", "menu:offers")],
             BACK_TO_MENU]
    return "\n".join(lines), kb(rows)


@router.message(Command("outreach"))
async def cmd_outreach(message: Message, crm: CRM, outreach: OutreachService, llm: LLM, settings: Settings) -> None:
    text, markup = await _panel(crm, outreach, llm, settings)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "menu:out")
async def on_menu(callback: CallbackQuery, state: FSMContext, crm: CRM, outreach: OutreachService,
                  llm: LLM, settings: Settings) -> None:
    await callback.answer()
    await state.clear()
    text, markup = await _panel(crm, outreach, llm, settings)
    await safe_edit(callback, text, markup, parse_mode="HTML")


# ----------------------------------------------------------------------
# New campaign
# ----------------------------------------------------------------------

@router.callback_query(F.data.in_({"out:new", "out:cats"}))
async def on_new(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(OutStates.niche)
    await safe_edit(callback, "Для какой ниши кампания?", niche_picker_categories("out", "menu:out"))


@router.callback_query(F.data.startswith("out:cat:"))
async def on_new_cat(callback: CallbackQuery) -> None:
    await callback.answer()
    category, markup = niche_picker_niches("out", int(callback.data.split(":")[2]))
    await safe_edit(callback, f"<b>{html.escape(category)}</b>:", markup, parse_mode="HTML")


@router.callback_query(F.data == "out:custom")
async def on_new_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(OutStates.niche)
    await safe_edit(callback, "Напишите нишу, например: <i>кровельщики</i>", parse_mode="HTML")


async def _choose_offer(target, state: FSMContext, label: str, offer_lib: OfferLibrary, llm: LLM) -> None:
    await state.update_data(niche_label=label)
    latest = await offer_lib.latest_offer(label)
    rows = []
    if latest:
        rows.append([(f"📄 Мой оффер от {latest['created_at'][:10]}", f"out:useoffer:{latest['id']}")])
    if llm.enabled:
        rows.append([("✨ Сгенерировать оффер (DeepSeek)", "out:gen")])
    rows += [[("✍️ Написать своё сообщение", "out:own")], [("⬅️ Назад", "out:new")]]
    text = (f"Ниша: <b>{html.escape(label)}</b>\n\nОткуда взять текст? Оффер с 3 вариантами первого сообщения "
            "(A/B/C-тест) и двумя напоминаниями генерируется по вашей библиотеке приёмов из «🧠 Офферы».")
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, kb(rows), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb(rows), parse_mode="HTML")


@router.callback_query(F.data.startswith("out:n:"))
async def on_new_niche(callback: CallbackQuery, state: FSMContext, offer_lib: OfferLibrary, llm: LLM) -> None:
    await callback.answer()
    niche = get_niche(callback.data.split(":", 2)[2])
    if niche:
        await _choose_offer(callback, state, niche.label, offer_lib, llm)


@router.message(OutStates.niche, F.text, ~F.text.startswith("/"))
async def on_new_niche_text(message: Message, state: FSMContext, offer_lib: OfferLibrary, llm: LLM) -> None:
    await _choose_offer(message, state, message.text.strip()[:60], offer_lib, llm)


async def create_campaign_from_offer(chat_id: int, crm: CRM, settings: Settings, llm: LLM, label: str,
                                     offer: dict) -> int:
    return await crm.create_campaign(
        chat_id, label, label, offer["offer"], offer["variants"],
        use_demo=bool(settings.DEMO_BASE_URL), personalize=llm.enabled,
    )


@router.callback_query(F.data.startswith("out:useoffer:"))
async def on_use_offer(callback: CallbackQuery, state: FSMContext, crm: CRM, offer_lib: OfferLibrary,
                       settings: Settings, llm: LLM) -> None:
    await callback.answer()
    offer = await offer_lib.get_offer(int(callback.data.split(":")[2]))
    if not offer:
        return
    await state.clear()
    cid = await create_campaign_from_offer(callback.message.chat.id, crm, settings, llm, offer["niche"], offer)
    await _show_campaign(callback, crm, cid, new=True)


@router.callback_query(F.data == "out:gen")
async def on_gen(callback: CallbackQuery, state: FSMContext, crm: CRM, offer_lib: OfferLibrary, db: Database,
                 settings: Settings, llm: LLM) -> None:
    await callback.answer("Генерирую…")
    label = (await state.get_data()).get("niche_label", "")
    niche = next((n for n in NICHES if n.label == label), None)
    status = await callback.message.answer("✨ Генерирую оффер и варианты сообщений…")
    try:
        profile = await db.get_setting(callback.message.chat.id, "my_profile", "")
        offer = await generate_offer(llm, label, niche or {}, await offer_lib.list_techniques(), profile)
    except LLMError as exc:
        await status.edit_text(f"Не получилось: {exc}")
        return
    oid = await offer_lib.save_offer(label, offer["offer"], offer["variants"], offer.get("angles"))
    await status.delete()
    await state.clear()
    cid = await create_campaign_from_offer(callback.message.chat.id, crm, settings, llm, label,
                                           await offer_lib.get_offer(oid))
    await _show_campaign(callback, crm, cid, new=True)


@router.callback_query(F.data == "out:own")
async def on_own(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(OutStates.own_message)
    await safe_edit(callback, "Пришлите первое сообщение. Подстановки:\n"
                    "<code>{name}</code> — имя, <code>{company}</code> — компания, <code>{city}</code> — город, "
                    "<code>{demo_url}</code> — ссылка на демо-сайт.\n\n"
                    "Совет: коротко, по-человечески, один вопрос в конце и фраза «если неактуально — "
                    "просто напишите, больше не побеспокою».", parse_mode="HTML")


@router.message(OutStates.own_message, F.text, ~F.text.startswith("/"))
async def on_own_text(message: Message, state: FSMContext, crm: CRM, settings: Settings, llm: LLM) -> None:
    label = (await state.get_data()).get("niche_label", "кампания")
    variants = [{"name": "A", "first_message": message.text.strip(),
                 "followup_1": "Подскажите, успели посмотреть моё сообщение выше?",
                 "followup_2": "Если сейчас неактуально — просто напишите, больше не побеспокою."}]
    cid = await crm.create_campaign(message.chat.id, label, label, message.text.strip(), variants,
                                    use_demo=bool(settings.DEMO_BASE_URL), personalize=llm.enabled)
    await state.clear()
    text, markup = await _campaign_view(crm, cid, new=True)
    await message.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


# ----------------------------------------------------------------------
# Campaign view
# ----------------------------------------------------------------------

async def _campaign_view(crm: CRM, cid: int, new: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    c = await crm.get_campaign(cid)
    st = await crm.campaign_stats(cid)
    lines = [f"<b>{'✅ Кампания создана: ' if new else ''}{html.escape(c['name'])}</b>",
             f"Статус: {'▶️ идёт' if c['status'] == 'active' else '⏸ на паузе'} · "
             f"напоминания через {', '.join(str(d) for d in c['delays'])} дн.",
             funnel_text(st)]
    for v in st["variants"]:
        rate = round(v["replied"] * 100 / v["contacted"]) if v["contacted"] else 0
        lines.append(f"Вариант {v['variant']}: написали {v['contacted']}, ответили {v['replied']} ({rate}%), "
                     f"интерес {v['positive']}")
    first = c["variants"][0]["first_message"] if c["variants"] else c["offer"]
    lines.append(f"\n<i>{html.escape(first[:350])}</i>")
    if new:
        lines.append("\nДобавьте лидов и нажмите «Запустить». Сообщения уходят в рабочие часы с паузами.")
    toggle = ("⏸ Пауза", f"out:tog:{cid}") if c["status"] == "active" else ("▶️ Запустить", f"out:tog:{cid}")
    markup = kb([
        [toggle, ("👁 Превью", f"out:prev:{cid}")],
        [("➕ Лиды из сбора карт", f"out:addm:{cid}"), ("➕ Telegram-лиды", f"out:addt:{cid}")],
        [(f"{check(c['use_demo'])} Демо-сайт", f"out:demo:{cid}"),
         (f"{check(c['personalize'])} AI-персонализация", f"out:pers:{cid}")],
        [("📥 CRM в Excel", f"out:xls:{cid}")],
        [("⬅️ Рассылки", "menu:out")],
    ])
    return "\n".join(lines), markup


async def _show_campaign(callback: CallbackQuery, crm: CRM, cid: int, new: bool = False) -> None:
    text, markup = await _campaign_view(crm, cid, new)
    await safe_edit(callback, text, markup, parse_mode="HTML", disable_web_page_preview=True)


def _num(callback: CallbackQuery) -> int:
    return int(callback.data.split(":")[2])


@router.callback_query(F.data.startswith("out:c:"))
async def on_campaign(callback: CallbackQuery, crm: CRM) -> None:
    await callback.answer()
    await _show_campaign(callback, crm, _num(callback))


@router.callback_query(F.data.startswith("out:tog:"))
async def on_toggle(callback: CallbackQuery, crm: CRM, outreach: OutreachService) -> None:
    cid = _num(callback)
    c = await crm.get_campaign(cid)
    if c["status"] != "active":
        if not outreach.active_accounts():
            await callback.answer("Нет подключённых Telegram-аккаунтов (TG_SESSION)", show_alert=True)
            return
        if not (await crm.campaign_stats(cid))["total"]:
            await callback.answer("Сначала добавьте лидов", show_alert=True)
            return
    await crm.update_campaign(cid, status="paused" if c["status"] == "active" else "active")
    await callback.answer("Запущено" if c["status"] != "active" else "На паузе")
    await _show_campaign(callback, crm, cid)


@router.callback_query(F.data.startswith("out:demo:") | F.data.startswith("out:pers:"))
async def on_flag(callback: CallbackQuery, crm: CRM, settings: Settings, llm: LLM) -> None:
    cid = _num(callback)
    c = await crm.get_campaign(cid)
    if callback.data.startswith("out:demo:"):
        if not c["use_demo"] and not settings.DEMO_BASE_URL:
            await callback.answer("Задайте DEMO_BASE_URL (адрес VPS, например http://1.2.3.4:8080)", show_alert=True)
            return
        await crm.update_campaign(cid, use_demo=not c["use_demo"])
    else:
        if not c["personalize"] and not llm.enabled:
            await callback.answer("Нужен BAI_API_KEY", show_alert=True)
            return
        await crm.update_campaign(cid, personalize=not c["personalize"])
    await callback.answer()
    await _show_campaign(callback, crm, cid)


@router.callback_query(F.data.startswith("out:addm:"))
async def on_add_maps(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    cid = _num(callback)
    sessions = await db.get_history(limit=10)
    if not sessions:
        await callback.message.answer("Сначала соберите компании в «🔎 Компании с карт».")
        return
    rows = [[(f"{s['niche'][:26]} {(s.get('city') or '')[:14]} · {s['count']}", f"out:am:{cid}:{s['id']}")]
            for s in sessions]
    await safe_edit(callback, "Из какого сбора добавить компании? Берутся те, у кого есть Telegram "
                    "в соцсетях или телефон (поиск аккаунта по номеру — до 10 в день на аккаунт).",
                    kb(rows + [[("⬅️ Назад", f"out:c:{cid}")]]))


async def add_session_to_campaign(db: Database, crm: CRM, cid: int, sid: int) -> str:
    orgs = await db.get_session_orgs(sid)
    leads = leads_from_orgs(orgs)
    added, skipped = await crm.add_leads(cid, leads)
    with_tg = sum(1 for l in leads if l["tg_username"])
    return (f"Из {len(orgs)} компаний: с Telegram в соцсетях {with_tg}, только телефон {len(leads) - with_tg}, "
            f"без контактов {len(orgs) - len(leads)}.\nДобавлено {added}, пропущено {skipped} "
            "(дубли, стоп-лист, уже писали в другой кампании).")


@router.callback_query(F.data.startswith("out:am:"))
async def on_add_maps_session(callback: CallbackQuery, db: Database, crm: CRM) -> None:
    await callback.answer()
    _, _, cid, sid = callback.data.split(":")
    await callback.message.answer(await add_session_to_campaign(db, crm, int(cid), int(sid)))
    await _show_campaign(callback, crm, int(cid))


@router.callback_query(F.data.startswith("out:addt:"))
async def on_add_tg(callback: CallbackQuery, db: Database, crm: CRM) -> None:
    cid = _num(callback)
    c = await crm.get_campaign(cid)
    rows = await db.get_tg_leads(niche=c["niche"]) or await db.get_tg_leads()
    if not rows:
        await callback.answer("Нет сохранённых Telegram-лидов: соберите их в «👷 Telegram-лиды» "
                              "(нужен подключённый аккаунт)", show_alert=True)
        return
    added, skipped = await crm.add_leads(cid, leads_from_tg(rows))
    await callback.answer(f"Добавлено {added}, пропущено {skipped}", show_alert=True)
    await _show_campaign(callback, crm, cid)


@router.callback_query(F.data.startswith("out:prev:"))
async def on_preview(callback: CallbackQuery, crm: CRM, outreach: OutreachService) -> None:
    await callback.answer("Готовлю превью…")
    cid = _num(callback)
    c = await crm.get_campaign(cid)
    leads = [l for l in await crm.list_leads(cid) if l["status"] == "new"]
    sample = leads[0] if leads else {
        "id": 0, "name": "Иван", "company": "Ромашка", "city": "Москва", "category": "", "variant": "",
        "step": 0, "demo_url": "", "extra": {}, "source": "maps",
    }
    parts = []
    for i, v in enumerate(c["variants"][:3]):
        lead = {**sample, "variant": v.get("name", "A"), "step": 0,
                "demo_url": sample.get("demo_url") or ("http://ваш-сервер/d/demo" if c["use_demo"] else "")}
        personal = c["personalize"] and i == 0
        text = await outreach.compose(lead, {**c, "personalize": personal})
        parts.append(f"<b>Вариант {html.escape(v.get('name', 'A'))}</b>"
                     f"{' (с AI-персонализацией)' if personal else ''}:\n{html.escape(text)}")
    who = (sample["company"] or sample["name"]) + ("" if leads else " (пример)")
    await callback.message.answer(f"👁 Превью для «{html.escape(who)}»\n\n" + "\n\n".join(parts),
                                  parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("out:xls:"))
async def on_export(callback: CallbackQuery, crm: CRM) -> None:
    await callback.answer()
    cid = _num(callback)
    rows = [{**l, "tg": f"@{l['tg_username']}" if l["tg_username"] else "",
             "status_ru": STATUS_RU.get(l["status"], l["status"]),
             "label_ru": LABELS_RU.get(l["last_label"], "")} for l in await crm.list_leads(cid)]
    if not rows:
        await callback.message.answer("В кампании пока нет лидов.")
        return
    data, ext = export(rows, _CRM_COLUMNS, title="CRM")
    await callback.message.answer_document(document(data, f"crm_{cid}.{ext}"), caption=f"{len(rows)} лидов")


# ----------------------------------------------------------------------
# Inbox & lead card
# ----------------------------------------------------------------------

async def lead_card(crm: CRM, lead_id: int, header: str = "") -> tuple[str, InlineKeyboardMarkup]:
    lead = await crm.get_lead(lead_id)
    who = lead["company"] or lead["name"] or "лид"
    contact = f"@{lead['tg_username']}" if lead["tg_username"] else lead["phone"]
    lines = [header] if header else []
    lines += [f"<b>{html.escape(who)}</b> · {html.escape(contact)}",
              f"Статус: {STATUS_RU.get(lead['status'], lead['status'])}"
              + (f" · {LABELS_RU.get(lead['last_label'], '')}" if lead["last_label"] else "")]
    if lead["demo_url"]:
        lines.append(f"Демо: {html.escape(lead['demo_url'])}")
    for m in (await crm.messages(lead_id))[-6:]:
        author = "Вы" if m["direction"] == "out" else "Клиент"
        lines.append(f"\n<b>{author}</b> ({m['date'][5:16]}):\n{html.escape(m['text'][:600])}")
    markup = kb([
        [("✍️ Ответить", f"out:rep:{lead_id}"), ("🤖 Черновик ответа", f"out:draft:{lead_id}")],
        [("📞 Созвон", f"out:st:{lead_id}:meeting"), ("✅ Сделка", f"out:st:{lead_id}:won"),
         ("❌ Отказ", f"out:st:{lead_id}:lost")],
        [("⛔ Не писать", f"out:st:{lead_id}:stopped"), ("💬 Ответы", "out:inbox")],
    ])
    return "\n".join(lines), markup


@router.callback_query(F.data == "out:inbox")
async def on_inbox(callback: CallbackQuery, crm: CRM) -> None:
    await callback.answer()
    replies = await crm.recent_replies(15)
    if not replies:
        await safe_edit(callback, "Ответов пока нет.", kb([[("⬅️ Рассылки", "menu:out")]]))
        return
    lines = ["<b>💬 Последние ответы</b>"]
    rows, seen = [], set()
    for r in replies:
        who = r["company"] or r["name"] or "лид"
        lines.append(f"• {LABELS_RU.get(r['label'], r['label'])} — {html.escape(who[:30])}: "
                     f"{html.escape(r['text'][:80])}")
        if r["lead_id"] not in seen:
            seen.add(r["lead_id"])
            rows.append([(who[:30], f"out:l:{r['lead_id']}")])
    await safe_edit(callback, "\n".join(lines), kb(rows[:10] + [[("⬅️ Рассылки", "menu:out")]]), parse_mode="HTML")


@router.callback_query(F.data.startswith("out:l:"))
async def on_lead(callback: CallbackQuery, crm: CRM) -> None:
    await callback.answer()
    text, markup = await lead_card(crm, _num(callback))
    await safe_edit(callback, text, markup, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("out:st:"))
async def on_status(callback: CallbackQuery, crm: CRM) -> None:
    _, _, lead_id, status = callback.data.split(":")
    if status not in STATUS_RU:
        await callback.answer()
        return
    lead = await crm.get_lead(int(lead_id))
    await crm.update_lead(int(lead_id), status=status, next_at="")
    if status == "stopped":
        await crm.add_stop(stop_keys(lead["tg_username"], lead["tg_user_id"], lead["phone"]), reason="вручную")
    await callback.answer(STATUS_RU[status])
    text, markup = await lead_card(crm, int(lead_id))
    await safe_edit(callback, text, markup, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("out:rep:"))
async def on_reply_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(OutStates.reply)
    await state.update_data(reply_lead=_num(callback))
    await callback.message.answer("Напишите ответ — он уйдёт клиенту с того же аккаунта. /cancel — отмена.")


@router.message(OutStates.reply, F.text, ~F.text.startswith("/"))
async def on_reply_text(message: Message, state: FSMContext, crm: CRM, outreach: OutreachService) -> None:
    lead_id = (await state.get_data()).get("reply_lead")
    await state.clear()
    try:
        await outreach.send_manual(lead_id, message.text)
    except Exception as exc:
        await message.answer(f"Не отправлено: {exc}")
        return
    text, markup = await lead_card(crm, lead_id, header="✅ Отправлено")
    await message.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("out:draft:"))
async def on_draft(callback: CallbackQuery, state: FSMContext, outreach: OutreachService) -> None:
    await callback.answer("Пишу черновик…")
    lead_id = _num(callback)
    try:
        draft = await outreach.draft_reply(lead_id)
    except (LLMError, LookupError) as exc:
        await callback.message.answer(f"Не получилось: {exc}")
        return
    await state.update_data(draft=draft, draft_lead=lead_id)
    await callback.message.answer(
        f"🤖 Черновик:\n\n{html.escape(draft)}", parse_mode="HTML",
        reply_markup=kb([[("📤 Отправить", f"out:sd:{lead_id}"), ("✍️ Свой текст", f"out:rep:{lead_id}")]]))


@router.callback_query(F.data.startswith("out:sd:"))
async def on_send_draft(callback: CallbackQuery, state: FSMContext, crm: CRM, outreach: OutreachService) -> None:
    data = await state.get_data()
    lead_id = _num(callback)
    if data.get("draft_lead") != lead_id or not data.get("draft"):
        await callback.answer("Черновик устарел — сгенерируйте заново", show_alert=True)
        return
    try:
        await outreach.send_manual(lead_id, data["draft"])
    except Exception as exc:
        await callback.answer(f"Не отправлено: {exc}"[:190], show_alert=True)
        return
    await state.update_data(draft=None)
    await callback.answer("Отправлено")
    text, markup = await lead_card(crm, lead_id, header="✅ Отправлено")
    await safe_edit(callback, text, markup, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("out:pick:"))
async def on_pick_campaign(callback: CallbackQuery, crm: CRM) -> None:
    """From a finished search: choose which campaign gets these companies."""
    await callback.answer()
    sid = _num(callback)
    campaigns = await crm.list_campaigns()
    if not campaigns:
        await callback.message.answer("Сначала создайте кампанию.", reply_markup=kb([[("➕ Новая кампания", "out:new")]]))
        return
    rows = [[(c["name"][:40], f"out:am:{c['id']}:{sid}")] for c in campaigns[:10]]
    await callback.message.answer("В какую кампанию добавить?", reply_markup=kb(rows + [[("➕ Новая", "out:new")]]))
