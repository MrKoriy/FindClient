"""Step 2 of the Telethon login: sign in with the code and print TG_SESSION.

    python3 tg_step2.py +79001234567 <phone_code_hash> <code> [2fa_password]
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

load_dotenv("/opt/2gi_scraper/.env")


async def main() -> None:
    phone, code_hash, code = sys.argv[1], sys.argv[2], sys.argv[3]
    password = sys.argv[4] if len(sys.argv) > 4 else ""
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]

    client = TelegramClient(StringSession(), api_id, api_hash, flood_sleep_threshold=60)
    await client.connect()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
    except SessionPasswordNeededError:
        if not password:
            print("NEED_2FA_PASSWORD=yes")
            await client.disconnect()
            sys.exit(2)
        await client.sign_in(password=password)

    me = await client.get_me()
    print(f"LOGGED_IN_AS={me.first_name} @{me.username} id={me.id}")
    print(f"TG_SESSION={client.session.save()}")
    await client.disconnect()


asyncio.run(main())
