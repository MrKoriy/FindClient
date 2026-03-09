"""2GIS API client for searching organizations and parsing contact data."""

import asyncio
from typing import Any

import aiohttp

from models.organization import Organization


# Social network contact types recognized by the parser.
_SOCIAL_TYPES = frozenset(
    ("vk", "instagram", "facebook", "twitter", "youtube", "skype", "icq")
)


class TwoGISClient:
    """Searches 2GIS catalog and returns parsed Organization objects.

    Args:
        session: An open aiohttp.ClientSession for making HTTP requests.
        api_key: 2GIS API key.
        base_url: Catalog API endpoint.
        page_size: Number of items per page (max 50).
        request_delay: Seconds to sleep between paginated requests.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        base_url: str = "https://catalog.api.2gis.com/3.0/items",
        page_size: int = 50,
        request_delay: float = 0.3,
    ) -> None:
        self.session = session
        self.api_key = api_key
        self.base_url = base_url
        self.page_size = page_size
        self.request_delay = request_delay

    async def search(self, query: str, count: int) -> list[Organization]:
        """Search for organizations and return up to *count* results.

        Paginates automatically, sleeping *request_delay* seconds between
        pages.  Stops when the requested count is reached, the API returns
        an empty items list, or there are no more pages.
        """
        organizations: list[Organization] = []
        page = 1

        while len(organizations) < count:
            params = self._build_params(query, page)

            async with self.session.get(self.base_url, params=params) as resp:
                data = await resp.json()

            result = data.get("result", {})
            items: list[dict[str, Any]] = result.get("items", [])

            if not items:
                break

            organizations.extend(self._parse_items(items))
            page += 1

            # If we already have enough, stop before sleeping
            if len(organizations) >= count:
                break

            # Respect rate limiting between pages
            if self.request_delay > 0:
                await asyncio.sleep(self.request_delay)

        return organizations[:count]

    # ------------------------------------------------------------------
    # Parameter building
    # ------------------------------------------------------------------

    def _build_params(self, query: str, page: int) -> dict[str, Any]:
        """Build query parameters for the 2GIS catalog API."""
        return {
            "q": query,
            "key": self.api_key,
            "page_size": self.page_size,
            "page": page,
            "fields": (
                "items.contact_groups,items.org,items.reviews,"
                "items.schedule,items.external_content"
            ),
            "sort": "general_rating",
            "city_id": 4504222397630173,  # Moscow
        }

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_items(self, items: list[dict[str, Any]]) -> list[Organization]:
        """Parse a list of raw API item dicts into Organization objects."""
        return [self._parse_item(item) for item in items]

    def _parse_item(self, item: dict[str, Any]) -> Organization:
        """Parse a single API item dict into an Organization.

        Uses defensive `.get()` access so that missing keys never cause
        a crash -- absent fields fall back to empty strings / zero.
        """
        contact_groups = item.get("contact_groups", [])
        contacts = self._parse_contacts(contact_groups)

        address = item.get("address_name") or item.get("full_address_name", "")
        rating = item.get("reviews", {}).get("general_rating", 0.0)

        return Organization(
            id=str(item.get("id", "")),
            name=item.get("name", ""),
            phone=contacts["phone"],
            email=contacts["email"],
            website=contacts["website"],
            address=address,
            rating=float(rating),
            socials=contacts["socials"],
        )

    def _parse_contacts(self, contact_groups: list[dict[str, Any]]) -> dict[str, str]:
        """Extract phone, email, website, and socials from contact_groups.

        The 2GIS API returns contact_groups as a list of groups, each
        containing a ``contacts`` list.  Each contact dict has at least
        ``type`` and ``value`` keys; websites may also have ``alias``,
        and social links may have ``url``.

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
                    # Prefer short alias (e.g. "example.com") over full URL
                    websites.append(contact.get("alias", value))
                elif ctype in _SOCIAL_TYPES:
                    socials.append(contact.get("url", value))

        return {
            "phone": ", ".join(phones),
            "email": ", ".join(emails),
            "website": ", ".join(websites),
            "socials": ", ".join(socials),
        }
