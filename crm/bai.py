"""Модуль интеграции с B.AI API (Qwen 3.8 Flash / DeepSeek) и маркетинг-скиллами.

Включает специализированные навыки (skills):
1. Russian B2B Outreach Specialist:
   - Полная адаптация под менталитет российского малого бизнеса в Telegram/WhatsApp.
   - Запрет на американские клише: созвоны в Zoom, навязчивый календарный букинг, фальшивая лесть.
   - Фокус на потере клиентов из поиска Яндекса, фактах из 2GIS и легком бесконтактном CTA.
2. Humanizer & Anti-AI Slop:
   - Запрет на длинные тире (\\u2014) и средние тире (\\u2013).
   - Запрет на вводные AI-штампы (не секрет что, в современном мире, давайте будем честны).
   - Живой, лаконичный язык реального специалиста (30-45 слов, до 3 предложений).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from dotenv import load_dotenv

from crm.http import post_json

load_dotenv()
load_dotenv("crm.env")

log = logging.getLogger("crm.bai")

BAI_DEFAULT_URL = os.environ.get("BAI_BASE_URL", "https://api.b.ai/v1")
BAI_DEFAULT_KEY = os.environ.get("BAI_API_KEY", "")
BAI_DEFAULT_MODEL = os.environ.get("BAI_MODEL", "qwen3.8-flash")

SUPPORTED_MODELS = [
    {
        "id": "qwen3.8-flash",
        "name": "Qwen 3.8 Flash (Alibaba)",
        "tag": "Рекомендуется для РФ",
        "description": "Лучшее владение русским языком, естественный тон без англицизмов.",
    },
    {
        "id": "deepseek-v4.1-flash",
        "name": "DeepSeek V4.1 Flash",
        "tag": "Сильная логика",
        "description": "Точное следование жестким инструкциям и структуре.",
    },
    {
        "id": "glm-5.3-flash",
        "name": "GLM 5.3 Flash",
        "tag": "Быстрая модель",
        "description": "Компактная Flash-модель Zhipu AI.",
    },
    {
        "id": "mimo-v2.6-flash",
        "name": "Mimo V2.6 Flash",
        "tag": "Альтернатива",
        "description": "Flash-модель Xiaomi MiMo.",
    },
]

# Системный промпт со встроенными навыками Humanizer и Russian Outreach
SYSTEM_OUTREACH_PROMPT = """Ты - топовый русскоязычный B2B-копирайтер и эксперт по прямому cold outreach
в Telegram и WhatsApp для малого бизнеса в России.

ТВОИ ВСТРОЕННЫЕ НАВЫКИ (SKILLS):

1. НАВЫК: АДАПТАЦИЯ ПОД РФ РЫНОК (NO AMERICANISMS):
- Категорически запрещены предложения созвона: никаких «созвонимся на 15 минут», «удобно в Zoom»,
«выделите время во вторник». В РФ с незнакомцами не созваниваются!
- Категорически запрещена ложная американская вежливость: «Надеюсь, эта неделя продуктивна»,
«Надеюсь, у вас все хорошо», «Давайте будем честны».
- Запрещен корпоративный пафос и инфоцыганские слова: «лидогенерация», «масштабирование»,
«увеличение выручки в 3 раза», «инновационный», «уникальный», «революционный».
- Общение уважительное, деловое, строго на «Вы», но без чинопочитания и без панибратства.

2. НАВЫК: HUMANIZER & ANTI-SLOP (ЖИВОЙ ЧЕЛОВЕК):
- СТРОЖАЙШИЙ ЗАПРЕТ на любые длинные тире (\u2014, \u2013). Запрещены абсолютно!
Заменяй на точку, запятую или дефис (-).
- Запрещены вводные клише AI: «Не секрет, что», «В современном мире»,
«Ключевым преимуществом является», «Стоит отметить».
- Запрещены маркированные списки и жирные заголовки в тексте сообщения.
- Текст должен читаться легко и бегло, как сообщение от реального мастера своего дела.
- Объем: строго 30-45 слов. До 3-4 коротких предложений.

