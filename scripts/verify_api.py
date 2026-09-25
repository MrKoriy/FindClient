"""2GIS web scraping verification script -- go/no-go gate for the project.

Runs a small search query by scraping 2GIS web pages and reports whether
contact data (phone, email, website, socials) is accessible.

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


async def main() -> None:
    """Run a sample search and print contact data availability."""
    load_dotenv()

    query = "рестораны"
    count = 5

    print("=== 2GIS Web Scraping Verification ===")
    print(f"Query: {query} (Moscow)")

    async with aiohttp.ClientSession() as session:
        client = TwoGISClient(
            session=session,
            page_size=50,
            request_delay=0.5,
        )

        try:
            orgs = await client.search(query=query, count=count)
        except Exception as exc:
            print(f"\nERROR: Scraping failed: {exc}")
            sys.exit(1)

    if not orgs:
        print("\nNo results returned. Check your network connection.")
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
        print("VERDICT: PASS -- contact data is accessible via web scraping")
    else:
        print("VERDICT: FAIL -- no contact data found on web pages.")
        sys.exit(1)


def _pct(part: int, total: int) -> int:
    """Return integer percentage, safe against division by zero."""
    return round(part * 100 / total) if total else 0


if __name__ == "__main__":
    asyncio.run(main())
