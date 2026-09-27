"""Поиск ЛПР в ЕГРЮЛ/ЕГРИП через egrul.nalog.ru.

ЕГРЮЛ - единственный бесплатный источник, где ФИО руководителя компании
лежит открыто. Поток повторяет сайт: POST с запросом -> токен -> опрос
``search-result/{токен}`` до готовности. Для ИП (k='fl') руководитель -
сам предприниматель, его ФИО стоит в поле ``n``.

Матчим торговое имя из карточки карты с юридическим наименованием по
словам и региону: «Мадин, стоматология» + Казань -> ООО «МАДИН»
(Татарстан), а не ЗАО «МАДИН» из Саратова.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

_EGRUL = "https://egrul.nalog.ru/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
_POLL_TRIES = 8
_POLL_PAUSE = 1.0

# Юридические формы и канцелярит в наименованиях: для сверки слов бесполезны.
_STOPWORDS = frozenset((
    "ооо", "оао", "зао", "пао", "ао", "ип", "нко", "фгуп", "гуп", "муп", "общество",
    "ограниченной", "ответственностью", "закрытое", "открытое", "акционерное",
    "компания", "фирма", "групп", "группа", "регистрация", "индивидуальный",
    "предприниматель", "крестьянское", "хозяйство",
))

# Город -> подстрока региона из ЕГРЮЛ (rn). Для сверки кандидатов.
_REGIONS = {
    "москва": "москва",
    "moscow": "москва",
    "msk": "москва",
    "санкт-петербург": "санкт-петербург",
    "санкт": "санкт-петербург",
    "spb": "санкт-петербург",
    "новосибирск": "новосибирск",
    "екатеринбург": "свердловск",
    "казань": "татарстан",
    "нижний новгород": "нижегородск",
    "краснодар": "краснодарск",
    "самара": "самарск",
    "ростов-на-дону": "ростовск",
    "уфа": "башкортостан",
    "челябинск": "челябинск",
    "воронеж": "воронежск",
    "пермь": "пермск",
    "сочи": "краснодарск",
    "волгоград": "волгоградск",
    "красноярск": "красноярск",
    "тюмень": "тюменск",
    "иркутск": "иркутск",
}


def _tokens(name: str) -> set[str]:
    """Осмысленные слова наименования: без юрформ, пунктуации и кавычек."""
    words = re.split(r"[^0-9а-яёa-z]+", (name or "").lower().replace("ё", "е"))
    return {w for w in words if len(w) > 1 and w not in _STOPWORDS}


def _region_of(city: str) -> str:
    return _REGIONS.get((city or "").strip().lower().replace("ё", "е"), "")


def _match_score(want: set[str], row: dict[str, Any], region: str) -> float:
    have = _tokens(row.get("c") or row.get("n") or "")
    if not have or not (want & have):
        return 0.0
    score = len(want & have) / len(have)
    if region and region in (row.get("rn") or "").lower().replace("ё", "е"):
        score += 0.5
    return score


class EgrulClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Результаты поиска ЕГРЮЛ по строке. Пусто = не нашлось или сервис молчит."""
        try:
            async with self.session.post(
                _EGRUL, data={"query": query, "page": "0"},
                headers={"User-Agent": _UA},
            ) as r:
                data = await r.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            log.debug("ЕГРЮЛ: запрос «%s» не прошёл: %s", query, exc)
            return []
        token = (data or {}).get("t")
        if not token or data.get("captchaRequired"):
            log.debug("ЕГРЮЛ: капча или пустой токен на «%s»", query)
            return []

        for _ in range(_POLL_TRIES):
            await asyncio.sleep(_POLL_PAUSE)
            try:
                async with self.session.get(
                    f"{_EGRUL}search-result/{token}", headers={"User-Agent": _UA},
                ) as r:
                    res = await r.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
                log.debug("ЕГРЮЛ: чтение результата «%s»: %s", query, exc)
                return []
            if res and res.get("status") == "wait":
                continue
            return (res or {}).get("rows") or []
        return []

    async def find_director(self, name: str, city: str = "") -> dict[str, Any] | None:
        """Торговое имя + город -> ЛПР.

        Возвращает ``{"director", "position", "inn", "company"}`` или None,
        если уверенного совпадения нет. Ошибочное совпадение хуже пустого:
        писать «не тому» Иванову смысла нет.
        """
        query = f"{name} {city}".strip()
        rows = await self.search(query)
        if not rows:
            return None

        want = _tokens(name)
        region = _region_of(city)
        best: dict[str, Any] | None = None
        best_score = 0.0
        for row in rows:
            score = _match_score(want, row, region)
            if score > best_score:
                best, best_score = row, score
        if not best or best_score < 0.5:
            return None

        if best.get("k") == "fl":
            # ИП: решение принимает сам предприниматель.
            fio = (best.get("n") or "").strip()
            if not fio:
                return None
            return {
                "director": fio.title(),
                "position": "ИП",
                "inn": best.get("i", ""),
                "company": fio.title(),
            }

        g = (best.get("g") or "").strip()
        if ":" in g:
            position, _, fio = g.partition(":")
        else:
            position, fio = "руководитель", g
        fio = fio.strip()
        if not fio:
            return None
        return {
            "director": fio,
            "position": position.strip().lower() or "руководитель",
            "inn": best.get("i", ""),
            "company": (best.get("c") or best.get("n") or "").strip(),
        }
