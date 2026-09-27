"""Step 1 of the Telethon login: request a login code.

Prints the phone_code_hash needed by step 2. Telegram sends the code to the
account (app notification or SMS).

    python3 tg_step1.py +79001234567
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv("/opt/2gi_scraper/.env")


async def main() -> None:
    phone = sys.argv[1]
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]

    client = TelegramClient(StringSession(), api_id, api_hash, flood_sleep_threshold=60)
    await client.connect()
    sent = await client.send_code_request(phone)
    print(f"PHONE={phone}")
    print(f"PHONE_CODE_HASH={sent.phone_code_hash}")
    print(f"CODE_TYPE={type(sent.type).__name__}")
    print("CODE_SENT=yes")
    await client.disconnect()


asyncio.run(main())
