"""Unit tests for TwoGISClient web scraping logic."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.twogis_client import (
    TwoGISClient,
    _extract_state,
    _find_contact_groups,
    _parse_search_profiles,
)
from models.organization import Organization


# --- Fixtures ---


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
        "page_size": 50,
        "request_delay": 0.0,
    }
    defaults.update(overrides)
    return TwoGISClient(**defaults), session


def _async_context(response):
    """Create an async context manager that yields the given response."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _build_search_html(profiles: dict):
    """Build fake 2GIS search page HTML with initialState profiles.

    profiles: {firm_id: {name, address_name, rating}, ...}
    """
    state = {"data": {"entity": {"profile": {}}}}
    for fid, info in profiles.items():
        state["data"]["entity"]["profile"][fid] = {
            "data": {
                "name_ex": {"primary": info.get("name", "")},
                "address_name": info.get("address_name", ""),
                "reviews": {"general_rating": info.get("rating", 0.0)},
            }
        }
    # JS-escape the JSON for embedding in JSON.parse('...')
    raw = json.dumps(state, ensure_ascii=False)
    raw = raw.replace("\\", "\\\\").replace("'", "\\'")
    return f"<html><script>var initialState = JSON.parse('{raw}');</script></html>"


def _build_firm_html(contact_groups):
    """Build fake 2GIS firm page HTML with var initialState."""
    state = {"data": {"entity": {"profile": {"firm1": {"data": {"contact_groups": contact_groups}}}}}}
    raw = json.dumps(state, ensure_ascii=False)
    raw = raw.replace("\\", "\\\\").replace("'", "\\'")
    return f"<html><script>var initialState = JSON.parse('{raw}');</script></html>"


# --- _parse_contacts tests ---


class TestParseContacts:
    """Tests for _parse_contacts extraction logic."""

    def test_full_contact_groups(self):
        """All contact fields present -- phone, email, website, socials."""
        client, _ = _make_client()
        result = client._parse_contacts(FULL_CONTACT_GROUPS)

        assert "+7 (495) 111-11-11" in result["phone"]
        assert "+7 (495) 222-22-22" in result["phone"]
        assert result["email"] == "info@example.com"
        assert "example.com" in result["website"]
        assert "vk.com/example" in result["socials"]
        assert "instagram.com/example" in result["socials"]

    def test_multiple_phones_joined(self):
        client, _ = _make_client()
        groups = [{"contacts": [
            {"type": "phone", "value": "+7 111"},
            {"type": "phone", "value": "+7 222"},
        ]}]
        result = client._parse_contacts(groups)
        assert result["phone"] == "+7 111, +7 222"

    def test_website_uses_alias_if_present(self):
        client, _ = _make_client()
        groups = [{"contacts": [
            {"type": "website", "alias": "example.com", "value": "https://example.com"}
        ]}]
        result = client._parse_contacts(groups)
        assert result["website"] == "example.com"

    def test_website_falls_back_to_value(self):
        client, _ = _make_client()
        groups = [{"contacts": [
            {"type": "website", "value": "https://example.com"}
        ]}]
        result = client._parse_contacts(groups)
        assert result["website"] == "https://example.com"

    def test_empty_groups_returns_empty(self):
        client, _ = _make_client()
        result = client._parse_contacts([])
        assert result == {"phone": "", "email": "", "website": "", "socials": ""}

    def test_multiple_groups_merged(self):
        client, _ = _make_client()
        groups = [
            {"contacts": [{"type": "phone", "value": "+7 111"}]},
            {"contacts": [{"type": "email", "value": "a@b.com"}]},
        ]
        result = client._parse_contacts(groups)
        assert result["phone"] == "+7 111"
        assert result["email"] == "a@b.com"

    def test_vkontakte_parsed_as_social(self):
        client, _ = _make_client()
        groups = [{"contacts": [
            {"type": "vkontakte", "url": "https://vk.com/club123", "value": "https://vk.com/club123"},
        ]}]
        result = client._parse_contacts(groups)
        assert result["socials"] == "https://vk.com/club123"


# --- Search page extraction tests ---


class TestExtractState:
    """Tests for HTML state extraction."""

    def test_extracts_from_search_page(self):
        profiles = {"123": {"name": "Test", "address_name": "Addr", "rating": 4.0}}
        html = _build_search_html(profiles)
        state = _extract_state(html)
        assert state is not None
        assert "data" in state

    def test_extracts_from_firm_page(self):
        html = _build_firm_html([{"contacts": []}])
        state = _extract_state(html)
        assert state is not None
        assert "data" in state

    def test_returns_none_without_initialstate(self):
        assert _extract_state("<html><body></body></html>") is None

    def test_handles_js_escaping(self):
        """Correctly unescapes JS single-quoted string with backslashes."""
        profiles = {"456": {"name": "Peet's Coffee", "address_name": "Test St", "rating": 3.0}}
        html = _build_search_html(profiles)
        state = _extract_state(html)
        assert state is not None
        profs = state["data"]["entity"]["profile"]
        assert "456" in profs


class TestParseSearchProfiles:
    """Tests for search profile extraction from state."""

    def test_parses_profiles(self):
        state = {
            "data": {"entity": {"profile": {
                "111": {"data": {
                    "name_ex": {"primary": "Org A"},
                    "address_name": "ул. Тверская",
                    "reviews": {"general_rating": 4.5},
                }},
                "222": {"data": {
                    "name_ex": {"primary": "Org B"},
                    "address_name": "ул. Арбат",
                    "reviews": {"general_rating": 3.0},
                }},
            }}}
        }
        orgs = _parse_search_profiles(state)
        assert len(orgs) == 2
        ids = {o.id for o in orgs}
        assert ids == {"111", "222"}

    def test_empty_profiles(self):
        state = {"data": {"entity": {"profile": {}}}}
        assert _parse_search_profiles(state) == []

    def test_missing_path(self):
        assert _parse_search_profiles({}) == []
        assert _parse_search_profiles({"data": {}}) == []


