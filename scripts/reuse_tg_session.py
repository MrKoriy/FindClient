"""Reuse an existing authorized Telethon session for FindClient.

FindClient expects TG_SESSION as a StringSession, while an established
session may live as a SQLite file used by other scripts. We connect with
the file, verify it is still authorized, export a StringSession and write
it into the project's .env.

    python scripts/reuse_tg_session.py [путь/к/сессии] [.env]
"""

import asyncio
import os
import re
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
if not API_ID or not API_HASH:
    raise SystemExit("нужны TG_API_ID и TG_API_HASH в окружении")

SRC = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TG_SESSION_FILE", ".sessions/crm_sender")
ENV = sys.argv[2] if len(sys.argv) > 2 else ".env"


def set_key(text: str, key: str, value: str) -> str:
    if re.search(rf"^{key}=.*$", text, re.M):
        return re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    return text.rstrip("\n") + f"\n{key}={value}\n"


async def main() -> None:
    client = TelegramClient(SRC, API_ID, API_HASH, flood_sleep_threshold=60)
    await client.connect()

    if not await client.is_user_authorized():
        print("RESULT=NOT_AUTHORIZED")
        await client.disconnect()
        return

    me = await client.get_me()
    who = f"{me.first_name or ''} {me.last_name or ''}".strip()
    print(f"ACCOUNT={who} @{me.username} id={me.id} phone={me.phone}")

    session = client.session
    ss = StringSession()
    ss.set_dc(session.dc_id, session.server_address, session.port)
    ss.auth_key = session.auth_key
    string = ss.save()
    await client.disconnect()

    with open(ENV, encoding="utf-8") as fh:
        text = fh.read()
    text = set_key(text, "TG_API_ID", str(API_ID))
    text = set_key(text, "TG_API_HASH", API_HASH)
    text = set_key(text, "TG_SESSION", string)
    with open(ENV, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(ENV, 0o600)

    print(f"RESULT=OK session_len={len(string)} env={ENV}")


if __name__ == "__main__":
    asyncio.run(main())
