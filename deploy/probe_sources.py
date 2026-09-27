import asyncio
import sys

sys.path.insert(0, "/opt/2gi_scraper")

import aiohttp

from api.orders import fetch_fl, fetch_freelance_ru, fetch_kwork
from api.twogis_api import TwoGISApi
from api.yandex_client import YandexMapsClient


async def main() -> None:
    t = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=t) as s:
        print("--- 2GIS (ожидаем провал) ---")
        try:
            orgs = await TwoGISApi(s, request_delay=0.2).search("стоматология", "Казань", 3)
            print(f"  2GIS: {len(orgs)} организаций")
            for o in orgs[:3]:
                print(f"    {o.name[:40]} | {o.phone[:18]!r} | score={o.score}")
        except Exception as e:
            print("  2GIS FAILED:", repr(e)[:160])

        print("--- Яндекс Карты (веб-режим без ключа) ---")
        try:
            orgs = await YandexMapsClient(s).search("стоматология", "Казань", 5)
            print(f"  Яндекс: {len(orgs)} организаций")
            for o in orgs[:5]:
                print(f"    {o.name[:40]} | {o.phone[:18]!r} | {o.website[:26]!r} | score={o.score}")
        except Exception as e:
            print("  Яндекс FAILED:", repr(e)[:200])

        for name, fn in (("Kwork", fetch_kwork), ("FL.ru", fetch_fl), ("Freelance.ru", fetch_freelance_ru)):
            try:
                got = await fn(s)
                print(f"--- {name}: {len(got)} заказов; пример: {got[0].title[:60] if got else '-'}")
            except Exception as e:
                print(f"--- {name} FAILED:", repr(e)[:140])


asyncio.run(main())
