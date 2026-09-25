"""Application settings loaded from environment variables."""

import os
import re
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
    # Extra Telegram accounts for outreach (TG_SESSION_2, TG_SESSION_3, ...).
    TG_EXTRA_SESSIONS: tuple[str, ...] = ()
    # LLM: DeepSeek via B.AI; Jev (TypeSafe) for typed classification.
    BAI_API_KEY: str = ""
    BAI_MODEL: str = "DeepSeek-V4.1-Flash"
    TYPESAFE_API_KEY: str = ""
    JEV_MODEL: str = "jev-latest"
    # Demo sites web server (no domain yet: http://<VPS_IP>:8080).
    DEMO_BASE_URL: str = ""
    WEB_PORT: int = 8080
    OUTREACH_DAILY_MAX: int = 20
    OUTREACH_WORK_HOURS: str = "10-19"

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
            TG_EXTRA_SESSIONS=tuple(
                v.strip() for k, v in sorted(os.environ.items())
                if re.fullmatch(r"TG_SESSION_\d+", k) and v.strip()
            ),
            BAI_API_KEY=env("BAI_API_KEY", "").strip(),
            BAI_MODEL=env("BAI_MODEL", "DeepSeek-V4.1-Flash").strip() or "DeepSeek-V4.1-Flash",
            TYPESAFE_API_KEY=env("TYPESAFE_API_KEY", "").strip(),
            JEV_MODEL=env("JEV_MODEL", "jev-latest").strip() or "jev-latest",
            DEMO_BASE_URL=env("DEMO_BASE_URL", "").strip(),
            WEB_PORT=int(env("WEB_PORT", "8080") or 8080),
            OUTREACH_DAILY_MAX=int(env("OUTREACH_DAILY_MAX", "20") or 20),
            OUTREACH_WORK_HOURS=env("OUTREACH_WORK_HOURS", "10-19").strip() or "10-19",
        )
