"""2GIS web scraper for searching organizations and extracting contact data.

Fully web-based: scrapes 2GIS search pages for results and firm pages
for contact details.  No API key required.
"""

import asyncio
import json
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import aiohttp

from models.organization import Organization


# Social network contact types recognized by the parser.
_SOCIAL_TYPES = frozenset(
    ("vk", "vkontakte", "instagram", "facebook", "twitter", "youtube", "skype", "icq")
)

# Rotating User-Agents to reduce fingerprinting.
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
]

_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 503}

try:
    import brotli  # noqa: F401
    _ACCEPT_ENCODING = "gzip, deflate, br"
except ImportError:
    _ACCEPT_ENCODING = "gzip, deflate"

_SEARCH_URL = "https://2gis.ru/moscow/search/{query}"
_SEARCH_PAGE_URL = "https://2gis.ru/moscow/search/{query}/page/{page}"
_FIRM_URL = "https://2gis.ru/moscow/firm/{org_id}"

# Both search and firm pages use: var initialState = JSON.parse('...');
_STATE_RE = re.compile(
    r"var\s+initialState\s*=\s*JSON\.parse\('(.+?)'\);", re.DOTALL
)


class TwoGISClient:
    """Scrapes 2GIS website for organization search and contact data.

    No API key needed -- all data comes from web page scraping.

    Args:
        session: An open aiohttp.ClientSession for making HTTP requests.
        page_size: Not used for web scraping (kept for interface compat).
        request_delay: Seconds to sleep between HTTP requests.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        page_size: int = 50,
        request_delay: float = 0.3,
    ) -> None:
        self.session = session
        self.page_size = page_size
        self.request_delay = request_delay

    async def search(
        self,
        query: str,
        count: int,
        start_page: int = 1,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> list[Organization]:
        """Search for organizations and return up to *count* results.

        Args:
            start_page: Page number to start from (skip earlier pages).
            on_progress: Optional async callback(enriched_so_far, total).
        """
        organizations: list[Organization] = []
        page = start_page

        while len(organizations) < count:
            orgs_from_page = await self._fetch_search_page(query, page)
            if not orgs_from_page:
                break

            organizations.extend(orgs_from_page)
            page += 1

            if len(organizations) >= count:
                break

            if self.request_delay > 0:
                await asyncio.sleep(self._jittered_delay())

        organizations = organizations[:count]

        # Enrich each org with contacts from firm pages
        for i, org in enumerate(organizations):
            if not org.phone and not org.email and not org.website:
                contacts = await self._fetch_contacts_from_web(org.id)
                if contacts:
                    org.phone = contacts["phone"]
                    org.email = contacts["email"]
                    org.website = contacts["website"]
                    org.socials = contacts["socials"]
                if self.request_delay > 0:
                    await asyncio.sleep(self._jittered_delay())

            if on_progress and (i + 1) % 5 == 0:
                await on_progress(i + 1, len(organizations))

        return organizations

    def _jittered_delay(self) -> float:
        """Return request_delay with +/- 50% random jitter."""
        return self.request_delay * random.uniform(0.5, 1.5)

    # ------------------------------------------------------------------
    # Search page scraping
    # ------------------------------------------------------------------

    async def _fetch_search_page(
        self, query: str, page: int
    ) -> list[Organization]:
        """Fetch and parse a search results page from 2GIS website."""
        encoded = quote(query)
        if page == 1:
            url = _SEARCH_URL.format(query=encoded)
        else:
            url = _SEARCH_PAGE_URL.format(query=encoded, page=page)

        html = await self._fetch_html(url)
        if html is None:
            return []

        state = _extract_state(html)
        if state is None:
            return []

        return _parse_search_profiles(state)

    # ------------------------------------------------------------------
    # Firm page scraping for contacts
    # ------------------------------------------------------------------

    async def _fetch_contacts_from_web(self, org_id: str) -> dict[str, str] | None:
        """Scrape contact data from the 2GIS firm web page."""
        url = _FIRM_URL.format(org_id=org_id)
        html = await self._fetch_html(url)
        if html is None:
            return None

        state = _extract_state(html)
        if state is None:
            return None

        contact_groups = _find_contact_groups(state)
        if contact_groups is None:
            return None

        return self._parse_contacts(contact_groups)

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    async def _fetch_html(self, url: str) -> str | None:
        """Fetch a URL with rotating UA, realistic headers, and retry on 429/503."""
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.5,en;q=0.3",
            "Accept-Encoding": _ACCEPT_ENCODING,
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        for attempt in range(_MAX_RETRIES):
            try:
                async with self.session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.text()
                    if resp.status in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                        backoff = (2 ** attempt) + random.uniform(1, 3)
                        await asyncio.sleep(backoff)
                        continue
                    return None
            except Exception:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return None
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_contacts(
        self, contact_groups: list[dict[str, Any]]
    ) -> dict[str, str]:
        """Extract phone, email, website, and socials from contact_groups.

        Returns a dict with keys ``phone``, ``email``, ``website``,
        ``socials`` -- each a comma-joined string or empty string.
        """
        phones: list[str] = []
        emails: list[str] = []
        websites: list[str] = []
        socials: list[str] = []

        for group in contact_groups:
            for contact in group.get("contacts", []):
                ctype = contact.get("type", "")
                value = contact.get("value", "")

                if ctype == "phone":
                    phones.append(value)
                elif ctype == "email":
                    emails.append(value)
                elif ctype == "website":
                    # Prefer alias, fall back to value, strip 2GIS redirect
                    url = contact.get("alias") or value
                    if "link.2gis.ru" in url and "?" in url:
                        url = url.split("?", 1)[1]
                    websites.append(url)
                elif ctype in _SOCIAL_TYPES:
                    socials.append(contact.get("url", value))

        return {
            "phone": ", ".join(phones),
            "email": ", ".join(emails),
            "website": ", ".join(websites),
            "socials": ", ".join(socials),
        }


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _extract_state(html: str) -> dict[str, Any] | None:
    """Extract initialState JSON from a 2GIS page (search or firm).

    Both page types embed state as ``var initialState = JSON.parse('...');``
    with JS single-quote escaping.
    """
    match = _STATE_RE.search(html)
    if not match:
        return None

    raw = match.group(1)
    # JS single-quoted string unescaping (backslash first, then quotes)
    raw = raw.replace("\\\\", "\\")
    raw = raw.replace("\\'", "'")

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_search_profiles(state: dict[str, Any]) -> list[Organization]:
    """Extract Organization list from search page initialState.

    Data lives at ``state.data.entity.profile`` as a dict of
    ``{firm_id: {data: {...}}}`` entries.
    """
    profiles = (
        state.get("data", {}).get("entity", {}).get("profile", {})
    )
    if not isinstance(profiles, dict):
        return []

    orgs: list[Organization] = []
    for firm_id, profile in profiles.items():
        data = profile.get("data", {})
        if not isinstance(data, dict):
            continue

        name = data.get("name_ex", {}).get("primary", "") or data.get("name", "")
        address = data.get("address_name", "")
        rating = data.get("reviews", {}).get("general_rating", 0.0)

        orgs.append(Organization(
            id=str(firm_id),
            name=name,
            address=address,
            rating=float(rating),
        ))

    return orgs


def _find_contact_groups(obj: Any, depth: int = 0) -> list[dict[str, Any]] | None:
    """Recursively search for a non-empty ``contact_groups`` list."""
    if depth > 12:
        return None

    if isinstance(obj, dict):
        if "contact_groups" in obj:
            cg = obj["contact_groups"]
            if isinstance(cg, list) and cg:
                return cg
        for value in obj.values():
            result = _find_contact_groups(value, depth + 1)
            if result is not None:
                return result

    elif isinstance(obj, list):
        for item in obj:
            result = _find_contact_groups(item, depth + 1)
            if result is not None:
                return result

    return None
