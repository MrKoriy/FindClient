"""Unit tests for TwoGISClient parsing and pagination logic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.twogis_client import TwoGISClient
from models.organization import Organization


# --- Fixtures ---


def _make_item(
    item_id="123",
    name="Test Org",
    address_name="ул. Тверская, 1",
    rating=4.5,
    contact_groups=None,
):
    """Build a fake 2GIS API item dict."""
    item = {
        "id": item_id,
        "name": name,
        "address_name": address_name,
        "reviews": {"general_rating": rating},
    }
    if contact_groups is not None:
        item["contact_groups"] = contact_groups
    return item


FULL_CONTACT_GROUPS = [
    {
        "contacts": [
            {"type": "phone", "value": "+7 (495) 111-11-11"},
            {"type": "phone", "value": "+7 (495) 222-22-22"},
            {"type": "email", "value": "info@example.com"},
            {"type": "website", "alias": "example.com", "value": "https://example.com"},
            {"type": "vk", "url": "https://vk.com/example"},
            {"type": "instagram", "url": "https://instagram.com/example"},
        ]
    }
]


def _make_client(**overrides):
    """Create a TwoGISClient with mock session."""
    session = AsyncMock()
    defaults = {
        "session": session,
        "api_key": "test-key",
        "base_url": "https://catalog.api.2gis.com/3.0/items",
        "page_size": 50,
        "request_delay": 0.0,
    }
    defaults.update(overrides)
    return TwoGISClient(**defaults), session


# --- _parse_item tests ---


class TestParseItem:
    """Tests for _parse_item field extraction."""

    def test_full_contact_groups(self):
        """All contact fields present -- phone, email, website, socials."""
        client, _ = _make_client()
        item = _make_item(contact_groups=FULL_CONTACT_GROUPS)
        org = client._parse_item(item)

        assert org.id == "123"
        assert org.name == "Test Org"
        assert org.address == "ул. Тверская, 1"
        assert org.rating == 4.5
        assert "+7 (495) 111-11-11" in org.phone
        assert "+7 (495) 222-22-22" in org.phone
        assert org.email == "info@example.com"
        assert "example.com" in org.website
        assert "vk.com/example" in org.socials
        assert "instagram.com/example" in org.socials

    def test_missing_contact_groups_key(self):
        """Item has no contact_groups key at all -- should not crash."""
        client, _ = _make_client()
        item = _make_item()  # no contact_groups kwarg -> key absent
        org = client._parse_item(item)

        assert org.id == "123"
        assert org.name == "Test Org"
        assert org.phone == ""
        assert org.email == ""
        assert org.website == ""
        assert org.socials == ""

    def test_empty_contact_groups_list(self):
        """Item has contact_groups but the list is empty."""
        client, _ = _make_client()
        item = _make_item(contact_groups=[])
        org = client._parse_item(item)

        assert org.phone == ""
        assert org.email == ""
        assert org.website == ""
        assert org.socials == ""

    def test_partial_data_only_phone(self):
        """Only phone present, no email/website/socials."""
        client, _ = _make_client()
        groups = [{"contacts": [{"type": "phone", "value": "+7 999 000 00 00"}]}]
        item = _make_item(contact_groups=groups)
        org = client._parse_item(item)

        assert org.phone == "+7 999 000 00 00"
        assert org.email == ""
        assert org.website == ""
        assert org.socials == ""

    def test_address_fallback_to_full_address(self):
        """Uses full_address_name when address_name is missing."""
        client, _ = _make_client()
        item = {
            "id": "456",
            "name": "Fallback Addr Org",
            "full_address_name": "Москва, ул. Арбат, 10",
            "reviews": {"general_rating": 3.0},
        }
        org = client._parse_item(item)
        assert org.address == "Москва, ул. Арбат, 10"

    def test_no_reviews_key(self):
        """Item has no reviews key -- rating defaults to 0.0."""
        client, _ = _make_client()
        item = {"id": "789", "name": "No Rating Org"}
        org = client._parse_item(item)
        assert org.rating == 0.0


# --- _parse_contacts tests ---


class TestParseContacts:
    """Tests for _parse_contacts extraction logic."""

    def test_multiple_phones_joined(self):
        """Multiple phone contacts joined by ', '."""
        client, _ = _make_client()
        groups = [
            {
                "contacts": [
                    {"type": "phone", "value": "+7 111"},
                    {"type": "phone", "value": "+7 222"},
                ]
            }
        ]
        result = client._parse_contacts(groups)
        assert result["phone"] == "+7 111, +7 222"

    def test_social_links_joined(self):
        """Multiple social contacts joined by ', '."""
        client, _ = _make_client()
        groups = [
            {
                "contacts": [
                    {"type": "vk", "url": "https://vk.com/a"},
                    {"type": "facebook", "url": "https://fb.com/b"},
                ]
            }
        ]
        result = client._parse_contacts(groups)
        assert result["socials"] == "https://vk.com/a, https://fb.com/b"

    def test_website_uses_alias_if_present(self):
        """Website contact prefers alias field over value."""
        client, _ = _make_client()
        groups = [
            {
                "contacts": [
                    {"type": "website", "alias": "example.com", "value": "https://example.com"}
                ]
            }
        ]
        result = client._parse_contacts(groups)
        assert result["website"] == "example.com"

    def test_website_falls_back_to_value(self):
        """Website contact uses value when alias is missing."""
        client, _ = _make_client()
        groups = [
            {
                "contacts": [
                    {"type": "website", "value": "https://example.com"}
                ]
            }
        ]
        result = client._parse_contacts(groups)
        assert result["website"] == "https://example.com"

    def test_empty_groups_returns_empty(self):
        """Empty contact_groups list returns empty strings."""
        client, _ = _make_client()
        result = client._parse_contacts([])
        assert result == {"phone": "", "email": "", "website": "", "socials": ""}

    def test_multiple_groups_merged(self):
        """Contacts from multiple groups are merged."""
        client, _ = _make_client()
        groups = [
            {"contacts": [{"type": "phone", "value": "+7 111"}]},
            {"contacts": [{"type": "email", "value": "a@b.com"}]},
        ]
        result = client._parse_contacts(groups)
        assert result["phone"] == "+7 111"
        assert result["email"] == "a@b.com"


# --- Pagination tests ---


class TestSearchPagination:
    """Tests for search() pagination logic."""

    @pytest.mark.asyncio
    async def test_pagination_two_pages(self):
        """Fetches two pages when total exceeds page_size."""
        client, session = _make_client(page_size=2, request_delay=0.0)

        page1_resp = AsyncMock()
        page1_resp.json = AsyncMock(
            return_value={
                "result": {
                    "total": 4,
                    "items": [
                        _make_item(item_id="1", name="Org 1"),
                        _make_item(item_id="2", name="Org 2"),
                    ],
                }
            }
        )

        page2_resp = AsyncMock()
        page2_resp.json = AsyncMock(
            return_value={
                "result": {
                    "total": 4,
                    "items": [
                        _make_item(item_id="3", name="Org 3"),
                        _make_item(item_id="4", name="Org 4"),
                    ],
                }
            }
        )

        session.get = AsyncMock(
            side_effect=[
                _async_context(page1_resp),
                _async_context(page2_resp),
            ]
        )

        orgs = await client.search("test", count=4)
        assert len(orgs) == 4
        assert orgs[0].name == "Org 1"
        assert orgs[3].name == "Org 4"
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_pagination_stops_on_empty_items(self):
        """Stops paginating when API returns empty items list."""
        client, session = _make_client(page_size=2, request_delay=0.0)

        page1_resp = AsyncMock()
        page1_resp.json = AsyncMock(
            return_value={
                "result": {
                    "total": 10,
                    "items": [
                        _make_item(item_id="1", name="Org 1"),
                        _make_item(item_id="2", name="Org 2"),
                    ],
                }
            }
        )

        page2_resp = AsyncMock()
        page2_resp.json = AsyncMock(
            return_value={
                "result": {"total": 10, "items": []}
            }
        )

        session.get = AsyncMock(
            side_effect=[
                _async_context(page1_resp),
                _async_context(page2_resp),
            ]
        )

        orgs = await client.search("test", count=10)
        assert len(orgs) == 2  # stopped after empty page
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_search_caps_at_requested_count(self):
        """Returns no more than the requested count."""
        client, session = _make_client(page_size=5, request_delay=0.0)

        resp = AsyncMock()
        resp.json = AsyncMock(
            return_value={
                "result": {
                    "total": 100,
                    "items": [_make_item(item_id=str(i), name=f"Org {i}") for i in range(5)],
                }
            }
        )

        session.get = AsyncMock(return_value=_async_context(resp))

        orgs = await client.search("test", count=3)
        assert len(orgs) == 3


# --- Helpers ---


def _async_context(response):
    """Create an async context manager that yields the given response."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm
