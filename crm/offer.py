"""Генератор офферов и классификатор качества (AI + Jev Judge).

Модуль реализует навыки (skills) для B2B cold outreach по компаниям без сайта:
1. Синтез нескольких вариантов офферов (Direct, Platform Risk, Value-First, Quiz/Calculator).
2. Выбор сильнейшего кандидата через TypeSafe Jev (System One AI decision layer).
3. Классификатор офферов AI + Jev Judge:
   - Оценка по 4 ключевым измерениям (0-100 баллов):
     * Персонализация и контекст (0-25)
     * Ценность и оффер (0-25)
     * Призыв к действию и трение (0-25)
     * Антиспам и риски блокировки (0-25)
   - Вердикт: EXCELLENT / GOOD / NEEDS_WORK / SPAM_RISK
   - Двухуровневый движок: TypeSafe Jev API (если задан TYPESAFE_API_KEY) +
     детерминированный экспертный Jev-Rules движок (работает автономно).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from dotenv import load_dotenv

load_dotenv()
load_dotenv("crm.env")

from crm import bai
from crm import templates as tpl

log = logging.getLogger("crm.offer")

TYPESAFE_URL = os.environ.get("TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone")
TYPESAFE_API_KEY = os.environ.get("TYPESAFE_API_KEY", "")
TYPESAFE_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")

BAI_BASE_URL = os.environ.get("BAI_BASE_URL", "https://api.b.ai/v1")
BAI_API_KEY = os.environ.get("BAI_API_KEY", "")
BAI_MODEL = os.environ.get("BAI_MODEL", "qwen3.8-flash")

# Проверенные стратегии офферов для компаний с карт без сайта
OFFER_STRATEGIES = {
    "lost_traffic": {
        "id": "lost_traffic",
        "name": "Потеря клиентов из поиска",
        "description": "Упор на то, что карточка в 2GIS есть, но клиенты из поиска Яндекса уходят к конкурентам.",
        "template": (
            "Здравствуйте! Нашёл вас на картах: {facts}. "
            "Сайта нет, а те, кто ищет вас в поиске, до карточки не доходят. "
            "Собираю такие сайты за три дня. Есть смысл говорить дальше?"
        ),
    },
    "social_only": {
        "id": "social_only",
        "name": "Соцсети вместо сайта",
        "description": "Для тех, у кого вместо сайта указана группа VK или соцсеть, которая не дает SEO.",
        "template": (
            "Здравствуйте! На картах у вас {facts}, а вместо сайта только "
            "страница в соцсетях, которую вы не контролируете. Те, кто ищет вас "
            "в поиске, до неё не доходят. Собираю такие сайты за три дня. "
            "Посмотреть, как это будет на ваших услугах?"
        ),
    },
    "ready_concept": {
        "id": "ready_concept",
        "name": "Готовый концепт за 3 дня",
        "description": "Демонстрация готового решения под их сферу с низким порогом входа.",
        "template": (
            "Добрый день! Обратил внимание на вашу компанию: {facts}. "
            "Сейчас как раз делаю сайты для сферы {category}. "
            "Могу прислать готовый концепт страницы под ваши услуги за пару минут. "
            "Взглянете?"
        ),
    },
    "conversion_quiz": {
        "id": "conversion_quiz",
        "name": "Квиз и форма расчета",
        "description": "Оффер на быстрый сайт-квиз, который берет контакты клиентов сразу из поиска.",
        "template": (
            "Здравствуйте! У вас отличная карточка на картах ({facts}), но нет своего сайта. "
            "Клиенты из поиска уходят к конкурентам. Собираю такие страницы-квизы за три дня под ключ. "
            "Есть смысл показать пример?"
        ),
    },
}


def get_strategies() -> list[dict[str, str]]:
    """Возвращает список доступных стратегий офферов."""
    return [
        {
            "id": s["id"],
            "name": s["name"],
            "description": s["description"],
            "sample": s["template"],
        }
        for s in OFFER_STRATEGIES.values()
    ]


def synthesize_candidates(
    target: dict[str, Any] | None = None,
    link: str = "",
    with_link: bool = False,
    prompt_hint: str = "",
    bai_key: str = "",
    bai_model: str = "",
    bai_url: str = "",
) -> list[dict[str, Any]]:
    """Генерирует 4 различных варианта оффера под конкретную цель."""
    target = target or {}
    candidates = []

    # Пробуем получить живой оффер через B.AI (Qwen 3.8 Flash + Russian Outreach & Humanizer skills)
    bkey = bai_key or os.environ.get("BAI_API_KEY", "") or BAI_API_KEY
    bmodel = bai_model or os.environ.get("BAI_MODEL", "") or BAI_MODEL or "qwen3.8-flash"
    burl = bai_url or os.environ.get("BAI_BASE_URL", "") or BAI_BASE_URL
    bai_text = None
    if bkey:
        try:
            bai_text = bai.generate_bai_offer(
                target=target,
                strategy_id="lost_traffic",
                prompt_hint=prompt_hint,
                api_key=bkey,
                model=bmodel,
                base_url=burl,
            )
        except Exception as exc:
            log.warning("B.AI offer synthesis fallback: %s", exc)

    # Кандидат 1: B.AI Qwen 3.8 Flash (или базовая стратегия Потеря клиентов из поиска)
    if bai_text:
        candidates.append({
            "id": "lost_traffic",
            "title": f"B.AI {bmodel} (скиллы РФ)",
            "text": bai_text,
            "criteria": "Synthesized by Qwen 3.8 Flash with Russian outreach and Humanizer skills,"
                        " natural tone, facts from 2GIS, loss of search traffic, zero-friction CTA",
            "source": "bai",
        })
    else:
        t1 = tpl.render(OFFER_STRATEGIES["lost_traffic"]["template"], target, link=link, with_link=with_link)
        c1_text = tpl.cleanup(t1)
        c1_title = "Потеря клиентов из поиска"
        if prompt_hint:
            hint_clean = prompt_hint.strip()
            c1_text = tpl.cleanup(
                f"Здравствуйте! Обратил внимание на вашу компанию на картах. {hint_clean}. "
                "Сделаю такой сайт за три дня. Есть смысл показать концепт?"
            )
            c1_title = "С учетом вашего пожелания"
        candidates.append({
            "id": "lost_traffic",
            "title": c1_title,
            "text": c1_text,
            "criteria": "Starts with facts, highlights search clients lost to competitors,"
                        " 3-day turnaround, low-friction closing question",
            "source": "rules",
        })

    # Кандидат 2: Соцсети вместо сайта
    t2 = tpl.render(OFFER_STRATEGIES["social_only"]["template"], target, link=link, with_link=with_link)
    candidates.append({
        "id": "social_only",
        "title": "Соцсети вместо сайта",
        "text": tpl.cleanup(t2),
        "criteria": "Points out reliance on social media profiles that don't capture"
                    " organic search, offers 3-day site demo",
        "source": "rules",
    })

    # Кандидат 3: Готовый концепт за 3 дня
    t3 = tpl.render(OFFER_STRATEGIES["ready_concept"]["template"], target, link=link, with_link=with_link)
    candidates.append({
        "id": "ready_concept",
        "title": "Готовый концепт за 3 дня",
        "text": tpl.cleanup(t3),
        "criteria": "Friendly, niche-tailored value proposition, offers a 2-minute look at ready concept",
        "source": "rules",
    })

    # Кандидат 4: Страница-квиз с расчетом цены
    t4 = tpl.render(OFFER_STRATEGIES["conversion_quiz"]["template"], target, link=link, with_link=with_link)
    candidates.append({
        "id": "conversion_quiz",
        "title": "Квиз и форма расчета",
        "text": tpl.cleanup(t4),
        "criteria": "High conversion angle, promises lead quiz for fast customer capture, respectful CTA",
        "source": "rules",
    })

    return candidates


def select_best_with_jev(
    candidates: list[dict[str, Any]],
    target: dict[str, Any] | None = None,
    api_key: str = "",
) -> dict[str, Any] | None:
    """Запрашивает у TypeSafe Jev System One выбор наиболее конверсионного кандидата."""
    key = api_key or os.environ.get("TYPESAFE_API_KEY", "") or TYPESAFE_API_KEY
    if not key or not candidates:
        return None

    target = target or {}
    criteria = {c["id"]: c["criteria"] for c in candidates}

    body = {
        "model": os.environ.get("TYPESAFE_MODEL", TYPESAFE_MODEL),
        "state": {
            "target": target,
            "goal": "Select the highest-converting, safest cold message candidate to send in Telegram"
                    " to a local business without a website",
        },
        "questions": {
            "best_offer": {
                "type": "choice",
                "criteria": criteria,
                "instructions": "Choose the candidate with the highest response rate and lowest spam risk.",
            }
        },
    }

    for attempt in range(3):
        try:
            req = urllib.request.Request(
                TYPESAFE_URL,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                    "User-Agent": "FindClient-CRM/2.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                ans = data.get("answers", {}).get("best_offer", {})
                choice = ans.get("choice")
                probs = ans.get("probabilities", {})
                confidence = float(ans.get("confidence", 0.5))
                return {
                    "choice": choice,
                    "probabilities": probs,
                    "confidence": confidence,
                    "model": data.get("model", "jev"),
                }
        except Exception as exc:
            log.debug("TypeSafe Jev candidate selection attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    return None


def generate_offer(
    target: dict[str, Any] | None = None,
    strategy_id: str = "lost_traffic",
    link: str = "",
    with_link: bool = False,
    prompt_hint: str = "",
    api_key: str = "",
    bai_key: str = "",
    bai_model: str = "",
    bai_url: str = "",
) -> dict[str, Any]:
    """Генерирует готовый персональный оффер для цели с отбором через Jev и B.AI."""
    target = target or {}

    # Если цель не выбрана, подставляем реалистичные параметры
    if not target or not target.get("name"):
        target = {
            "name": target.get("name") or "Клиника Дента",
            "category": target.get("category") or "стоматология",
            "city": target.get("city") or "Москва",
            "rating": target.get("rating") or 4.8,
            "reviews": target.get("reviews") or 42,
        }

    # Синтезируем 4 кандидата (с участием B.AI Qwen 3.8 Flash, если задан ключ)
    candidates = synthesize_candidates(
        target=target,
        link=link,
        with_link=with_link,
        prompt_hint=prompt_hint,
        bai_key=bai_key,
        bai_model=bai_model,
        bai_url=bai_url,
    )

    # Запрашиваем у Jev выбор лучшего кандидата
    jev_decision = select_best_with_jev(candidates, target=target, api_key=api_key)

    winner_id = strategy_id
    if jev_decision and jev_decision.get("choice"):
        # Если стратегия не была явно переопределена пользователем, берем выбор Jev
        if strategy_id == "lost_traffic" or strategy_id not in [c["id"] for c in candidates]:
            winner_id = jev_decision["choice"]

    # Находим победителя
    winner_cand = next((c for c in candidates if c["id"] == winner_id), candidates[0])

    # Проставляем вероятности Jev по каждому кандидату
    probs = (jev_decision or {}).get("probabilities", {})
    candidate_cards = []
    for c in candidates:
        p = probs.get(c["id"], 0.0)
        candidate_cards.append({
            "id": c["id"],
            "title": c["title"],
            "text": c["text"],
            "probability": round(p * 100, 1) if p else (85.0 if c["id"] == winner_id else 5.0),
            "is_winner": c["id"] == winner_id,
        })

    # Сортируем кандидатов: победитель первый
    candidate_cards.sort(key=lambda x: x["probability"], reverse=True)

    # Классифицируем выбранный оффер
    classification = classify_offer(winner_cand["text"], target=target, with_link=with_link, api_key=api_key)

    return {
        "strategy": winner_id,
        "strategy_name": winner_cand["title"],
        "text": winner_cand["text"],
        "candidates": candidate_cards,
        "jev_selection": jev_decision,
        "classification": classification,
    }


def _evaluate_hook_and_personalization(text: str, target: dict[str, Any]) -> tuple[int, list[str]]:
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


def _evaluate_value_and_offer(text: str, target: dict[str, Any]) -> tuple[int, list[str]]:
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


def _evaluate_cta_and_friction(text: str) -> tuple[int, list[str]]:
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


def _evaluate_antispam_and_risk(raw_text: str, with_link: bool = False) -> tuple[int, list[str]]:
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


def _call_typesafe_jev(
    text: str, target: dict[str, Any] | None = None, api_key: str = ""
) -> dict[str, Any] | None:
    """Вызов TypeSafe Jev API для классификации оффера через систему решений Jev."""
    key = api_key or os.environ.get("TYPESAFE_API_KEY", "") or TYPESAFE_API_KEY
    if not key:
        return None

    body = {
        "model": os.environ.get("TYPESAFE_MODEL", TYPESAFE_MODEL),
        "state": {
            "offer_text": text,
            "target": target or {},
            "goal": "Classify whether this cold outreach offer is effective, safe, and high-converting",
        },
        "questions": {
            "verdict": {
                "type": "choice",
                "criteria": {
                    "EXCELLENT": "Strong hook, personal facts/niche, under 50 words,"
                                 " exactly 1 low-friction question, no price, highest response rate",
                    "GOOD": "Clear value, acceptable to send, slight improvements possible",
                    "NEEDS_WORK": "Too long, lacks personalization, high friction, or weak call to action",
                    "SPAM_RISK": "Contains prices in 1st msg, external links, em-dashes,"
                                 " multiple questions, high complaint risk",
                },
                "instructions": "Evaluate the cold outreach message quality and pick the most accurate verdict.",
            }
        },
    }

    # Повторы на обрыв связи (характерно для провайдера)
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                TYPESAFE_URL,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                    "User-Agent": "FindClient-CRM/2.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                ans = data.get("answers", {}).get("verdict", {})
                choice = ans.get("choice", "")
                probs = ans.get("probabilities", {})
                confidence = float(ans.get("confidence", 0.5))

                weighted = (
                    probs.get("EXCELLENT", 0.0) * 96
                    + probs.get("GOOD", 0.0) * 80
                    + probs.get("NEEDS_WORK", 0.0) * 55
                    + probs.get("SPAM_RISK", 0.0) * 20
                )
                ai_score = int(round(weighted)) if probs else (
                    90 if choice == "EXCELLENT" else 75 if choice == "GOOD" else 55
                )

                return {
                    "choice": choice,
                    "score": ai_score,
                    "confidence": confidence,
                    "probabilities": probs,
                    "summary": (
                        f"Jev ({data.get('model', 'decision')}): вердикт {choice}"
                        f" (уверенность {int(confidence * 100)}%)"
                    ),
                }
        except Exception as exc:
            log.debug("TypeSafe Jev attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    return None


def classify_offer(
    text: str,
    target: dict[str, Any] | None = None,
    with_link: bool = False,
    api_key: str = "",
) -> dict[str, Any]:
    """Классифицирует оффер с помощью AI + Jev Judge."""
    clean_text = tpl.cleanup(text)
    target = target or {}

    s1, notes1 = _evaluate_hook_and_personalization(clean_text, target)
    s2, notes2 = _evaluate_value_and_offer(clean_text, target)
    s3, notes3 = _evaluate_cta_and_friction(clean_text)
    s4, notes4 = _evaluate_antispam_and_risk(text, with_link=with_link)

    rules_score = s1 + s2 + s3 + s4
    recommendations = notes1 + notes2 + notes3 + notes4

    # Запрашиваем TypeSafe Jev API
    ai_result = _call_typesafe_jev(clean_text, target, api_key=api_key)

    if ai_result and "score" in ai_result:
        ai_score = int(ai_result.get("score", rules_score))
        total_score = int(0.5 * rules_score + 0.5 * ai_score)
        source = "jev_ai"
        ai_summary = ai_result.get("summary", "")
    else:
        total_score = rules_score
        source = "jev_rules"
        ai_summary = ""

    if total_score >= 85:
        verdict = "excellent"
        verdict_title = "Отличный оффер"
        status_label = "Высокая конверсия, готов к отправке"
    elif total_score >= 70:
        verdict = "good"
        verdict_title = "Хороший оффер"
        status_label = "Годен к отправке, замечания минимальны"
    elif total_score >= 50:
        verdict = "needs_work"
        verdict_title = "Требует доработки"
        status_label = "Есть риски снижения отклика или жалоб"
    else:
        verdict = "poor"
        verdict_title = "Спам-риск / Плохой оффер"
        status_label = "Высокий риск блокировки или игнорирования"

    words_count = len(re.findall(r"[А-Яа-яA-Za-z0-9]+", clean_text))

    return {
        "score": total_score,
        "verdict": verdict,
        "verdict_title": verdict_title,
        "status_label": status_label,
        "source": source,
        "ai_summary": ai_summary,
        "words": words_count,
        "dimensions": {
            "hook": {"score": s1, "max": 25, "name": "Крючок и персонализация"},
            "value": {"score": s2, "max": 25, "name": "Ценность и оффер"},
            "cta": {"score": s3, "max": 25, "name": "Призыв к действию (CTA)"},
            "antispam": {"score": s4, "max": 25, "name": "Антиспам и безопасность"},
        },
        "recommendations": recommendations,
        "checks": {
            "under_word_limit": words_count <= tpl.WORD_LIMIT,
            "no_em_dash": "\u2014" not in text and "\u2013" not in text,
            "single_question": clean_text.count("?") == 1,
            "no_price": not bool(
                re.search(
                    r"\d[\d\s]{2,}\s*(руб|₽|р\.)|\bпрайс|\bстоимост|\bскидк|\bцен[аыуе]\b|\bоплат",
                    clean_text, re.I,
                )
            ),
            "no_broken_vars": "{" not in clean_text and "}" not in clean_text,
        },
    }


def auto_improve_offer(
    text: str,
    target: dict[str, Any] | None = None,
    api_key: str = "",
    bai_key: str = "",
    bai_model: str = "",
    bai_url: str = "",
) -> str:
    """Улучшает слабый оффер, применяя скилл Humanizer модели B.AI Qwen 3.8 Flash и Jev-отбор."""
    target = target or {}

    # 1. Пробуем переписать текст через B.AI Humanizer (Qwen 3.8 Flash)
    bkey = bai_key or os.environ.get("BAI_API_KEY", "") or BAI_API_KEY
    bmodel = bai_model or os.environ.get("BAI_MODEL", "") or BAI_MODEL or "qwen3.8-flash"
    burl = bai_url or os.environ.get("BAI_BASE_URL", "") or BAI_BASE_URL
    if bkey:
        try:
            bai_humanized = bai.humanize_with_bai(
                text=text,
                target=target,
                api_key=bkey,
                model=bmodel,
                base_url=burl,
            )
            if bai_humanized:
                return bai_humanized
        except Exception as exc:
            log.warning("B.AI humanize fallback: %s", exc)

    # 2. Если B.AI недоступен, выполняем детерминированную очистку
    fixed = text.replace("\u2014", "-").replace("\u2013", "-")
    # Спейс-дефис - это то же длинное тире, только замаскированное: в живых
    # сообщениях его нет, сворачиваем в запятую.
    fixed = re.sub(r"\s+-\s+", ", ", fixed)

    # Убираем цены и прайс-триггеры
    fixed = re.sub(r"\d[\d\s]{2,}\s*(руб|₽|р\.)", "демо-версию", fixed, flags=re.I)
    fixed = re.sub(r"\b(по прайсу|со скидкой|стоимость|цена)\b", "детали", fixed, flags=re.I)

    # Если больше одного вопроса - сокращаем до одного четкого вопроса
    if fixed.count("?") > 1:
        parts = fixed.split("?")
        main_part = parts[0]
        last_question = parts[-2].strip()
        fixed = f"{main_part.strip()}. {last_question}?"

    fixed = tpl.cleanup(fixed)

    # Синтезируем альтернативные качественные рерайты
    cat = (target.get("category") or "вашей сферы").strip()
    facts = tpl.facts_phrase(target) or "отличная репутация на картах"

    alt_rewrites = [
        {
            "id": "tightened",
            "title": "Сжатый и прямой",
            "text": fixed,
            "criteria": "Cleaned up original with fixed punctuation and no spam words",
        },
        {
            "id": "search_focus",
            "title": "Фокус на поиске",
            "text": (
                f"Здравствуйте! На картах у вас {facts}, но нет сайта. Клиенты из поиска уходят"
                " к конкурентам. Соберу такой сайт за три дня. Есть смысл показать концепт?"
            ),
            "criteria": "High response rate, points out lost search clients, 3-day turnaround, low friction",
        },
        {
            "id": "demo_focus",
            "title": "Фокус на готовом примере",
            "text": (
                f"Добрый день! Заметил вашу компанию на картах. Делаю конверсионные сайты"
                f" для сферы {cat}. Могу прислать готовый пример под ваши услуги за пару минут. Взглянете?"
            ),
            "criteria": "Respectful, value-first, offers a quick look with zero commitment",
        }
    ]

    # Спрашиваем Jev, какой из рерайтов сильнее
    jev_choice = select_best_with_jev(alt_rewrites, target=target, api_key=api_key)
    if jev_choice and jev_choice.get("choice"):
        winner = next((r for r in alt_rewrites if r["id"] == jev_choice["choice"]), alt_rewrites[0])
        return winner["text"]

    return fixed
