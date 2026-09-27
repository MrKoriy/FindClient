"""Детерминированный движок оценки оффера (Jev Rules).

Четыре измерения по 25 баллов: крючок и персонализация, ценность и оффер,
призыв к действию, антиспам-риски. Работает автономно - без внешних API -
и служит нижней границей качества: AI-оценка смешивается с ним поровну.
"""

from __future__ import annotations

import re
from typing import Any

from crm import templates as tpl


def evaluate_hook_and_personalization(text: str, target: dict[str, Any]) -> tuple[int, list[str]]:
    """Оценка крючка и персонализации (максимум 25 баллов)."""
    score = 0
    notes = []

    lower = text.lower()
    first_sentence = (re.split(r"[.!?]", text)[0] if text else "").strip().lower()

    # Проверка: первая строка про них, а не про нас
    we_starts = ("я ", "мы ", "наша компания", "меня зовут", "хочу предложить", "предлагаю вам")
    if any(first_sentence.startswith(w) or f" {w}" in first_sentence for w in we_starts):
        notes.append("Начало с рассказа о себе («я/мы»), а не о бизнесе клиента")
    else:
        score += 10

    # Проверка персонализации: упоминание фактов, отзывов, ниши, рейтинга или города
    has_facts = False
    if target:
        name = (target.get("name") or "").lower()
        city = (target.get("city") or "").lower()
        cat = (target.get("category") or "").lower()
        if name and name in lower:
            has_facts = True
        if city and city in lower:
            has_facts = True
        if cat and cat in lower:
            has_facts = True

    if "отзыв" in lower or "карт" in lower or "2gis" in lower or "рейтинг" in lower or has_facts:
        score += 10
    else:
        notes.append("Мало персонализации: нет привязки к картам, отзывам или сфере")

    # Естественное приветствие без шаблонного тона
    greetings = ("здравствуйте", "добрый день", "доброго дня")
    if any(lower.startswith(g) for g in greetings):
        score += 5
    elif lower.startswith("привет"):
        score += 2
        notes.append("«Привет» в холодном B2B может звучать фамильярно")
    else:
        notes.append("Нет уважительного приветствия в начале")

    return min(score, 25), notes


def evaluate_value_and_offer(text: str, target: dict[str, Any]) -> tuple[int, list[str]]:
    """Оценка ценности и конкретики оффера (максимум 25 баллов)."""
    score = 0
    notes = []
    lower = text.lower()

    # Наличие понятной проблемы или контекста
    problem_triggers = (
        "сайта нет", "нет сайта", "нет своего сайта", "без сайта", "вместо сайта",
        "не доходят", "уходят", "теряете", "поиске", "конкурент"
    )
    if any(t in lower for t in problem_triggers):
        score += 10
    else:
        notes.append("Не подсвечена понятная проблема клиента (потеря клиентов, отсутствие сайта)")

    # Конкретное решение / скорость / формат
    solution_triggers = ("три дня", "3 дня", "за пару минут", "концепт", "пример", "собираю", "страниц", "квиз", "сайт")
    if any(t in lower for t in solution_triggers):
        score += 10
    else:
        notes.append("Размытое предложение: нет конкретики, что именно предлагается")

    # Отсутствие пустого инфобизнес-жаргона
    buzzwords = ("уникальн", "взрывн", "лучшие в мире", "гарантия 100%", "супер-предложение", "только сегодня")
    if any(b in lower for b in buzzwords):
        notes.append("Обнаружен рекламный клише-жаргон")
    else:
        score += 5

    return min(score, 25), notes


def evaluate_cta_and_friction(text: str) -> tuple[int, list[str]]:
    """Оценка призыва к действию и трения (максимум 25 баллов)."""
    score = 0
    notes = []

    questions = text.count("?")
    if questions == 1:
        score += 15
    elif questions == 0:
        notes.append("Нет вопросительного призыва к действию в конце")
    else:
        notes.append(f"Больше одного вопроса ({questions}). В холодном сообщении нужен ровно один")

    # Низкое трение: легкий вопрос согласия
    low_friction = ("есть смысл", "взглянете", "посмотреть", "интересно", "показать", "говорить дальше")
    high_friction = ("созвониться", "номер телефона", "встретиться", "купите", "оплатите", "выставим счет")

    lower = text.lower()
    if any(h in lower for h in high_friction):
        notes.append("Высокое трение в первом сообщении: предложение звонка/встречи/оплаты отпугивает")
    elif any(w in lower for w in low_friction):
        score += 10
    else:
        score += 5

    return min(score, 25), notes


def evaluate_antispam_and_risk(raw_text: str, with_link: bool = False) -> tuple[int, list[str]]:
    """Оценка спам-рисков и надежности доставки (максимум 25 баллов)."""
    score = 25
    notes = []

    # 1. Длина сообщения (до 50 слов)
    words = len(re.findall(r"[А-Яа-яA-Za-z0-9]+", raw_text))
    if words > 50:
        over = words - 50
        deduction = min(15, 5 + over // 2)
        score -= deduction
        notes.append(f"{words} слов (лимит {tpl.WORD_LIMIT}): холодные тексты длиннее 50 слов игнорируются")
    elif words < 12:
        score -= 5
        notes.append(f"Слишком коротко ({words} слов): не успевает сформировать оффер")

    # 2. Длинное тире - спам/AI маркер (проверяем исходный текст)
    if "\u2014" in raw_text or "\u2013" in raw_text:
        score -= 8
        notes.append("Длинное тире (\\u2014/\\u2013) - явный маркер AI или рассылки, замените на дефис или точку")

    # 3. Прайс в первом сообщении
    has_price = bool(re.search(r"\d[\d\s]{2,}\s*(руб|₽|р\.)|\bпрайс|\bскидк|\bцен[аыуе]\b|\bоплат", raw_text, re.I))
    if has_price:
        score -= 15
        notes.append("Упоминание цены в первом сообщении провоцирует жалобу на спам")

    # 4. Ссылка в первом сообщении
    if not with_link and ("http://" in raw_text or "https://" in raw_text or "t.me/" in raw_text):
        score -= 10
        notes.append("Ссылка в первом сообщении - главный триггер для Telegram антиспама")

    # 5. Сломанные переменные / мусор
    if "{" in raw_text or "}" in raw_text:
        score -= 15
        notes.append("Остались нераскрытые переменные шаблона ({...})")

    if re.search(r"\s{2,}|[:\-]\s*[.,]|\bи\s*[.,]", raw_text):
        score -= 10
        notes.append("Следы склейки шаблона (двойные пробелы или знаки препинания без слов)")

    # 6. Падежные несогласования
    for prep in ("в", "во", "на", "из", "для", "по", "о", "об"):
        for var in ("city", "category"):
            if re.search(rf"\b{prep}\s+\{{{var}\}}", raw_text, re.I):
                score -= 10
                notes.append(f"Падеж: «{prep} {{{var}}}» даст ошибку падежа")

    return max(0, score), notes


