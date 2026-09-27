"""Тесты клиента ЕГРЮЛ: разбор ответа, матчинг имени, ИП и юрлица."""

from unittest.mock import AsyncMock

import pytest

from api.egrul import EgrulClient, _match_score, _region_of, _tokens

UL_ROWS = [
    {
        "c": "ООО \"СТОМАТОЛОГИЯ КАЗАНЬ\"", "n": "ОБЩЕСТВО ... \"СТОМАТОЛОГИЯ КАЗАНЬ\"",
        "g": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР: Аскаров Тимур Габдерауфович",
        "i": "1686014140", "k": "ul", "rn": "Республика Татарстан (Татарстан)",
    },
    {
        "c": "ООО \"МАДИН\"", "n": "ОБЩЕСТВО ... \"МАДИН\"",
        "g": "ДИРЕКТОР: Овечкина Мария Вячеславовна",
        "i": "1655263845", "k": "ul", "rn": "Республика Татарстан (Татарстан)",
    },
    {
        "c": "ЗАО \"МАДИН\"", "n": "ЗАКРЫТОЕ АКЦИОНЕРНОЕ ... \"МАДИН\"",
        "g": "ДИРЕКТОР: Шарипов Рустам Рашидович",
        "i": "8602301377", "k": "ul", "rn": "Ханты-Мансийский автономный округ - Югра",
    },
]

IP_ROWS = [{"n": "ОВЕЧКИНА МАРИЯ АЛЕКСАНДРОВНА", "i": "236000980281", "k": "fl", "c": None}]


class TestTokens:
    def test_legal_forms_dropped(self):
        assert _tokens('ООО "МАДИН"') == {"мадин"}
        assert _tokens("Индивидуальный предприниматель Иванов А.Б.") == {"иванов"}

    def test_yo_normalized(self):
        assert _tokens("Ёлки-палки") == {"елки", "палки"}


class TestRegion:
    def test_city_to_region(self):
        assert _region_of("Казань") == "татарстан"
        assert _region_of("москва") == "москва"
        assert _region_of("Уфа") == "башкортостан"
        assert _region_of("Незнакомск") == ""


class TestMatchScore:
    def test_region_beats_same_name(self):
        want = _tokens("Мадин, стоматология")
        tatar = _match_score(want, UL_ROWS[1], "татарстан")
        ugra = _match_score(want, UL_ROWS[2], "татарстан")
        assert tatar > ugra

    def test_no_overlap_is_zero(self):
        assert _match_score({"ромашка"}, UL_ROWS[1], "") == 0.0


@pytest.mark.asyncio
async def test_find_director_ul(monkeypatch):
    client = EgrulClient(session=None)
    monkeypatch.setattr(client, "search", AsyncMock(return_value=UL_ROWS))
    info = await client.find_director("Мадин, стоматология", "Казань")
    assert info is not None
    assert info["director"] == "Овечкина Мария Вячеславовна"
    assert info["position"] == "директор"
    assert info["inn"] == "1655263845"
    assert info["company"] == 'ООО "МАДИН"'


@pytest.mark.asyncio
async def test_find_director_ip(monkeypatch):
    client = EgrulClient(session=None)
    monkeypatch.setattr(client, "search", AsyncMock(return_value=IP_ROWS))
    info = await client.find_director("Овечкина Мария Александровна", "Краснодар")
    assert info is not None
    assert info["position"] == "ИП"
    assert info["director"] == "Овечкина Мария Александровна"


@pytest.mark.asyncio
async def test_find_director_no_confident_match(monkeypatch):
    client = EgrulClient(session=None)
    monkeypatch.setattr(client, "search", AsyncMock(return_value=UL_ROWS))
    # Ноль пересечений слов: пустой результат лучше ошибочного Иванова.
    assert await client.find_director("Ромашка цветочная", "Казань") is None


@pytest.mark.asyncio
async def test_find_director_search_empty(monkeypatch):
    client = EgrulClient(session=None)
    monkeypatch.setattr(client, "search", AsyncMock(return_value=[]))
    assert await client.find_director("Ромашка", "Казань") is None
