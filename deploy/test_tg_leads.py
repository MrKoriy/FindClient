"""Functional check: can the reused account actually search chats and read groups?"""

import asyncio
import os
import sys

sys.path.insert(0, "/opt/2gi_scraper")

from dotenv import load_dotenv

load_dotenv("/opt/2gi_scraper/.env")

from services.telegram_service import TelegramUserService


async def main() -> None:
    svc = TelegramUserService(
        int(os.environ["TG_API_ID"]),
        os.environ["TG_API_HASH"],
        os.environ["TG_SESSION"],
    )
    ok = await svc.start()
    print(f"START={ok} error={svc.error!r}", flush=True)
    if not ok:
        return

    found = await svc.search_chats(["прорабы"], modifiers=("чат",))
    print(f"SEARCH_CHATS={len(found)}", flush=True)
    for c in found[:5]:
        print(f"   @{c['username']} | {c['title'][:40]} | {c['members']} | {c['type']}", flush=True)

    leads = await svc.harvest_authors(["stroiteli_moscow"], per_chat=60, with_bio=0)
    print(f"HARVEST_AUTHORS={len(leads)}", flush=True)
    for lead in leads[:6]:
        print(
            f"   @{lead.username or '-'} | {lead.name[:25]} | {lead.phone or '-'} | "
            f"msgs={lead.messages} | score={lead.score}",
            flush=True,
        )

    await svc.stop()
    print("DONE", flush=True)


asyncio.run(main())
