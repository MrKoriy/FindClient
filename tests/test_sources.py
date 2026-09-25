"""Parsing tests for map sources (2GIS API, Yandex) and shared helpers."""

from api.common import clean_social, clean_url, is_social_url, lead_score, phone_key
from api.twogis_api import parse_item, sign
from api.yandex_client import _web_sig, parse_official_feature, parse_web_item

TWOGIS_ITEM = {
    "id": "70000001055509979_hash",
    "name_ex": {"primary": "Мадин", "extension": "стоматология"},
    "address_name": "улица Коммунаров, 2",
    "org": {"branch_count": 10},
    "reviews": {"general_rating": 5, "general_review_count": 377},
    "rubrics": [{"name": "Частные стоматологии", "kind": "primary"}, {"name": "Косметолог", "kind": "additional"}],
    "contact_groups": [{"contacts": [
        {"type": "phone", "value": "+78432392222"},
        {"type": "website", "url": "http://madinstom.ru", "value": "http://link.2gis.ru/x?http://madinstom.ru"},
        {"type": "website", "url": "http://rutube.ru/channel/1/", "value": "http://link.2gis.ru/y"},
        {"type": "vkontakte", "value": "https://vk.com/madin"},
        {"type": "whatsapp", "url": "https://wa.me/79001112233?text=hello"},
        {"type": "email", "value": "a@madin.ru"},
    ]}],
}


class TestTwoGIS:

    def test_parse_item(self):
        org = parse_item(TWOGIS_ITEM, "Казань", "kazan")
        assert org.id == "70000001055509979"
        assert org.name == "Мадин, стоматология"
        assert org.website == "http://madinstom.ru"
        assert "rutube.ru" in org.socials and "https://wa.me/79001112233" in org.socials
        assert org.email == "a@madin.ru"
        assert org.category == "Частные стоматологии"
        assert (org.reviews, org.branches, org.rating) == (377, 10, 5.0)
        assert org.url == "https://2gis.ru/kazan/firm/70000001055509979"

    def test_parse_item_without_site_scores_higher(self):
        item = {**TWOGIS_ITEM, "contact_groups": [{"contacts": [{"type": "phone", "value": "+7900"}]}]}
        assert parse_item(item).score > parse_item(TWOGIS_ITEM).score
        assert not parse_item(item).has_website

    def test_sign_is_deterministic_and_order_independent(self):
        a = sign("/3.0/items", {"q": "x", "page": 1}, "salt")
        b = sign("/3.0/items", {"page": 1, "q": "x"}, "salt")
        assert a == b and a != sign("/3.0/items", {"q": "y", "page": 1}, "salt")


class TestYandex:

    def test_parse_web_item(self):
        org = parse_web_item({
            "type": "business", "id": "1046681774", "title": "ЦСИ", "address": "Садовая, 19",
            "urls": ["https://impl.ru/?utm_source=yandex_map", "https://vk.com/impl"],
            "phones": [{"value": "+74959275256"}],
            "categories": [{"name": "Стоматология"}],
            "ratingData": {"ratingValue": 4.9, "reviewCount": 801},
            "socialLinks": [{"href": "https://t.me/csi_impl"}],
            "seoname": "tsi",
        }, "Москва")
        assert org.id == "ya1046681774"
        assert org.website == "https://impl.ru/"
        assert "https://vk.com/impl" in org.socials and "https://t.me/csi_impl" in org.socials
        assert org.reviews == 801 and org.source == "yandex"
        assert org.url == "https://yandex.ru/maps/org/tsi/1046681774/"

    def test_non_business_skipped(self):
        assert parse_web_item({"type": "collection", "id": "1"}) is None

    def test_parse_official(self):
        org = parse_official_feature({"properties": {"CompanyMetaData": {
            "id": "42", "name": "Кафе", "address": "Москва", "url": "https://instagram.com/cafe",
            "Phones": [{"formatted": "+7 (495) 000-00-00"}], "Categories": [{"name": "Кафе"}],
        }}})
        assert org.id == "ya42" and org.website == "" and "instagram" in org.socials

    def test_web_signature(self):
        assert _web_sig("a=1") == _web_sig("a=1") != _web_sig("a=2")


class TestCommon:

    def test_social_detection(self):
        assert is_social_url("https://vk.com/a") and is_social_url("t.me/x") and is_social_url("https://m.vk.com/a")
        assert not is_social_url("https://romashka.ru") and not is_social_url("")

    def test_clean_url_and_social(self):
        assert clean_url("https://a.ru/?yclid=1") == "https://a.ru/"
        assert clean_url("https://a.ru/p?id=3&utm_source=x") == "https://a.ru/p?id=3"
        assert clean_social("https://wa.me/7900?text=hi") == "https://wa.me/7900"

    def test_phone_key(self):
        assert phone_key("+7 (495) 111-22-33, +7 900") == phone_key("84951112233") == "4951112233"
        assert phone_key("123") == ""

    def test_lead_score_bounds(self):
        best = lead_score(has_website=False, phone="1", reviews=10000, branches=10, rating=5)
        worst = lead_score(has_website=True, phone="", reviews=0, branches=0, rating=0)
        assert best == 100 and worst == 0
