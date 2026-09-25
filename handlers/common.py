"""Shared UI helpers: access control, keyboards, throttled progress messages."""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

CITIES = (
    "Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург", "Казань",
    "Нижний Новгород", "Краснодар", "Самара", "Ростов-на-Дону", "Уфа",
    "Челябинск", "Воронеж", "Пермь", "Сочи",
)


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def grid(items: list[tuple[str, str]], cols: int = 2) -> list[list[tuple[str, str]]]:
    return [items[i:i + cols] for i in range(0, len(items), cols)]


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural: plural(3, "компания", "компании", "компаний") -> "3 компании"."""
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def check(flag: bool) -> str:
    return "✅" if flag else "▫️"


MAIN_MENU = kb([
    [("🔎 Компании с карт (2GIS + Яндекс)", "menu:scrape")],
    [("💰 Денежные ниши", "menu:niches"), ("👷 Telegram-лиды", "menu:tg")],
    [("💼 Заказы с бирж", "menu:orders")],
    [("📨 Рассылки + CRM", "menu:out"), ("🧠 Офферы", "menu:offers")],
    [("🕘 История", "menu:history"), ("📈 Статистика", "menu:stats")],
])

BACK_TO_MENU = [("⬅️ Меню", "menu:home")]


class AccessMiddleware(BaseMiddleware):
    """Personal tool: only OWNER_IDS may use the bot (if the list is configured)."""

    def __init__(self, owner_ids: frozenset[int]) -> None:
        self.owner_ids = owner_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not self.owner_ids or (user and user.id in self.owner_ids):
            return await handler(event, data)
        text = f"⛔ Доступ закрыт. Ваш Telegram ID: {user.id if user else '?'} — добавьте его в OWNER_IDS."
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        return None


class Progress:
    """Edits one status message, at most every `interval` seconds (Telegram rate limits)."""

    def __init__(self, message: Message, prefix: str, interval: float = 2.5) -> None:
        self.message = message
        self.prefix = prefix
        self.interval = interval
        self._last = 0.0
        self._text = ""

    async def __call__(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last < self.interval or text == self._text:
            return
        self._last, self._text = now, text
        try:
            await self.message.edit_text(f"{self.prefix}\n\n⏳ {text}")
        except Exception:
            pass


async def safe_edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None, **kw) -> None:
    """Edit the callback message; fall back to a new message if editing fails."""
    try:
        await callback.message.edit_text(text, reply_markup=markup, **kw)
    except Exception:
        await callback.message.answer(text, reply_markup=markup, **kw)


def document(data: bytes, name: str) -> BufferedInputFile:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
    return BufferedInputFile(data, filename=safe)


def niche_picker_categories(prefix: str, back: str) -> InlineKeyboardMarkup:
    """Category list; callbacks `{prefix}:cat:{i}`, custom text `{prefix}:custom`."""
    from data.niches import CATEGORIES

    items = [(c, f"{prefix}:cat:{i}") for i, c in enumerate(CATEGORIES)]
    return kb(grid(items, 2) + [[("✍️ Своя ниша", f"{prefix}:custom")], [("⬅️ Назад", back)]])


def niche_picker_niches(prefix: str, cat_index: int) -> tuple[str, InlineKeyboardMarkup]:
    from data.niches import CATEGORIES, niches_in

    category = CATEGORIES[cat_index]
    items = [(n.label, f"{prefix}:n:{n.id}") for n in niches_in(category)]
    return category, kb(grid(items, 1) + [[("⬅️ Категории", f"{prefix}:cats")]])
