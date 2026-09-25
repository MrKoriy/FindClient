"""Personalized one-page demo landing for a lead: copy (LLM or template), HTML render, storage."""

from __future__ import annotations

import datetime as _dt
import html
import logging
import os
import re
import secrets
import string
from pathlib import Path
from typing import Any
from urllib.parse import quote

from models.organization import Organization

log = logging.getLogger(__name__)

PALETTES: dict[str, dict[str, str]] = {
    "blue": {"p": "#2563eb", "p2": "#172554", "a": "#38bdf8", "soft": "#eff6ff"},
    "green": {"p": "#059669", "p2": "#052e16", "a": "#a3e635", "soft": "#ecfdf5"},
    "graphite": {"p": "#334155", "p2": "#0b1120", "a": "#f59e0b", "soft": "#f1f5f9"},
    "orange": {"p": "#ea580c", "p2": "#431407", "a": "#fbbf24", "soft": "#fff7ed"},
    "purple": {"p": "#7c3aed", "p2": "#2e1065", "a": "#f9a8d4", "soft": "#f5f3ff"},
    "teal": {"p": "#0d9488", "p2": "#042f2e", "a": "#5eead4", "soft": "#f0fdfa"},
}
DEFAULT_PALETTE = "blue"

SLUG_RE = re.compile(r"^[a-z0-9-]{3,80}$")
_TEMPLATES = Path(__file__).resolve().parent.parent / "web" / "templates"
_BASE36 = string.digits + string.ascii_lowercase

# ------------------------------------------------------------------ fallback copy

