"""Тесты разбора контактов: contact_groups 2GIS, мобильные номера, мессенджеры."""

from api.common import is_mobile_phone, messenger_link, mobile_numbers
from api.contacts import join_contacts, split_contacts


class TestMobilePhones:

    def test_mobile_detected(self):
        assert is_mobile_phone("+7 900 111-22-33")
        assert is_mobile_phone("+79001112233")
        assert is_mobile_phone("89001112233")

    def test_landline_and_tollfree_rejected(self):
        assert not is_mobile_phone("+7 (495) 111-22-33")
        assert not is_mobile_phone("+74991234567")
        assert not is_mobile_phone("+74961234567")
        assert not is_mobile_phone("88005553535")
        assert not is_mobile_phone("+7 843 239-22-22")  # городской Казань

    def test_mobile_among_landlines(self):
        assert is_mobile_phone("+7 495 111-22-33, +7 900 111-22-33")

    def test_mobile_numbers_keeps_only_mobiles(self):
        phone = "+7 (495) 111-22-33, +7 900 111-22-33, 88005553535"
        assert mobile_numbers(phone) == "+7 900 111-22-33"


class TestMessengerLink:

    def test_tg_and_whatsapp(self):
        assert messenger_link("https://vk.com/x, https://t.me/owner") == "https://t.me/owner"
        assert messenger_link("https://wa.me/79001112233") == "https://wa.me/79001112233"
        assert messenger_link("https://t.me/owner, https://vk.com/x") == "https://t.me/owner"

    def test_no_messenger(self):
        assert messenger_link("https://vk.com/x, https://ok.ru/y") == ""
        assert messenger_link("") == ""

    def test_scheme_optional(self):
        assert messenger_link("t.me/owner") == "https://t.me/owner"


def test_link_redirect_in_value_unwrapped():
    groups = [{"contacts": [{"type": "website", "value": "http://link.2gis.ru/x?http://foo.ru"}]}]
    assert split_contacts(groups)["website"] == ["http://foo.ru"]


def test_url_preferred_over_value():
    groups = [{"contacts": [{"type": "website", "url": "http://bar.ru", "value": "http://link.2gis.ru/y"}]}]
    assert split_contacts(groups)["website"] == ["http://bar.ru"]


def test_alias_used_when_no_url():
    groups = [{"contacts": [{"type": "website", "alias": "example.com", "value": "https://example.com"}]}]
    assert split_contacts(groups)["website"] == ["example.com"]


def test_duplicate_values_dropped():
    groups = [{"contacts": [
        {"type": "phone", "value": "+7"},
        {"type": "phone", "value": "+7"},
    ]}]
    assert split_contacts(groups)["phone"] == ["+7"]


def test_social_website_goes_to_socials():
    groups = [{"contacts": [{"type": "website", "value": "https://vk.com/club1"}]}]
    c = split_contacts(groups)
    assert c["socials"] == ["https://vk.com/club1"] and c["website"] == []


def test_messenger_type_from_value():
    groups = [{"contacts": [{"type": "telegram", "value": "https://t.me/foo"}]}]
    assert split_contacts(groups)["socials"] == ["https://t.me/foo"]


def test_join_contacts_glues_with_commas():
    groups = [{"contacts": [
        {"type": "phone", "value": "+7"},
        {"type": "phone", "value": "+8"},
    ]}]
    assert join_contacts(groups) == {"phone": "+7, +8", "email": "", "website": "", "socials": ""}


def test_ok_and_odnoklassniki_both_recognized():
    for ctype in ("ok", "odnoklassniki"):
        groups = [{"contacts": [{"type": ctype, "value": "https://ok.ru/group"}]}]
        assert split_contacts(groups)["socials"], ctype
