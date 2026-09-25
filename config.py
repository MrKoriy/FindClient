"""Application settings loaded from environment variables."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class Settings:
    """Application configuration loaded from .env file."""

    BOT_TOKEN: str
    PAGE_SIZE: int = 50
    REQUEST_DELAY: float = 0.3

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

        return cls(BOT_TOKEN=bot_token)