_NICHES: dict[str, dict[str, Any]] = {
    "construction": {
        "keys": ("строит", "стройк", "ремонт", "отделк", "кровл", "фасад", "бригад", "коттедж",
                 "монтаж", "сантехн", "электромонт", "бетон", "фундамент", "дизайн интерьер"),
        "palette": "graphite",
        "headline": "Ремонт и строительство под ключ{in_city}",
        "subheadline": "{name}: смета за 1 день, фиксированная цена в договоре и контроль "
                       "качества на каждом этапе.",
        "services": [
            ("Ремонт квартир под ключ", "От черновой отделки до расстановки мебели — одна бригада и один договор."),
            ("Строительство домов", "Каркасные, газобетонные и кирпичные дома с проектом и сметой."),
            ("Отделочные работы", "Штукатурка, плитка, ламинат, натяжные потолки и покраска."),
            ("Электрика и сантехника", "Разводка, замена труб и проводки по нормам и с гарантией."),
            ("Кровля и фасады", "Монтаж и ремонт кровли, утепление и облицовка фасада."),
            ("Бесплатный замер и смета", "Инженер приедет, всё измерит и посчитает стоимость без предоплаты."),
        ],
        "advantages": ["Фиксированная смета в договоре", "Гарантия на все работы",
                       "Фотоотчёты о ходе работ", "Оплата поэтапно"],
        "about": "{name} — команда мастеров с опытом в ремонте и строительстве{in_city}. Работаем "
                 "по договору, помогаем с закупкой материалов и сдаём объекты в срок. "
                 "Вы всегда знаете, на каком этапе работы и за что платите.",
        "cta": "Вызвать замерщика бесплатно",
        "faq": [
            ("Сколько стоит ремонт?", "Стоимость зависит от площади и материалов. После бесплатного замера вы получите подробную смету — она фиксируется в договоре."),
            ("Даёте ли вы гарантию?", "Да, на все виды работ действует гарантия, она прописана в договоре."),
            ("Можно ли платить поэтапно?", "Да, оплата делится на этапы: вы платите только за выполненную и принятую работу."),
        ],
    },
    "medical": {
        "keys": ("стомат", "зуб", "дент", "клиник", "медиц", "врач", "имплант", "ортодонт",
                 "поликлин", "диагност", "ветеринар"),
        "palette": "teal",
        "headline": "Здоровая улыбка без боли и страха{in_city}",
        "subheadline": "{name}: современное оборудование, внимательные врачи и честный план "
                       "лечения с ценой до начала работ.",
        "services": [
            ("Лечение зубов", "Лечение кариеса и каналов с современной анестезией."),
            ("Имплантация", "Установка имплантов и коронок под ключ с гарантией."),
            ("Профессиональная гигиена", "Ультразвуковая чистка, Air Flow и фторирование за один визит."),
            ("Протезирование", "Коронки, виниры и протезы из надёжных материалов."),
            ("Детская стоматология", "Бережный подход к маленьким пациентам и лечение без стресса."),
            ("Консультация и план лечения", "Осмотр, снимок и понятный план с фиксированной стоимостью."),
        ],
        "advantages": ["Лечение без боли", "Цена фиксируется до начала лечения",
                       "Опытные врачи", "Удобная запись"],
        "about": "{name} — клиника, где ценят ваше время и комфорт. Используем современное "
                 "оборудование, соблюдаем строгие стандарты стерильности и объясняем каждый шаг лечения.",
        "cta": "Записаться на приём",
        "faq": [
            ("Будет ли больно?", "Мы используем современную анестезию, поэтому лечение проходит комфортно даже для самых чувствительных пациентов."),
            ("Сколько стоит консультация?", "Стоимость первичной консультации уточняйте по телефону — администратор подскажет и подберёт удобное время."),
            ("Можно ли лечиться в рассрочку?", "Для объёмного лечения можно обсудить поэтапную оплату — уточните условия у администратора."),
        ],
    },
    "auto": {
        "keys": ("авто", "шиномонт", "кузов", " сто ", "эвакуат", "шины", "тонир", "детейлинг",
                 "двигател", "развал"),
        "palette": "orange",
        "headline": "Автосервис, которому доверяют{in_city}",
        "subheadline": "{name}: диагностика, ремонт и обслуживание автомобиля с гарантией и "
                       "прозрачной ценой.",
        "services": [
            ("Компьютерная диагностика", "Находим причину неисправности быстро и точно."),
            ("Техническое обслуживание", "Замена масла, фильтров и расходников по регламенту."),
            ("Ремонт двигателя и ходовой", "Ремонт любой сложности с качественными запчастями."),
            ("Шиномонтаж", "Сезонная замена шин, балансировка и хранение колёс."),
            ("Кузовной ремонт", "Рихтовка, покраска и полировка с подбором цвета."),
            ("Автоэлектрика", "Диагностика и ремонт электрооборудования любых марок."),
        ],
        "advantages": ["Гарантия на работы", "Согласуем цену до ремонта",
                       "Не навязываем лишнего", "Запись без очередей"],
        "about": "{name} — автосервис с опытными мастерами и профессиональным оборудованием. "
                 "Честно называем цену заранее и не навязываем лишних работ.",
        "cta": "Записаться на сервис",
        "faq": [
            ("Сколько длится диагностика?", "Обычно 30–60 минут. После неё вы получите список работ и точную стоимость."),
            ("Можно со своими запчастями?", "Да, можем установить ваши запчасти или подобрать проверенные у поставщиков."),
            ("Есть ли гарантия?", "Да, на выполненные работы действует гарантия — условия зафиксируем в заказ-наряде."),
        ],
    },
    "beauty": {
        "keys": ("красот", "парикмах", "барбер", "маникюр", "космет", "ногт", "спа", "spa",
                 "бров", "ресниц", "визаж", "эпиляц", "nail", "массаж"),
        "palette": "purple",
        "headline": "Красота и уход, к которым хочется возвращаться",
        "subheadline": "{name}: опытные мастера, профессиональная косметика и уютная атмосфера{in_city}.",
        "services": [
            ("Стрижки и укладки", "Женские, мужские и детские стрижки, укладки для любого события."),
            ("Окрашивание", "Сложные техники окрашивания и бережный уход за волосами."),
            ("Маникюр и педикюр", "Аппаратный маникюр, покрытие гель-лаком и дизайн."),
            ("Брови и ресницы", "Коррекция, окрашивание, ламинирование и наращивание."),
            ("Уход за лицом", "Чистка, пилинги и уходовые процедуры по типу кожи."),
            ("Подарочные сертификаты", "Приятный подарок для близких на любую сумму."),
        ],
        "advantages": ["Опытные мастера", "Стерильные инструменты",
                       "Профессиональная косметика", "Запись в удобное время"],
        "about": "{name} — место, где о вас заботятся. Подберём услугу под ваш запрос, "
                 "работаем аккуратно и ценим ваше время.",
        "cta": "Записаться",
        "faq": [
            ("Как записаться?", "Позвоните нам или напишите в мессенджер — подберём удобное время и мастера."),
            ("Можно перенести запись?", "Конечно — просто предупредите нас заранее, и мы подберём другое время."),
            ("Какую косметику вы используете?", "Работаем на профессиональной косметике проверенных брендов."),
        ],
    },
    "legal": {
        "keys": ("юрист", "юридич", "адвокат", "нотари", "бухгалт", "банкрот", "правов"),
        "palette": "blue",
        "headline": "Юридическая помощь, которая решает задачи",
        "subheadline": "{name}: разберёмся в ситуации, предложим стратегию и доведём дело до результата.",
        "services": [
            ("Консультация юриста", "Разбор ситуации и понятный план действий уже на первой встрече."),
            ("Споры и суды", "Представительство в судах общей юрисдикции и арбитраже."),
            ("Банкротство физлиц", "Законное списание долгов с полным сопровождением процедуры."),
            ("Договоры и документы", "Составление и проверка договоров, претензий и исков."),
            ("Сопровождение бизнеса", "Абонентское юридическое обслуживание компаний и ИП."),
            ("Семейные и наследственные дела", "Разводы, раздел имущества, алименты, наследство."),
        ],
        "advantages": ["Стоимость известна заранее", "Конфиденциальность",
                       "Опыт в судебной практике", "Работаем по договору"],
        "about": "{name} — юристы, которые говорят понятным языком. Оцениваем перспективы "
                 "честно и работаем на результат клиента{in_city}.",
        "cta": "Получить консультацию",
        "faq": [
            ("Сколько стоит консультация?", "Стоимость зависит от вопроса — уточните по телефону, мы сразу скажем цену."),
            ("Можно ли решить вопрос удалённо?", "Да, многие задачи решаются онлайн: документы, консультации и подготовка исков."),
            ("Вы гарантируете результат?", "Мы честно оцениваем перспективы дела и берёмся только за задачи, в которых можем помочь."),
        ],
    },
    "generic": {
        "keys": (),
        "palette": "blue",
        "headline": "{name}",
        "subheadline": "{category_cap}{in_city}: качественно, в срок и по честной цене.",
        "services": [
            ("Консультация", "Расскажем об услугах и поможем выбрать оптимальное решение."),
            ("Индивидуальный подход", "Учитываем ваши задачи, сроки и бюджет."),
            ("Работа под ключ", "Берём на себя все этапы — от заявки до результата."),
            ("Гарантия качества", "Отвечаем за результат и соблюдаем договорённости."),
        ],
        "advantages": ["Опыт и репутация", "Честные цены", "Работаем в срок", "Удобная связь"],
        "about": "{name} — надёжный партнёр{in_city}. Ценим каждого клиента и дорожим репутацией.",
        "cta": "Оставить заявку",
        "faq": [
            ("Как с вами связаться?", "Позвоните по телефону на сайте или напишите в мессенджер — ответим быстро."),
            ("Сколько стоят услуги?", "Стоимость зависит от задачи. Позвоните — рассчитаем цену за несколько минут."),
            ("Где вы находитесь?", "Адрес и контакты указаны в разделе «Контакты» внизу страницы."),
        ],
    },
}


