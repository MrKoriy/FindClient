"""One-time login for the Telegram user account; prints TG_SESSION for .env.

Get TG_API_ID / TG_API_HASH at https://my.telegram.org -> API development tools.
Use a separate (ideally aged) account: mass reading is fine, mass messaging gets banned.

    python scripts/tg_login.py
"""

import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    load_dotenv()
    api_id = int(os.environ.get("TG_API_ID") or input("TG_API_ID: "))
    api_hash = os.environ.get("TG_API_HASH") or input("TG_API_HASH: ")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = await client.get_me()
        print(f"\nВошли как {me.first_name} (@{me.username}). Добавьте в .env:\n")
        print(f"TG_SESSION={client.session.save()}")


if __name__ == "__main__":
    asyncio.run(main())
