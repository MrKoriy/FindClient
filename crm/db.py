"""Хранилище CRM: цели, шаблоны, журнал отправок, группы.

Отдельная база (`crm.db`), а не `scraper.db`, по двум причинам:

1. Бот держит свою базу и делает автомиграции при старте. Если CRM начнёт
   писать в ту же базу, любая ошибка в схеме ломает бота, а не панель.
2. CRM нужна для истории «кому что отправлено» - это данные, которые дороже
   любой выгрузки, и их лучше изолировать.

Цели импортируются из таблицы `organizations` основной базы: это компании с
карт. Для рассылки годятся те, у кого нет сайта - им и продаём сайт.

Все функции асинхронные (aiosqlite): панель и воркер зовут их из
событийного цикла, и дисковый ввод-вывод его не блокирует.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager, suppress
from typing import Any

import aiosqlite

DEFAULT_CRM_DB = os.environ.get("CRM_DB", "crm.db")
DEFAULT_SCRAPER_DB = os.environ.get("DB_PATH", "scraper.db")

# Статусы цели. Порядок важен: по нему строится сводка.
TARGET_STATUSES = (
    "new",          # ещё не писали
    "queued",       # в очереди на отправку
    "sent",         # отправлено, ответа нет
    "replied",      # ответил
    "refused",      # отказался
    "blocked",      # заблокировал / пожаловался
    "skip",         # не наш случай (крупная сеть, франшиза, уже есть сайт)
)

STATUS_LABELS = {
    "new": "Не писали",
    "queued": "В очереди",
    "sent": "Отправлено",
    "replied": "Ответил",
    "refused": "Отказ",
    "blocked": "Блок / жалоба",
    "skip": "Не наш случай",
}

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS targets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_key     TEXT    NOT NULL DEFAULT '',   -- org_id|session_id из organizations
    name        TEXT    NOT NULL DEFAULT '',
    username    TEXT    NOT NULL DEFAULT '',   -- @юзернейм, если известен
    phone       TEXT    NOT NULL DEFAULT '',
    email       TEXT    NOT NULL DEFAULT '',
    website     TEXT    NOT NULL DEFAULT '',
    socials     TEXT    NOT NULL DEFAULT '',
    address     TEXT    NOT NULL DEFAULT '',
    city        TEXT    NOT NULL DEFAULT '',
    category    TEXT    NOT NULL DEFAULT '',
    rating      REAL    NOT NULL DEFAULT 0,
    reviews     INTEGER NOT NULL DEFAULT 0,
    source      TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'new',
    note        TEXT    NOT NULL DEFAULT '',
    director    TEXT    NOT NULL DEFAULT '',   -- ЛПР из ЕГРЮЛ: «ФИО (должность)»
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_key)
);

CREATE INDEX IF NOT EXISTS idx_targets_status ON targets (status);
CREATE INDEX IF NOT EXISTS idx_targets_city   ON targets (city);

CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL DEFAULT '',
    category    TEXT    NOT NULL DEFAULT '',   -- пусто = подходит всем
    body        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id   INTEGER,
    chat        TEXT    NOT NULL DEFAULT '',   -- @юзернейм или группа
    kind        TEXT    NOT NULL DEFAULT 'dm', -- dm | group
    body        TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'queued', -- queued|sent|failed|skipped
    error       TEXT    NOT NULL DEFAULT '',
    tg_id       INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    sent_at     TEXT,
    FOREIGN KEY (target_id) REFERENCES targets (id)
);

CREATE INDEX IF NOT EXISTS idx_messages_status ON messages (status);
CREATE INDEX IF NOT EXISTS idx_messages_sent   ON messages (sent_at);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL,
    niche       TEXT    NOT NULL DEFAULT '',
    title       TEXT    NOT NULL DEFAULT '',
    members     INTEGER NOT NULL DEFAULT 0,
    posted_at   TEXT,
    posts       INTEGER NOT NULL DEFAULT 0,
    note        TEXT    NOT NULL DEFAULT '',
    UNIQUE (username)
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL DEFAULT '',
    text        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# Настройки по умолчанию. Лимиты взяты не с потолка: свежий аккаунт
# выдерживает 10-15 холодных сообщений в день, прогретый - 30-40.
# Аккаунт Леонида уже помечен, поэтому стартуем с нижней границы.
DEFAULT_SETTINGS = {
    "daily_cap": "10",            # сообщений в день
    "min_delay": "90",            # пауза между сообщениями, сек
    "max_delay": "240",           # верхняя граница случайной паузы, сек
    "cooldown_every": "8",        # после скольких сообщений длинная пауза
    "cooldown_seconds": "1200",   # длинная пауза, сек (20 мин)
    "work_from": "10",            # рабочие часы, с
    "work_to": "20",              # рабочие часы, по
    "enabled": "0",               # отправка выключена, пока не включат руками
    "dry_run": "1",               # по умолчанию только показывать, не отправлять
    "first_message_no_link": "1", # ссылка не в первом сообщении
    "timezone_offset": "3",       # UTC+3
    "typesafe_api_key": "",       # ключ TypeSafe Jev API
    "typesafe_model": "jev-latest", # модель классификатора
    "bai_api_key": "",            # ключ B.AI API
    "bai_base_url": "https://api.b.ai/v1", # адрес шлюза B.AI
    "bai_model": "qwen3.8-flash", # модель генератора (Qwen 3.8 Flash / DeepSeek)
    "enable_humanizer": "1",      # включен скилл Humanizer
    "enable_russian_outreach": "1", # включен скилл Russian Outreach
}


@asynccontextmanager
async def connect(path: str | None = None):
    """Соединение с транзакцией и гарантированным закрытием.

    В отличие от `sqlite3`, у aiosqlite выход из `async with conn` закрывает
    соединение, но не коммитит - поэтому коммит делаем явно, а откат - на
    исключении. Семантика повторяет прежний `with sqlite3.connect(...)`.
    """
    conn = await aiosqlite.connect(path or DEFAULT_CRM_DB, timeout=30)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        await conn.commit()
    except BaseException:
        with suppress(Exception):
            await conn.rollback()
        raise
    finally:
        await conn.close()


async def init_db(path: str | None = None) -> None:
    """Создаёт схему, дописывает недостающие настройки, докатывает колонки."""
    async with connect(path) as conn:
        await conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            await conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        await _migrate(conn)


# Добавление колонок к уже существующим таблицам. CREATE TABLE IF NOT EXISTS
# на живой базе молча ничего не делает, и первая же запись в новую колонку
# падает «no such column». Здесь тот же приём, что у бота в db/database.py.
_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "targets": [
        ("director", "TEXT NOT NULL DEFAULT ''"),
    ],
}


async def _migrate(conn: aiosqlite.Connection) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        cur = await conn.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cur.fetchall()}
        for name, decl in columns:
            if name not in existing:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


async def get_settings(path: str | None = None) -> dict[str, str]:
    async with connect(path) as conn:
        cur = await conn.execute("SELECT key, value FROM settings")
        rows = await cur.fetchall()
    out = dict(DEFAULT_SETTINGS)
    out.update({r["key"]: r["value"] for r in rows})
    return out


async def set_settings(values: dict[str, Any], path: str | None = None) -> None:
    async with connect(path) as conn:
        for key, value in values.items():
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


async def log_event(kind: str, text: str, path: str | None = None) -> None:
    async with connect(path) as conn:
        await conn.execute("INSERT INTO events (kind, text) VALUES (?, ?)", (kind, text))


# --------------------------------------------------------------------------
# Цели
# --------------------------------------------------------------------------

def _read_organizations(scraper_db: str) -> list[sqlite3.Row]:
    """Синхронное чтение каталога компаний (его выполняем в отдельном потоке).

    Колонка director появилась позже: если бот ещё ни разу не запускался
    после апгрейда, её может не быть - читаем без неё.
    """
    src = sqlite3.connect(scraper_db)
    src.row_factory = sqlite3.Row
    cur = src.execute("PRAGMA table_info(organizations)")
    has_director = any(r[1] == "director" for r in cur.fetchall())
    select = (
        "SELECT org_id, session_id, name, phone, email, website, socials, address,"
        " city, category, rating, reviews, source"
    )
    if has_director:
        select += ", director"
    rows = src.execute(f"{select} FROM organizations").fetchall()
    src.close()
    return rows


async def import_targets(scraper_db: str | None = None, crm_db: str | None = None) -> int:
    """Подтягивает компании с карт в цели.

    Берём только тех, у кого **нет сайта**: им и продаём сайт. Компании с
    сайтом попадают в базу как `skip` - чтобы не выпадали из виду совсем,
    но и в очередь не лезли.
    """
    scraper_db = scraper_db or DEFAULT_SCRAPER_DB
    if not os.path.exists(scraper_db):
        raise FileNotFoundError(f"нет основной базы: {scraper_db}")

    rows = await asyncio.to_thread(_read_organizations, scraper_db)

    added = 0
    async with connect(crm_db) as conn:
        for r in rows:
            org_key = f"{r['org_id']}|{r['session_id']}"
            has_site = bool((r["website"] or "").strip())
            status = "skip" if has_site else "new"
            cur = await conn.execute(
                "INSERT OR IGNORE INTO targets"
                " (org_key, name, phone, email, website, socials, address, city,"
                "  category, rating, reviews, source, status, director)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    org_key, r["name"] or "", r["phone"] or "", r["email"] or "",
                    r["website"] or "", r["socials"] or "", r["address"] or "",
                    r["city"] or "", r["category"] or "", r["rating"] or 0,
                    r["reviews"] or 0, r["source"] or "", status,
                    (r["director"] if "director" in r.keys() else "") or "",
                ),
            )
            added += cur.rowcount
    return added


async def seed_demo_targets(crm_db: str | None = None) -> int:
    """Засевает реалистичные цели без сайта для тестирования CRM и генератора офферов."""
    demos = [
        ("demo_1", "Клиника «ДентаЛайн»", "+79991112233", "Москва, ул. Тверская, 12",
         "Москва", "стоматология", 4.9, 84, "dentaline_msk"),
        ("demo_2", "Юридическая группа «Правовед»", "+78122223344",
         "Санкт-Петербург, Невский пр., 45", "Санкт-Петербург", "юридические услуги", 4.8, 62, "pravoved_spb"),
        ("demo_3", "Автосервис «Моторс Про»", "+74953334455", "Москва, Варшавское ш., 88",
         "Москва", "авторемонт", 4.7, 115, "motorspro_ru"),
        ("demo_4", "Студия красоты «Элеганс»", "+79164445566", "Казань, ул. Баумана, 21",
         "Казань", "салон красоты", 5.0, 93, "elegance_beauty"),
        ("demo_5", "Ремонт квартир «СтройКом»", "+78125556677",
         "Санкт-Петербург, пр. Просвещения, 30", "Санкт-Петербург", "ремонт квартир", 4.6, 38, "stroykom_remont"),
    ]
    added = 0
    async with connect(crm_db) as conn:
        for org_key, name, phone, addr, city, cat, rating, reviews, username in demos:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO targets (org_key, name, phone, address, city, category,"
                " rating, reviews, username, status) VALUES (?,?,?,?,?,?,?,?,?, 'new')",
                (org_key, name, phone, addr, city, cat, rating, reviews, username)
            )
            added += cur.rowcount
    return added


async def import_groups(crm_db: str | None = None, niches_module=None) -> int:
    """Заливает проверенный каталог чатов из data/niches.py в таблицу groups."""
    if niches_module is None:
        from data.niches import NICHES as niches_module

    added = 0
    async with connect(crm_db) as conn:
        for n in niches_module:
            for chat in n.tg_chats:
                cur = await conn.execute(
                    "INSERT OR IGNORE INTO groups (username, niche) VALUES (?, ?)",
                    (chat, n.label),
                )
                added += cur.rowcount
    return added


async def list_targets(
    status: str | None = None,
    city: str | None = None,
    search: str | None = None,
    limit: int = 200,
    offset: int = 0,
    crm_db: str | None = None,
) -> list[dict]:
    where, args = [], []
    if status and status != "all":
        where.append("status = ?")
        args.append(status)
    if city:
        where.append("city = ?")
        args.append(city)
    if search:
        where.append(
            "(name LIKE ? OR phone LIKE ? OR address LIKE ? OR socials LIKE ?"
            " OR category LIKE ? OR email LIKE ?)"
        )
        args += [f"%{search}%"] * 6
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    # Сначала компании с мессенджером в контактах (t.me/wa.me): это ближайший
    # путь к ЛПР, такие цели дороже остальных.
    order = ("(socials LIKE '%t.me/%' OR socials LIKE '%wa.me/%') DESC,"
             " reviews DESC, rating DESC")
    async with connect(crm_db) as conn:
        cur = await conn.execute(
            f"SELECT * FROM targets {clause} ORDER BY {order}"
            f" LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def count_targets(crm_db: str | None = None) -> dict[str, int]:
    async with connect(crm_db) as conn:
        cur = await conn.execute(
            "SELECT status, count(*) AS n FROM targets GROUP BY status"
        )
        rows = await cur.fetchall()
    out = {s: 0 for s in TARGET_STATUSES}
    for r in rows:
        out[r["status"]] = r["n"]
    out["all"] = sum(out.values())
    return out


async def get_target(target_id: int, crm_db: str | None = None) -> dict | None:
    async with connect(crm_db) as conn:
        cur = await conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,))
        row = await cur.fetchone()
    return dict(row) if row else None


async def set_target_status(
    target_id: int, status: str, note: str | None = None, crm_db: str | None = None
) -> None:
    if status not in TARGET_STATUSES:
        raise ValueError(f"неизвестный статус: {status}")
    async with connect(crm_db) as conn:
        if note is None:
            await conn.execute(
                "UPDATE targets SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, target_id),
            )
        else:
            await conn.execute(
                "UPDATE targets SET status = ?, note = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (status, note, target_id),
            )


async def save_target_username(target_id: int, username: str, crm_db: str | None = None) -> None:
    async with connect(crm_db) as conn:
        await conn.execute(
            "UPDATE targets SET username = ?, updated_at = datetime('now') WHERE id = ?",
            (username.lstrip("@"), target_id),
        )


# --------------------------------------------------------------------------
# Шаблоны
# --------------------------------------------------------------------------

async def list_templates(crm_db: str | None = None) -> list[dict]:
    async with connect(crm_db) as conn:
        cur = await conn.execute("SELECT * FROM templates ORDER BY category, name")
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def save_template(
    name: str, category: str, body: str, template_id: int | None = None,
    crm_db: str | None = None,
) -> int:
    async with connect(crm_db) as conn:
        if template_id:
            await conn.execute(
                "UPDATE templates SET name = ?, category = ?, body = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (name, category, body, template_id),
            )
            return template_id
        cur = await conn.execute(
            "INSERT INTO templates (name, category, body) VALUES (?, ?, ?)",
            (name, category, body),
        )
        return int(cur.lastrowid)


async def delete_template(template_id: int, crm_db: str | None = None) -> None:
    async with connect(crm_db) as conn:
        await conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))


# --------------------------------------------------------------------------
# Очередь и журнал
# --------------------------------------------------------------------------

async def queue_message(
    target_id: int | None,
    chat: str,
    body: str,
    kind: str = "dm",
    crm_db: str | None = None,
) -> int:
    async with connect(crm_db) as conn:
        cur = await conn.execute(
            "INSERT INTO messages (target_id, chat, kind, body, status)"
            " VALUES (?, ?, ?, ?, 'queued')",
            (target_id, chat.lstrip("@"), kind, body),
        )
        if target_id:
            await conn.execute(
                "UPDATE targets SET status = 'queued', updated_at = datetime('now')"
                " WHERE id = ? AND status = 'new'",
                (target_id,),
            )
        return int(cur.lastrowid)


async def next_queued(crm_db: str | None = None) -> dict | None:
    async with connect(crm_db) as conn:
        cur = await conn.execute(
            "SELECT * FROM messages WHERE status = 'queued' ORDER BY id LIMIT 1"
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def mark_message(
    message_id: int,
    status: str,
    error: str = "",
    tg_id: int | None = None,
    crm_db: str | None = None,
) -> None:
    async with connect(crm_db) as conn:
        await conn.execute(
            "UPDATE messages SET status = ?, error = ?, tg_id = ?,"
            " sent_at = CASE WHEN ? = 'sent' THEN datetime('now') ELSE sent_at END"
            " WHERE id = ?",
            (status, error[:500], tg_id, status, message_id),
        )
        if status == "sent":
            cur = await conn.execute(
                "SELECT target_id FROM messages WHERE id = ?", (message_id,)
            )
            row = await cur.fetchone()
            if row and row["target_id"]:
                await conn.execute(
                    "UPDATE targets SET status = 'sent', updated_at = datetime('now')"
                    " WHERE id = ?",
                    (row["target_id"],),
                )


async def sent_today(crm_db: str | None = None, tz_offset: int = 3) -> int:
    """Сколько отправлено за сегодня в местном времени."""
    modifier = f"{tz_offset:+d} hours"
    async with connect(crm_db) as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM messages"
            " WHERE status = 'sent' AND date(sent_at, ?) = date('now', ?)",
            (modifier, modifier),
        )
        row = await cur.fetchone()
    return row["n"]


async def list_messages(limit: int = 100, crm_db: str | None = None) -> list[dict]:
    async with connect(crm_db) as conn:
        cur = await conn.execute(
            "SELECT m.*, t.name AS target_name FROM messages m"
            " LEFT JOIN targets t ON t.id = m.target_id"
            " ORDER BY m.id DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def summary(crm_db: str | None = None) -> dict:
    settings = await get_settings(crm_db)
    tz = int(settings.get("timezone_offset", "3"))
    cap = int(settings.get("daily_cap", "10"))
    today = await sent_today(crm_db, tz)
    counts = await count_targets(crm_db)
    async with connect(crm_db) as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM messages WHERE status = 'queued'"
        )
        queued = (await cur.fetchone())["n"]
        cur = await conn.execute(
            "SELECT count(*) AS n FROM messages WHERE status = 'failed'"
        )
        failed = (await cur.fetchone())["n"]
        cur = await conn.execute("SELECT count(*) AS n FROM groups")
        groups_total = (await cur.fetchone())["n"]
        cur = await conn.execute(
            "SELECT count(*) AS n FROM groups WHERE posted_at IS NOT NULL"
        )
        groups_posted = (await cur.fetchone())["n"]
    return {
        "targets": counts,
        "sent_today": today,
        "daily_cap": cap,
        "left_today": max(0, cap - today),
        "queued": queued,
        "failed": failed,
        "groups_total": groups_total,
        "groups_posted": groups_posted,
        "enabled": settings.get("enabled") == "1",
        "dry_run": settings.get("dry_run") == "1",
    }


# --------------------------------------------------------------------------
# Группы
# --------------------------------------------------------------------------

async def list_groups(
    niche: str | None = None, only_unposted: bool = False, crm_db: str | None = None
) -> list[dict]:
    where, args = [], []
    if niche:
        where.append("niche = ?")
        args.append(niche)
    if only_unposted:
        where.append("posted_at IS NULL")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    async with connect(crm_db) as conn:
        cur = await conn.execute(
            f"SELECT * FROM groups {clause} ORDER BY members DESC", args
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_group_posted(
    group_id: int, note: str = "", crm_db: str | None = None
) -> None:
    async with connect(crm_db) as conn:
        await conn.execute(
            "UPDATE groups SET posted_at = datetime('now'), posts = posts + 1,"
            " note = ? WHERE id = ?",
            (note, group_id),
        )


async def update_group_meta(
    username: str, title: str, members: int, crm_db: str | None = None
) -> None:
    async with connect(crm_db) as conn:
        await conn.execute(
            "UPDATE groups SET title = ?, members = ? WHERE username = ?",
            (title, members, username),
        )


async def niche_options(crm_db: str | None = None) -> list[str]:
    async with connect(crm_db) as conn:
        cur = await conn.execute(
            "SELECT DISTINCT niche FROM groups WHERE niche != '' ORDER BY niche"
        )
        rows = await cur.fetchall()
    return [r["niche"] for r in rows]