def _in_city(city: str) -> str:
    """' в Казани' — naive locative; multi-word names fall back to ' в г. …'."""
    city = city.strip()
    if not city:
        return ""
    if " " in city or not re.fullmatch(r"[А-ЯЁа-яё-]+", city):
        return f" в г. {city}"
    head, sep, tail = city.partition("-на-")
    last = head[-1].lower()
    if last == "й":
        return f" в г. {city}"
    if last == "ь":
        head = head[:-1] + "и"
    elif last in "ая":
        head = head[:-1] + "е"
    elif last == "ы":
        head = head[:-1] + "ах"
    elif last not in "иоуэюе":  # indeclinable endings stay as is
        head += "е"
    return f" в {head}{sep}{tail}"


def detect_niche(org: Organization) -> str:
    text = f" {org.category} {org.name} ".lower().replace("ё", "е")
    for niche, spec in _NICHES.items():
        if any(k in text for k in spec["keys"]):
            return niche
    return "generic"


def fallback_content(org: Organization) -> dict:
    spec = _NICHES[detect_niche(org)]
    category = (org.category or "").split(",")[0].strip()
    fmt = {
        "name": org.name.strip() or "Наша компания",
        "in_city": _in_city(org.city),
        "category_cap": (category[:1].upper() + category[1:]) if category else "Услуги",
    }
    return {
        "headline": spec["headline"].format(**fmt),
        "subheadline": spec["subheadline"].format(**fmt),
        "services": [{"title": t, "text": x} for t, x in spec["services"]],
        "advantages": list(spec["advantages"]),
        "about": spec["about"].format(**fmt),
        "cta": spec["cta"],
        "faq": [{"q": q, "a": a} for q, a in spec["faq"]],
        "palette": spec["palette"],
    }


