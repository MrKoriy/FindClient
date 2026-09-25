"""Scrape orchestration: 2GIS search -> dedup -> save -> CSV export."""

import csv
import io
from collections.abc import Awaitable, Callable
from dataclasses import asdict

import aiohttp

from api.twogis_client import TwoGISClient
from db.database import Database
from models.organization import Organization

# CSV columns with Russian headers for Excel compatibility.
_CSV_COLUMNS = [
    ("name", "Название"),
    ("phone", "Телефон"),
    ("email", "Email"),
    ("website", "Сайт"),
    ("address", "Адрес"),
    ("rating", "Рейтинг"),
    ("socials", "Соцсети"),
]


class ScrapeResult:
    """Result of a scrape operation."""

    __slots__ = ("organizations", "total_scraped", "duplicates_removed", "already_in_db", "csv_bytes")

    def __init__(
        self,
        organizations: list[Organization],
        total_scraped: int,
        duplicates_removed: int,
        already_in_db: int,
        csv_bytes: bytes,
    ) -> None:
        self.organizations = organizations
        self.total_scraped = total_scraped
        self.duplicates_removed = duplicates_removed
        self.already_in_db = already_in_db
        self.csv_bytes = csv_bytes

    @property
    def with_phone(self) -> int:
        return sum(1 for o in self.organizations if o.phone)

    @property
    def with_email(self) -> int:
        return sum(1 for o in self.organizations if o.email)

    @property
    def with_website(self) -> int:
        return sum(1 for o in self.organizations if o.website)

    @property
    def with_socials(self) -> int:
        return sum(1 for o in self.organizations if o.socials)


class ScrapeService:
    """Orchestrates scraping, dedup, persistence, and CSV generation."""

    # Approximate results per 2GIS search page.
    _RESULTS_PER_PAGE = 12

    def __init__(self, db: Database, request_delay: float = 0.3) -> None:
        self.db = db
        self.request_delay = request_delay

    async def scrape(
        self,
        niche: str,
        count: int,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> ScrapeResult:
        """Run full scrape flow: skip known pages -> search -> save -> CSV.

        Calculates how many pages were already scraped for this niche,
        starts from the next page, and returns only new contacts.
        """
        known_ids = await self.db.get_known_org_ids(niche)
        known_count = len(known_ids)

        # Skip pages we already scraped.  Go back 1 page for safety.
        start_page = max(1, known_count // self._RESULTS_PER_PAGE)

        async with aiohttp.ClientSession() as session:
            client = TwoGISClient(
                session=session,
                request_delay=self.request_delay,
            )
            all_orgs = await client.search(
                query=niche,
                count=count,
                start_page=start_page,
                on_progress=on_progress,
            )

        total_scraped = len(all_orgs)

        # Filter out any overlap from the safety page.
        new_orgs = [o for o in all_orgs if o.id not in known_ids]
        duplicates_removed = total_scraped - len(new_orgs)
        new_orgs = new_orgs[:count]

        if new_orgs:
            org_dicts = [asdict(o) for o in new_orgs]
            await self.db.save_session(niche, org_dicts)

        csv_bytes = _generate_csv(new_orgs)

        return ScrapeResult(
            organizations=new_orgs,
            total_scraped=total_scraped,
            duplicates_removed=duplicates_removed,
            already_in_db=known_count,
            csv_bytes=csv_bytes,
        )


def _generate_csv(organizations: list[Organization]) -> bytes:
    """Generate CSV bytes with utf-8-sig encoding for Excel Cyrillic support."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    # Header row with Russian names.
    writer.writerow([col[1] for col in _CSV_COLUMNS])

    for org in organizations:
        data = asdict(org)
        row = [str(data.get(col[0], "")) for col in _CSV_COLUMNS]
        writer.writerow(row)

    return buf.getvalue().encode("utf-8-sig")
