"""2GIS API verification script -- go/no-go gate for the project.

Runs a small search query against the 2GIS catalog API and reports
whether the API key returns contact data (phone, email, website,
socials).  If contact_groups data is inaccessible, the project
cannot proceed as designed.

Usage:
    python scripts/verify_api.py
"""

import asyncio
import sys

import aiohttp
from dotenv import load_dotenv

# Allow running as `python scripts/verify_api.py` from project root.
sys.path.insert(0, ".")

from api.twogis_client import TwoGISClient
from config import Settings


async def main() -> None:
    """Run a sample search and print contact data availability."""
    load_dotenv()

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    query = "рестораны"
    count = 5

    print("=== 2GIS API Verification ===")
    print(f"Query: {query} (Moscow)")

    async with aiohttp.ClientSession() as session:
        client = TwoGISClient(
            session=session,
            api_key=settings.TWOGIS_API_KEY,
            base_url=settings.TWOGIS_BASE_URL,
            page_size=settings.PAGE_SIZE,
            request_delay=settings.REQUEST_DELAY,
        )

        try:
            orgs = await client.search(query=query, count=count)
        except Exception as exc:
            print(f"\nERROR: API request failed: {exc}")
            sys.exit(1)

    if not orgs:
        print("\nNo results returned. Check your API key and network.")
        sys.exit(1)

    print(f"Results: {len(orgs)}\n")

    # Print each organization's details
    with_phone = 0
    with_email = 0
    with_website = 0
    with_socials = 0

    for i, org in enumerate(orgs, 1):
        print(f"{i}. {org.name}")
        print(f"   Phone: {org.phone or '(none)'}")
        print(f"   Email: {org.email or '(none)'}")
        print(f"   Website: {org.website or '(none)'}")
        print(f"   Address: {org.address or '(none)'}")
        print(f"   Rating: {org.rating}")
        print(f"   Socials: {org.socials or '(none)'}")
        print()

        if org.phone:
            with_phone += 1
        if org.email:
            with_email += 1
        if org.website:
            with_website += 1
        if org.socials:
            with_socials += 1

    # Summary
    total = len(orgs)
    print("=== Contact Data Summary ===")
    print(f"With phone: {with_phone}/{total} ({_pct(with_phone, total)}%)")
    print(f"With email: {with_email}/{total} ({_pct(with_email, total)}%)")
    print(f"With website: {with_website}/{total} ({_pct(with_website, total)}%)")
    print(f"With socials: {with_socials}/{total} ({_pct(with_socials, total)}%)")
    print()

    # Verdict: at least one org must have at least one contact field
    has_any_contact = any(
        org.phone or org.email or org.website or org.socials for org in orgs
    )

    if has_any_contact:
        print("VERDICT: PASS -- contact data is accessible")
    else:
        print("VERDICT: FAIL -- API key does not return contact data.")
        print(
            "The items.contact_groups field may require a paid API subscription."
        )
        print(
            "See: https://docs.2gis.com/en/platform-manager/subscription/services"
        )
        sys.exit(1)


def _pct(part: int, total: int) -> int:
    """Return integer percentage, safe against division by zero."""
    return round(part * 100 / total) if total else 0


if __name__ == "__main__":
    asyncio.run(main())