# ------------------------------------------------------------------ LLM copy

_SYSTEM = (
    "Ты — сильный копирайтер лендингов для малого бизнеса в России. Пишешь по-русски: "
    "конкретно, тепло, без воды и канцелярита. Не выдумывай цифры (цены, стаж, число клиентов), "
    "лицензии, акции и факты, которых нет во входных данных. Без HTML и эмодзи.\n"
    "Верни JSON строго такой структуры:\n"
    '{"headline": "заголовок до 70 символов", "subheadline": "подзаголовок до 160 символов", '
    '"services": [{"title": "услуга до 40 символов", "text": "1 предложение"}] (4-6 шт), '
    '"advantages": ["короткое преимущество до 40 символов"] (3-4 шт), '
    '"about": "абзац о компании, 2-3 предложения", "cta": "текст кнопки до 30 символов", '
    '"faq": [{"q": "вопрос клиента", "a": "ответ 1-2 предложения"}] (ровно 3), '
    f'"palette": одно из {", ".join(PALETTES)} — под нишу}}'
)


def _clean(value: Any, limit: int) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]*>", " ", value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip(" ,.;:—-") + "…"
    return text


def normalize_content(raw: Any, fallback: dict) -> dict:
    """Clamp LLM output to the expected shape; anything missing comes from `fallback`."""
    if not isinstance(raw, dict):
        return fallback
    out: dict[str, Any] = {}
    for key, limit in (("headline", 110), ("subheadline", 260), ("about", 900), ("cta", 40)):
        out[key] = _clean(raw.get(key), limit) or fallback[key]

    services = []
    for item in raw.get("services") or []:
        if isinstance(item, dict):
            title, text = _clean(item.get("title"), 60), _clean(item.get("text"), 240)
        else:
            title, text = _clean(item, 60), ""
        if title:
            services.append({"title": title, "text": text})
    services = services[:6]
    have = {s["title"].lower() for s in services}
    for s in fallback["services"]:
        if len(services) >= 4:
            break
        if s["title"].lower() not in have:
            services.append(s)
    out["services"] = services

    advantages = []
    for item in raw.get("advantages") or []:
        if isinstance(item, dict):
            item = item.get("title") or item.get("text")
        text = _clean(item, 80)
        if text:
            advantages.append(text)
    advantages = advantages[:4]
    for a in fallback["advantages"]:
        if len(advantages) >= 3:
            break
        if a not in advantages:
            advantages.append(a)
    out["advantages"] = advantages

    faq = []
    for item in raw.get("faq") or []:
        if isinstance(item, dict):
            q, a = _clean(item.get("q"), 160), _clean(item.get("a"), 600)
            if q and a:
                faq.append({"q": q, "a": a})
    faq = faq[:3]
    for f in fallback["faq"]:
        if len(faq) >= 3:
            break
        faq.append(f)
    out["faq"] = faq

    palette = str(raw.get("palette") or "").strip().lower()
    out["palette"] = palette if palette in PALETTES else fallback["palette"]
    return out


