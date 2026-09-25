"""Application settings loaded from environment variables."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _int_set(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


@dataclass
class Settings:
    """Application configuration loaded from .env file."""

    BOT_TOKEN: str
    PAGE_SIZE: int = 50
    REQUEST_DELAY: float = 0.3
    # Telegram user IDs allowed to use the bot. Empty = anyone (not recommended).
    OWNER_IDS: frozenset[int] = field(default_factory=frozenset)
    DB_PATH: str = "scraper.db"
    # Official Yandex "Поиск по организациям" key; without it the keyless web fallback is used.
    YANDEX_API_KEY: str = ""
    # Telethon user account for Telegram chat search/harvest (optional).
    TG_API_ID: int = 0
    TG_API_HASH: str = ""
    TG_SESSION: str = ""
    ORDERS_POLL_INTERVAL: int = 300
    HTTP_PROXY: str = ""

    @property
    def telegram_user_enabled(self) -> bool:
        return bool(self.TG_API_ID and self.TG_API_HASH and self.TG_SESSION)

    @classmethod
    def from_env(cls) -> "Settings":
        """Load settings from environment variables.

        Calls load_dotenv() to read .env file, then reads required
        variables from os.environ. Raises ValueError if any required
        variable is missing.
        """
        load_dotenv()

        bot_token = os.environ.get("BOT_TOKEN")
        if not bot_token:
            raise ValueError(
                "BOT_TOKEN is not set. Add it to your .env file."
            )

        env = os.environ.get
        api_id = env("TG_API_ID", "").strip()
        return cls(
            BOT_TOKEN=bot_token,
            REQUEST_DELAY=float(env("REQUEST_DELAY", "0.3") or 0.3),
            OWNER_IDS=_int_set(env("OWNER_IDS", "")),
            DB_PATH=env("DB_PATH", "scraper.db") or "scraper.db",
            YANDEX_API_KEY=env("YANDEX_API_KEY", "").strip(),
            TG_API_ID=int(api_id) if api_id.isdigit() else 0,
            TG_API_HASH=env("TG_API_HASH", "").strip(),
            TG_SESSION=env("TG_SESSION", "").strip(),
            ORDERS_POLL_INTERVAL=int(env("ORDERS_POLL_INTERVAL", "300") or 300),
            HTTP_PROXY=env("HTTP_PROXY", "").strip(),
        )
