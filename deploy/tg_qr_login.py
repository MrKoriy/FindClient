"""Telegram login via QR code — no code delivery involved.

Writes the current tg://login URL to /root/tg_qr_url.txt (rotates every ~20 s)
and, on success, the session string to /root/tg_session.txt.

Scan it from an already-authorized Telegram app:
    Settings -> Devices -> Link Desktop Device
The session will belong to whichever account scans it.
"""

import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

load_dotenv("/opt/2gi_scraper/.env")

URL_FILE = "/root/tg_qr_url.txt"
RESULT_FILE = "/root/tg_session.txt"
WINDOW = 900


async def main() -> None:
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]

    client = TelegramClient(StringSession(), api_id, api_hash, flood_sleep_threshold=60)
    await client.connect()

    for stale in (URL_FILE, RESULT_FILE):
        if os.path.exists(stale):
            os.remove(stale)

    qr = await client.qr_login()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WINDOW
    logged_in = False

    while loop.time() < deadline:
        with open(URL_FILE, "w") as fh:
            fh.write(qr.url)
        print(f"QR_URL={qr.url}", flush=True)
        try:
            await qr.wait(timeout=20)
            logged_in = True
            break
        except asyncio.TimeoutError:
            try:
                await qr.recreate()
            except Exception as exc:  # noqa: BLE001
                print(f"RECREATE_FAILED={type(exc).__name__}: {exc}", flush=True)
                break
        except SessionPasswordNeededError:
            print("NEED_2FA_PASSWORD=yes", flush=True)
            break

    if logged_in:
        me = await client.get_me()
        with open(RESULT_FILE, "w") as fh:
            fh.write(client.session.save())
        print(f"LOGGED_IN_AS={me.first_name} @{me.username} id={me.id}", flush=True)
        print("SESSION_WRITTEN=1", flush=True)
    else:
        print("NOT_LOGGED_IN", flush=True)

    await client.disconnect()


asyncio.run(main())