def _org_brief(org: Organization, offer: str) -> str:
    lines = [f"Компания: {org.name}"]
    for label, value in (("Сфера", org.category), ("Город", org.city), ("Адрес", org.address)):
        if value:
            lines.append(f"{label}: {value}")
    if org.rating:
        lines.append(f"Рейтинг на картах: {org.rating} ({org.reviews} отзывов)")
    if org.branches > 1:
        lines.append(f"Филиалов: {org.branches}")
    if offer.strip():
        lines.append(f"Пожелания к тексту: {offer.strip()}")
    lines.append("Напиши тексты для одностраничного сайта этой компании.")
    return "\n".join(lines)


async def generate_content(org: Organization, llm: Any, offer: str = "") -> dict:
    fallback = fallback_content(org)
    if not getattr(llm, "enabled", False):
        return fallback
    try:
        raw = await llm.chat_json(_SYSTEM, _org_brief(org, offer), temperature=0.8, max_tokens=1800)
    except Exception as exc:  # LLMError, network, bad JSON — the demo must still be built
        log.warning("demo copy via LLM failed for %s: %s", org.name, exc)
        return fallback
    return normalize_content(raw, fallback)


# ------------------------------------------------------------------ HTML

_ICON_PHONE = ('<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" '
               'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 '
               '2 0 0 1-2.18 2 19.8 19.8 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.12 4.18 '
               '2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.1 9.9a16 '
               '16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.9.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z"/></svg>')
_ICON_PIN = ('<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" '
             'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 '
             '13-9 13S3 17 3 10a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>')
_ICON_CHAT = ('<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" '
              'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 '
              '1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>')
_ICON_CHECK = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
               'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>')

_SOCIAL_LABELS = (
    (r"(wa\.me|whatsapp)", "WhatsApp"), (r"(t\.me|telegram)", "Telegram"),
    (r"vk\.(com|ru)", "ВКонтакте"), (r"max\.ru", "MAX"), (r"ok\.ru", "Одноклассники"),
    (r"instagram", "Instagram"), (r"youtube|youtu\.be", "YouTube"), (r"dzen\.ru", "Дзен"),
    (r"viber", "Viber"), (r"rutube", "Rutube"),
)


def _e(text: Any) -> str:
    return html.escape(str(text or ""), quote=True)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _phones(org: Organization) -> list[tuple[str, str]]:
    out = []
    for raw in re.split(r"[,;]", org.phone or ""):
        raw = raw.strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 5:
            continue
        if len(digits) == 11 and digits[0] == "8":
            digits = "7" + digits[1:]
        href = "+" + digits if len(digits) >= 11 or raw.startswith("+") else digits
        out.append((raw, href))
    return out


def _socials(org: Organization) -> list[tuple[str, str]]:
    out, seen = [], set()
    for url in re.split(r"[,\s]+", org.socials or ""):
        url = url.strip()
        if not re.match(r"^https?://[^\s\"'<>]+$", url, re.I) or url in seen:
            continue
        seen.add(url)
        label = next((name for pat, name in _SOCIAL_LABELS if re.search(pat, url, re.I)), None)
        if not label:
            label = re.sub(r"^https?://(www\.)?", "", url, flags=re.I).split("/")[0]
        out.append((label, url))
    return out[:8]


def _load_css() -> str:
    try:
        return (_TEMPLATES / "demo.css").read_text(encoding="utf-8")
    except OSError:
        log.warning("demo.css not found in %s", _TEMPLATES)
        return ""


