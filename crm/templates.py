"""Шаблоны сообщений: подстановка переменных и проверка на спам-признаки.

Правила взяты из черновиков `/root/coding/outreach/drafts.md` - они там
сформулированы как «скилл cold-message-writer» и проверены на живой рассылке:

* первая строка про них, а не про нас;
* до 50 слов, одна просьба на сообщение;
* без прайса и без двух вопросов сразу;
* длинное тире не используем.

Сюда добавлено то, чего в черновиках не было, но что прямо влияет на
вероятность бана: **ссылка не в первом сообщении**. Ссылка в холодном
первом сообщении - один из самых сильных сигналов для антиспама, и живые
люди в первых сообщениях ссылки почти не шлют.

Отдельная головная боль - согласование слов. «91 отзывов» и «в
Санкт-Петербург» выдают робота мгновенно, поэтому:

* число отзывов идёт через `{facts}` - уже согласованное («91 отзыв»,
  «2 отзыва», «5 отзывов»);
* в шаблонах по умолчанию города и категории стоят в именительном падеже
  или не стоят вовсе: падежи для произвольных названий не выводятся
  надёжно, а «в Санкт-Петербург» хуже, чем отсутствие города.
"""

from __future__ import annotations

import random
import re

# Простые переменные - подставляются как есть.
SIMPLE_PLACEHOLDERS = (
    "name", "reviews", "rating", "address", "city", "category", "phone", "link",
)
# Вычисляемые - уже в правильной форме.
COMPUTED_PLACEHOLDERS = ("facts",)
PLACEHOLDERS = SIMPLE_PLACEHOLDERS + COMPUTED_PLACEHOLDERS

WORD_LIMIT = 50

# Ниже этого рейтинга о нём лучше не напоминать. «1,0 и 2 отзыва» в первом
# сообщении работает против нас: человек видит, что мы ткнули его в слабое
# место. Такому адресату пишем про отзывы, но без оценки.
RATING_MIN = 4.0

DEFAULT_TEMPLATES = [
    {
        "name": "Карточка без сайта",
        "category": "",
        "body": (
            "Здравствуйте! Нашёл вас на картах: {facts}. Сайта нет, а те, кто "
            "ищет вас в поиске, до карточки не доходят. Собираю такие сайты за "
            "три дня. Есть смысл говорить дальше?"
        ),
    },
    {
        "name": "Соцсети вместо сайта",
        "category": "",
        "body": (
            "Здравствуйте! На картах у вас {facts}, а вместо сайта только "
            "страница в соцсетях, которую вы не контролируете. Те, кто ищет вас "
            "в поиске, до неё не доходят. Собираю такие сайты за три дня. "
            "Посмотреть, как это будет на ваших услугах?"
        ),
    },
    {
        "name": "Второе сообщение со ссылкой",
        "category": "",
        "body": (
            "Спасибо, что ответили. Вот пример, открывается за минуту: {link} "
            "Там направления с ценами, отзывы, карта и форма записи. Скажите, "
            "что добавить под вас?"
        ),
    },
]


class TemplateError(ValueError):
    """Шаблон не проходит проверку."""


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское согласование числительного с существительным."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def reviews_phrase(reviews) -> str:
    try:
        n = int(reviews)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return f"{n} {plural(n, 'отзыв', 'отзыва', 'отзывов')}"


def rating_phrase(rating) -> str:
    try:
        value = float(rating)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return f"{value:.1f}".replace(".", ",")


def facts_phrase(target: dict) -> str:
    """«5,0 и 91 отзыв» / «5,0» / «91 отзыв» / пусто.

    Рейтинг ниже `RATING_MIN` не показываем: хвалить «1,0» нельзя, а ткнуть
    человека в слабое место в первом сообщении - верный отказ и жалоба.
    """
    rating = rating_phrase(target.get("rating"))
    try:
        if float(target.get("rating") or 0) < RATING_MIN:
            rating = ""
    except (TypeError, ValueError):
        rating = ""
    reviews = reviews_phrase(target.get("reviews"))
    if rating and reviews:
        return f"{rating} и {reviews}"
    return rating or reviews


