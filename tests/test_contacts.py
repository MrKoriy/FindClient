"""Тесты единого разбора contact_groups 2GIS (api/contacts.py)."""

from api.contacts import join_contacts, split_contacts


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