# --- Firm page extraction tests ---




# --- Recursive finder tests ---


class TestFindContactGroups:

    def test_finds_at_top_level(self):
        obj = {"contact_groups": [{"contacts": []}]}
        assert _find_contact_groups(obj) == [{"contacts": []}]

    def test_finds_nested(self):
        obj = {"a": {"b": {"c": {"contact_groups": [{"contacts": []}]}}}}
        assert _find_contact_groups(obj) == [{"contacts": []}]

    def test_returns_none_when_missing(self):
        assert _find_contact_groups({"a": 1, "b": [2, 3]}) is None

    def test_skips_empty_contact_groups(self):
        obj = {
            "first": {"contact_groups": []},
            "second": {"contact_groups": [{"contacts": [{"type": "phone", "value": "+7"}]}]},
        }
        result = _find_contact_groups(obj)
        assert result is not None
        assert result[0]["contacts"][0]["value"] == "+7"


# --- Integration: search() tests ---


class TestSearch:

    @pytest.mark.asyncio
    async def test_search_enriches_with_firm_contacts(self):
        """search() scrapes search page for orgs, then firm pages for contacts."""
        client, session = _make_client(request_delay=0.0)

        profiles = {"999": {"name": "Org 1", "address_name": "Addr 1", "rating": 4.0}}
        search_resp = AsyncMock()
        search_resp.status = 200
        search_resp.text = AsyncMock(return_value=_build_search_html(profiles))

        firm_contacts = [{"contacts": [{"type": "phone", "value": "+79001234567"}]}]
        firm_resp = AsyncMock()
        firm_resp.status = 200
        firm_resp.text = AsyncMock(return_value=_build_firm_html(firm_contacts))

        session.get = MagicMock(side_effect=[
            _async_context(search_resp),
            _async_context(firm_resp),
        ])

        orgs = await client.search("test", count=1)
        assert len(orgs) == 1
        assert orgs[0].name == "Org 1"
        assert orgs[0].phone == "+79001234567"

    @pytest.mark.asyncio
    async def test_firm_page_failure_leaves_empty_contacts(self):
        client, session = _make_client(request_delay=0.0)

        profiles = {"888": {"name": "Org X", "address_name": "Addr X", "rating": 3.0}}
        search_resp = AsyncMock()
        search_resp.status = 200
        search_resp.text = AsyncMock(return_value=_build_search_html(profiles))

        firm_resp = AsyncMock()
        firm_resp.status = 403
        firm_resp.text = AsyncMock(return_value="Forbidden")

        session.get = MagicMock(side_effect=[
            _async_context(search_resp),
            _async_context(firm_resp),
        ])

        orgs = await client.search("test", count=1)
        assert len(orgs) == 1
        assert orgs[0].phone == ""

    @pytest.mark.asyncio
    async def test_pagination_multiple_pages(self):
        client, session = _make_client(request_delay=0.0)

        page1 = {"1": {"name": "A", "address_name": "", "rating": 0}}
        page2 = {"2": {"name": "B", "address_name": "", "rating": 0}}
        resp1 = AsyncMock()
        resp1.status = 200
        resp1.text = AsyncMock(return_value=_build_search_html(page1))
        resp2 = AsyncMock()
        resp2.status = 200
        resp2.text = AsyncMock(return_value=_build_search_html(page2))

        firm_resp = AsyncMock()
        firm_resp.status = 404
        firm_resp.text = AsyncMock(return_value="")

        session.get = MagicMock(side_effect=[
            _async_context(resp1),
            _async_context(resp2),
            _async_context(firm_resp),
            _async_context(firm_resp),
        ])

        orgs = await client.search("test", count=2)
        assert len(orgs) == 2

    @pytest.mark.asyncio
    async def test_stops_on_empty_page(self):
        client, session = _make_client(request_delay=0.0)

        page1 = {"1": {"name": "A", "address_name": "", "rating": 0}}
        resp1 = AsyncMock()
        resp1.status = 200
        resp1.text = AsyncMock(return_value=_build_search_html(page1))

        # Empty second page (no initialState)
        resp2 = AsyncMock()
        resp2.status = 200
        resp2.text = AsyncMock(return_value="<html></html>")

        firm_resp = AsyncMock()
        firm_resp.status = 404
        firm_resp.text = AsyncMock(return_value="")

        session.get = MagicMock(side_effect=[
            _async_context(resp1),
            _async_context(resp2),
            _async_context(firm_resp),
        ])

        orgs = await client.search("test", count=10)
        assert len(orgs) == 1

    @pytest.mark.asyncio
    async def test_caps_at_requested_count(self):
        client, session = _make_client(request_delay=0.0)

        profiles = {
            str(i): {"name": f"Org {i}", "address_name": "", "rating": 0}
            for i in range(5)
        }
        search_resp = AsyncMock()
        search_resp.status = 200
        search_resp.text = AsyncMock(return_value=_build_search_html(profiles))

        firm_resp = AsyncMock()
        firm_resp.status = 404
        firm_resp.text = AsyncMock(return_value="")

        session.get = MagicMock(side_effect=[
            _async_context(search_resp),
            *[_async_context(firm_resp) for _ in range(3)],
        ])

        orgs = await client.search("test", count=3)
        assert len(orgs) == 3
