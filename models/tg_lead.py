"""Telegram chat participant collected as a lead."""

from dataclasses import dataclass, field


@dataclass
class TgLead:
    user_id: int
    username: str = ""
    name: str = ""
    phone: str = ""
    bio: str = ""
    chats: set[str] = field(default_factory=set)
    messages: int = 0
    sample: str = ""
    last_seen: str = ""
    score: int = 0

    @property
    def link(self) -> str:
        return f"https://t.me/{self.username}" if self.username else f"tg://user?id={self.user_id}"