def render(body: str, target: dict, link: str = "", with_link: bool = True) -> str:
    """Подставляет переменные цели в шаблон."""
    values = {
        "name": (target.get("name") or "").strip(),
        "reviews": str(target.get("reviews") or 0),
        "rating": rating_phrase(target.get("rating")),
        "address": (target.get("address") or "").strip(),
        "city": (target.get("city") or "").strip(),
        "category": (target.get("category") or "").strip().lower(),
        "phone": (target.get("phone") or "").strip(),
        "link": link.strip(),
        "facts": facts_phrase(target),
    }

    text = body
    if not with_link:
        # Выкидываем предложение со ссылкой целиком.
        text = re.sub(r"[^.?!]*\{link\}[^.?!]*[.?!]?", "", text)

    for key in PLACEHOLDERS:
        if key == "link" and not values["link"]:
            continue
        text = text.replace("{" + key + "}", values[key])

    return cleanup(text)


def cleanup(text: str) -> str:
    """Убирает следы подстановки.

    Пустая переменная не должна оставлять «Нашёл вас на картах: . Сайта нет»
    или двойной пробел - такое читается как сломанный шаблон и работает
    против нас ровно так же, как и пустое место.
    """
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    text = re.sub(r"[ \t]{2,}", " ", text)
    # «: .» и «: ,» - переменная оказалась пустой
    text = re.sub(r"[:\-]\s*(?=[.,;!?])", "", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    # «и .» / «и ,» - союз остался без второго члена
    text = re.sub(r"\s+и\s*(?=[.,;!?])", "", text)
    text = re.sub(r"^\s*[,.!?:]\s*", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def check(body: str, with_link: bool = True) -> list[str]:
    """Замечания к шаблону. Пустой список = годен."""
    problems: list[str] = []

    sample = {
        "name": "Тест", "reviews": 91, "rating": 5.0, "address": "ул. Тестовая, 1",
        "city": "Санкт-Петербург", "category": "юридические услуги",
        "phone": "+78120000000",
    }
    text = render(body, sample, link="https://example.com", with_link=with_link)

    words = len(re.findall(r"[А-Яа-яA-Za-z0-9]+", text))
    if words > WORD_LIMIT:
        problems.append(f"{words} слов, лимит {WORD_LIMIT}")
    if "\u2014" in body or "\u2013" in body:
        problems.append("длинное тире")
    if text.count("?") > 1:
        problems.append("больше одного вопроса")
    if "{link}" in body and with_link and "https://" not in text:
        problems.append("ссылка не подставилась")
    if not with_link and "{" in text:
        problems.append("осталась неподставленная переменная")

    unknown = set(re.findall(r"\{(\w+)\}", body)) - set(PLACEHOLDERS)
    if unknown:
        problems.append("неизвестные переменные: " + ", ".join(sorted(unknown)))

    # Прайс в первом сообщении - прямой путь к жалобе. Ищем именно цену:
    # «направления с ценами» - это описание сайта, а не прайс, и ловить его
    # нельзя, иначе проверка начнёт врать и её перестанут читать.
    if re.search(
        r"\d[\d\s]{2,}\s*(руб|₽|р\.)|"
        r"\bпрайс|\bстоимост|\bскидк|\bцен[аыуе]\b|\bоплат",
        body, re.I,
    ):
        problems.append("похоже на прайс: в первом сообщении цену не называем")

    # Падежи: «в {city}», «для {category}» и подобное дадут «в Санкт-Петербург».
    for prep in ("в", "во", "на", "из", "для", "по", "о", "об"):
        for var in ("city", "category"):
            if re.search(rf"\b{prep}\s+\{{{var}\}}", body, re.I):
                problems.append(
                    f"падеж: «{prep} {{{var}}}» - подставляется именительный, "
                    f"получится «{prep} Санкт-Петербург». Переформулируйте"
                )

    # Пустая цель не должна давать мусор.
    empty = render(body, {"name": "", "reviews": 0, "rating": 0}, link="", with_link=False)
    if re.search(r"\s{2,}|[:\-]\s*[.,]|\bи\s*[.,]", empty):
        problems.append(f"при пустых данных получается мусор: {empty[:60]!r}")

    return problems


def pick_variant(body: str, seed: str | None = None) -> str:
    """Слегка варьирует приветствие.

    Одинаковые тела сообщений - самый явный признак рассылки. Меняем ровно
    одно слово, но этого достаточно, чтобы два сообщения не были байт-в-байт
    одинаковыми.
    """
    greetings = ("Здравствуйте!", "Добрый день!", "Здравствуйте.", "Добрый день.")
    rnd = random.Random(seed) if seed is not None else random
    return re.sub(
        r"^(Здравствуйте|Добрый день)[!.]", rnd.choice(greetings), body, count=1
    )