def render_html(org: Organization, content: dict) -> str:
    content = normalize_content(content, fallback_content(org))
    pal = PALETTES.get(content["palette"], PALETTES[DEFAULT_PALETTE])
    name = org.name.strip() or "Компания"
    category = (org.category or "").split(",")[0].strip()
    phones = _phones(org)
    socials = _socials(org)
    initial = _e(name.lstrip("«\"' ")[:1].upper() or "•")
    tel = f"tel:{_e(phones[0][1])}" if phones else "#contacts"
    call_label = "Позвонить" if phones else _e(content["cta"])

    badge_bits = [b for b in (category[:1].upper() + category[1:] if category else "", org.city.strip()) if b]
    badge = f'<span class="badge">{_e(" · ".join(badge_bits))}</span>' if badge_bits else ""

    rating = float(org.rating or 0)
    reviews = int(org.reviews or 0)
    rating_txt = f"{rating:.1f}".replace(".", ",")
    stars_w = max(0.0, min(rating / 5 * 100, 100))
    reviews_txt = f"{reviews} {_plural(reviews, 'отзыв', 'отзыва', 'отзывов')}" if reviews else "рейтинг на картах"

    # hero side card
    card_rows = []
    if rating > 0:
        card_rows.append(
            f'<div class="hc-rating"><b>{rating_txt}</b><div><span class="stars"><span style="width:'
            f'{stars_w:.0f}%">★★★★★</span>★★★★★</span><small>{_e(reviews_txt)}</small></div></div>'
        )
    for adv in content["advantages"][:3]:
        card_rows.append(f'<div class="hc-row"><i>{_ICON_CHECK}</i>{_e(adv)}</div>')
    if phones:
        card_rows.append(f'<a class="btn btn-main btn-block" href="{tel}">{_ICON_PHONE}{_e(phones[0][0])}</a>')
    else:
        card_rows.append(f'<a class="btn btn-main btn-block" href="#contacts">{_e(content["cta"])}</a>')
    hero_card = f'<div class="hero-card">{"".join(card_rows)}</div>'

    services = "".join(
        f'<article class="card"><span class="num">{i:02d}</span><h3>{_e(s["title"])}</h3>'
        + (f'<p>{_e(s["text"])}</p>' if s["text"] else "")
        + "</article>"
        for i, s in enumerate(content["services"], 1)
    )
    advantages = "".join(
        f'<div class="adv"><i>{_ICON_CHECK}</i><span>{_e(a)}</span></div>' for a in content["advantages"]
    )
    faq = "".join(
        f'<details{" open" if i == 0 else ""}><summary>{_e(f["q"])}</summary><p>{_e(f["a"])}</p></details>'
        for i, f in enumerate(content["faq"])
    )

    rating_block = ""
    if rating > 0:
        rating_block = (
            f'<div class="rating-card"><div class="rc-num">{rating_txt}</div>'
            f'<span class="stars big"><span style="width:{stars_w:.0f}%">★★★★★</span>★★★★★</span>'
            f'<p><b>{_e(reviews_txt)}</b> от клиентов на картах</p>'
            f'<small>Реальные оценки клиентов в 2ГИС и Яндекс Картах</small></div>'
        )

    contact_cards = []
    if phones:
        links = "".join(f'<a href="tel:{_e(h)}">{_e(d)}</a>' for d, h in phones[:3])
        contact_cards.append(f'<div class="ccard"><i>{_ICON_PHONE}</i><div><small>Телефон</small>{links}</div></div>')
    if org.address.strip() or org.city.strip():
        full = ", ".join(x for x in (org.city.strip(), org.address.strip()) if x)
        map_url = "https://yandex.ru/maps/?text=" + quote(f"{full}, {name}")
        contact_cards.append(
            f'<div class="ccard"><i>{_ICON_PIN}</i><div><small>Адрес</small><span>{_e(full)}</span>'
            f'<a class="link" href="{_e(map_url)}" target="_blank" rel="noopener noreferrer">Открыть на карте →</a></div></div>'
        )
    if socials:
        chips = "".join(
            f'<a class="chip" href="{_e(u)}" target="_blank" rel="noopener noreferrer">{_e(l)}</a>' for l, u in socials
        )
        contact_cards.append(f'<div class="ccard"><i>{_ICON_CHAT}</i><div><small>Мессенджеры и соцсети</small><div class="chips">{chips}</div></div></div>')

    top_phone = f'<a class="top-phone" href="{tel}">{_ICON_PHONE}<span>{_e(phones[0][0])}</span></a>' if phones else ""
    year = _dt.date.today().year

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<title>{_e(name)}{f" — {_e(category)}" if category else ""}</title>
<meta name="description" content="{_e(content["subheadline"])}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>:root{{--p:{pal["p"]};--p2:{pal["p2"]};--a:{pal["a"]};--soft:{pal["soft"]}}}
{_load_css()}</style>
</head>
<body>
<header class="hero">
<div class="glow g1"></div><div class="glow g2"></div>
<nav class="wrap top"><a class="logo" href="#"><i>{initial}</i><span>{_e(name)}</span></a>{top_phone}</nav>
<div class="wrap hero-grid">
<div class="hero-text">{badge}<h1>{_e(content["headline"])}</h1><p class="lead">{_e(content["subheadline"])}</p>
<div class="btns"><a class="btn btn-main" href="{tel}">{_ICON_PHONE}{call_label}</a><a class="btn btn-ghost" href="#services">Наши услуги</a></div></div>
{hero_card}
</div>
</header>
<main>
<section id="services" class="sec"><div class="wrap">
<div class="sec-head"><span class="eyebrow">Услуги</span><h2>Чем мы можем помочь</h2></div>
<div class="grid">{services}</div>
</div></section>
<section class="sec soft"><div class="wrap">
<div class="sec-head"><span class="eyebrow">Почему мы</span><h2>Нам доверяют, потому что</h2></div>
<div class="advs">{advantages}</div>
</div></section>
<section id="about" class="sec"><div class="wrap about{" has-rating" if rating_block else ""}">
<div><span class="eyebrow">О компании</span><h2>{_e(name)}</h2><p class="about-text">{_e(content["about"])}</p></div>
{rating_block}
</div></section>
<section class="sec soft"><div class="wrap narrow">
<div class="sec-head"><span class="eyebrow">Вопросы</span><h2>Частые вопросы</h2></div>
<div class="faq">{faq}</div>
</div></section>
<section class="sec cta-sec"><div class="wrap"><div class="cta-band">
<div><h2>{_e(content["cta"])}</h2><p>Ответим на вопросы и подскажем лучшее решение под вашу задачу.</p></div>
<a class="btn btn-main" href="{tel}">{_ICON_PHONE}{call_label}</a>
</div></div></section>
<section id="contacts" class="sec contacts"><div class="wrap">
<div class="sec-head"><span class="eyebrow">Контакты</span><h2>Свяжитесь с нами</h2></div>
<div class="cgrid">{"".join(contact_cards) or f"<p>{_e(name)}</p>"}</div>
</div></section>
</main>
<footer class="foot"><div class="wrap">© {year} {_e(name)}<br><span>Демо-версия сайта. Подготовлено для {_e(name)}</span></div></footer>
<a class="sticky btn btn-main" href="{tel}">{_ICON_PHONE}{call_label}</a>
</body>
</html>
"""


# ------------------------------------------------------------------ storage

_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
     "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"],
))


def make_slug(org: Organization) -> str:
    base = "".join(_TRANSLIT.get(ch, ch) for ch in (org.name or "").lower())
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:24].strip("-") or "demo"
    return f"{base}-{''.join(secrets.choice(_BASE36) for _ in range(6))}"


class DemoSiteStore:
    """Flat directory of `<slug>.html` files."""

    def __init__(self, directory: str = "demo_sites") -> None:
        self.directory = Path(directory)

    @staticmethod
    def valid_slug(slug: Any) -> bool:
        return isinstance(slug, str) and bool(SLUG_RE.fullmatch(slug))

    def save(self, slug: str, html_text: str) -> str:
        if not self.valid_slug(slug):
            raise ValueError(f"bad slug: {slug!r}")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{slug}.html"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(html_text, encoding="utf-8")
        os.replace(tmp, path)
        return str(path)

    def get(self, slug: str) -> str | None:
        if not self.valid_slug(slug):
            return None
        path = self.directory / f"{slug}.html"
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError):
            return None


async def build_demo(org: Organization, llm: Any, store: DemoSiteStore, base_url: str, offer: str = "") -> str:
    content = await generate_content(org, llm, offer)
    slug = make_slug(org)
    store.save(slug, render_html(org, content))
    return f"{base_url.rstrip('/')}/d/{slug}"