3. АРХИТЕКТУРА СООБЩЕНИЯ В TELEGRAM:
- Предложение 1 (Факт с карт): Приветствие + конкретный факт (город, сфера, рейтинг и отзывы на картах). Без лести.
- Предложение 2 (Потеря денег / оффер): У них нет сайта, клиенты из поиска Яндекса уходят к конкурентам с сайтами.
Твой оффер: собираю такие сайты за три дня под ключ.
- Предложение 3 (Низкострессовый CTA): Вопрос с нулевым порогом трения (например: «Есть смысл показать готовый
концепт под ваши услуги?» или «Взглянете?»).
"""


def get_supported_models() -> list[dict[str, str]]:
    """Возвращает список поддерживаемых моделей B.AI с описанием."""
    return SUPPORTED_MODELS


def clean_human_output(text: str) -> str:
    """Удаляет AI-маркеры, кавычки и запрещенные символы из ответа модели."""
    if not text:
        return ""
    cleaned = text.strip()
    # Убираем кавычки-обрамления, если модель обернула весь ответ
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith('«') and cleaned.endswith('»')):
        cleaned = cleaned[1:-1].strip()
    # Заменяем длинные и средние тире
    cleaned = cleaned.replace("\u2014", "-").replace("\u2013", "-")
    # Убираем двойные пробелы
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


async def call_bai_chat(
    messages: list[dict[str, str]],
    model: str = "",
    api_key: str = "",
    base_url: str = "",
    temperature: float = 0.7,
    max_tokens: int = 1200,
    attempts: int = 3,
) -> str | None:
    """Запрос в B.AI OpenAI-совместимый API с авто-повторами (без блокировки цикла)."""
    key = api_key or os.environ.get("BAI_API_KEY", "") or BAI_DEFAULT_KEY
    if not key:
        log.warning("B.AI API key is missing")
        return None

    model_name = model or os.environ.get("BAI_MODEL", "") or BAI_DEFAULT_MODEL
    url = (base_url or os.environ.get("BAI_BASE_URL", "") or BAI_DEFAULT_URL).rstrip("/") + "/chat/completions"

    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "FindClient-CRM/2.0 (B.AI Client)",
    }

    data = await post_json(url, payload, headers, attempts=attempts, timeout=50.0)
    if not data:
        return None
    choices = data.get("choices", [])
    if choices:
        content = choices[0].get("message", {}).get("content", "")
        if content:
            return clean_human_output(content)
    return None


async def generate_bai_offer(
    target: dict[str, Any] | None = None,
    strategy_id: str = "lost_traffic",
    prompt_hint: str = "",
    api_key: str = "",
    model: str = "",
    base_url: str = "",
) -> str | None:
    """Генерирует живой оффер через B.AI (Qwen 3.8 Flash) со всеми скиллами."""
    target = target or {}
    name = target.get("name") or "компания"
    category = target.get("category") or "услуги"
    city = target.get("city") or "Москва"
    rating = target.get("rating") or 4.8
    reviews = target.get("reviews") or 40

    user_prompt = (
        f"Компания: {name}\n"
        f"Сфера/ниша: {category}\n"
        f"Город: {city}\n"
        f"Репутация на картах 2GIS: рейтинг {rating}, отзывов: {reviews}\n"
        f"Сайта в карточке нет.\n"
    )

    if strategy_id == "social_only":
        user_prompt += (
            "Упор оффера: на картах указана только группа в соцсетях,"
            " нет нормального сайта под поиск Яндекса.\n"
        )
    elif strategy_id == "ready_concept":
        user_prompt += "Упор оффера: предложение взглянуть на готовый прототип страницы под их услуги за пару минут.\n"
    elif strategy_id == "conversion_quiz":
        user_prompt += "Упор оффера: сайт-квиз с формой расчета цены, захватывающий заявки из поиска.\n"
    else:
        user_prompt += "Упор оффера: потеря клиентов из поиска Яндекса в пользу конкурентов с сайтами.\n"

    if prompt_hint:
        user_prompt += f"Дополнительное пожелание/акцент: {prompt_hint.strip()}\n"

    user_prompt += (
        "Напиши готовый текст первого холодного сообщения в Telegram. "
        "Строго 30-45 слов, без длинных тире, без созвонов, без цен. "
        "Только финальный текст сообщения."
    )

    messages = [
        {"role": "system", "content": SYSTEM_OUTREACH_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    return await call_bai_chat(messages, model=model, api_key=api_key, base_url=base_url)


async def humanize_with_bai(
    text: str,
    target: dict[str, Any] | None = None,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
) -> str | None:
    """Прогоняет произвольный текст через скилл Humanizer модели Qwen 3.8 Flash."""
    target = target or {}
    name = target.get("name") or ""
    category = target.get("category") or ""
    city = target.get("city") or ""

    user_prompt = (
        "Перепиши исходный черновик сообщения по правилам Humanizer и Russian B2B Outreach.\n\n"
        f"Контекст цели: {name} ({category}, {city})\n"
        f"Исходный текст:\n«««\n{text}\n»»»\n\n"
        "Требования к рерайту:\n"
        "1. Устрани любые длинные тире (\\u2014, \\u2013). Замени на точку, запятую или дефис (-).\n"
        "2. Убери цены, скидки, созвоны в Zoom и навязчивые вопросы.\n"
        "3. Убери AI-клише и воду. Сделай текст живым, уверенным и простым (30-45 слов).\n"
        "4. Оставь один легкий вопрос в конце: «Есть смысл показать концепт?» или «Взглянете?».\n"
        "Выведи ТОЛЬКО финальный переписанный текст."
    )

    messages = [
        {"role": "system", "content": SYSTEM_OUTREACH_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    return await call_bai_chat(
        messages, model=model, api_key=api_key, base_url=base_url, temperature=0.5
    )
