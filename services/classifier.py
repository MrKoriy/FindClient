"""Typed decisions for outreach: reply intent, lead qualification, order filtering.

Backends in order: TypeSafe Jev (typed, calibrated, ~0.5 s), DeepSeek JSON, keyword rules.
"""

import logging
import re
from dataclasses import dataclass

from services.llm import LLM, LLMError

log = logging.getLogger(__name__)

INTENTS: dict[str, str] = {
    "interested": "Интересно, хочет подробнее, согласен посмотреть или созвониться",
    "price": "Спрашивает цену, сроки или условия",
    "question": "Задаёт уточняющий вопрос, но интерес не ясен",
    "later": "Сейчас неактуально, просит написать позже",
    "has_site": "Говорит, что сайт уже есть или делает другой подрядчик",
    "not_interested": "Вежливый отказ, не нужно",
    "stop": "Просит больше не писать, раздражён, грозит жалобой или блокировкой",
    "wrong_person": "Не тот человек, не занимается этим, не владелец",
    "other": "Автоответ, непонятное сообщение, приветствие без сути",
}

LABELS_RU = {
    "interested": "🔥 интересно", "price": "💰 спрашивает цену", "question": "❓ вопрос",
    "later": "⏳ позже", "has_site": "🌐 сайт уже есть", "not_interested": "🙅 отказ",
    "stop": "⛔ не писать", "wrong_person": "👤 не тот человек", "other": "💬 прочее",
}
HOT = {"interested", "price", "question"}


@dataclass
class ReplyClass:
    label: str
    hot: float  # probability the lead is worth an immediate personal reply
    confidence: float
    backend: str

    @property
    def label_ru(self) -> str:
        return LABELS_RU.get(self.label, self.label)

    @property
    def is_stop(self) -> bool:
        return self.label == "stop"


# Order matters: "stop" beats everything, "price" beats "interested".
_RULES: list[tuple[str, re.Pattern]] = [
    ("stop", re.compile(r"не\s+пиш|отстань|спам|заблок|жалоб|хватит|отпиш|удалите|не\s+беспоко|пош[её]л", re.I)),
    ("price", re.compile(r"сколько|цен[аеуы]|стоимост|по\s+деньгам|прайс|бюджет|за\s+сколько|срок", re.I)),
    ("has_site", re.compile(r"(уже\s+есть|есть\s+уже|имеется)\s+сайт|сайт\s+(уже\s+)?есть|делаем\s+сами|свой\s+программист", re.I)),
    ("later", re.compile(r"позже|потом|не\s+сейчас|через\s+(месяц|неделю|пару)|осенью|весной|летом|в\s+следующ", re.I)),
    ("not_interested", re.compile(r"не\s+(надо|нужно|интересно|актуально|требуется)|неактуально|нет,?\s+спасибо|откажусь", re.I)),
    ("wrong_person", re.compile(r"не\s+по\s+адресу|ошиблись|не\s+владел|не\s+занимаюсь|я\s+не\s+(директор|руковод)", re.I)),
    ("interested", re.compile(r"интересно|давайте|покажите|подробнее|созвон|звоните|позвоните|актуально|хочу", re.I)),
    ("question", re.compile(r"\?|как\s+(это|вы)|а\s+что|что\s+входит|кто\s+вы|откуда", re.I)),
]


def classify_rules(text: str) -> ReplyClass:
    for label, rx in _RULES:
        if rx.search(text or ""):
            return ReplyClass(label, 0.8 if label in HOT else 0.1, 0.5, "rules")
    return ReplyClass("other", 0.3, 0.3, "rules")


async def classify_reply(llm: LLM | None, text: str, our_message: str = "") -> ReplyClass:
    """Classify a lead's reply to our outreach."""
    if llm and llm.jev_enabled:
        try:
            answers = await llm.jev(
                {"наше_сообщение": our_message[:1500], "ответ_клиента": text[:2000]},
                {
                    "intent": {
                        "type": "choice",
                        "instructions": "Что означает ответ клиента на наше холодное предложение сделать сайт?",
                        "criteria": INTENTS,
                    },
                    "hot": {
                        "type": "noul",
                        "instructions": "Стоит ли продавцу лично ответить клиенту прямо сейчас, потому что есть шанс на сделку?",
                    },
                },
            )
            intent = answers.get("intent") or {}
            label = intent.get("choice") if intent.get("choice") in INTENTS else "other"
            return ReplyClass(label, float((answers.get("hot") or {}).get("noul", 0.0)),
                              float(intent.get("confidence", 0.0)), "jev")
        except Exception as exc:  # network/shape errors must not break reply handling
            log.warning("Jev classify failed: %s", exc)
    if llm and llm.enabled:
        try:
            data = await llm.chat_json(
                "Ты классифицируешь ответы на холодные сообщения веб-разработчика в Telegram. "
                "Метки: " + "; ".join(f"{k} — {v}" for k, v in INTENTS.items()) +
                '. Верни {"label": метка, "hot": вероятность 0..1 что стоит ответить лично сейчас, '
                '"confidence": 0..1}.',
                f"Наше сообщение:\n{our_message[:1500]}\n\nОтвет клиента:\n{text[:2000]}",
                temperature=0,
                max_tokens=200,
            )
            label = data.get("label") if data.get("label") in INTENTS else "other"
            return ReplyClass(label, float(data.get("hot", 0.5)), float(data.get("confidence", 0.5)), "deepseek")
        except (LLMError, ValueError, TypeError, AttributeError) as exc:
            log.warning("DeepSeek classify failed: %s", exc)
    return classify_rules(text)


async def qualify_texts(llm: LLM | None, texts: list[str], question: str) -> list[float | None]:
    """Jev yes-probability per text (None when Jev is not configured) — cheap map-reduce filter."""
    if not (llm and llm.jev_enabled):
        return [None] * len(texts)
    out: list[float | None] = []
    for text in texts:
        try:
            answers = await llm.jev(text[:3000], {"q": {"type": "noul", "instructions": question}})
            out.append(float((answers.get("q") or {}).get("noul", 0.0)))
        except Exception as exc:
            log.warning("Jev qualify failed: %s", exc)
            out.append(None)
    return out


SITE_ORDER_Q = ("Это заказ от клиента, которому нужно создать, переделать или доработать сайт или лендинг "
                "(а не вакансия в штат, не реклама исполнителя и не другая услуга)?")
BUYER_Q = ("Автор сообщения — владелец бизнеса, бригадир, прораб или компания, которые продают свои услуги "
           "или товары и могли бы заказать себе сайт (а не наёмный рабочий, ищущий работу, и не спамер)?")
