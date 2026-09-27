"""Хранилище CRM: цели, шаблоны, журнал отправок, группы.

Отдельная база (`crm.db`), а не `scraper.db`, по двум причинам:

1. Бот держит свою базу и делает автомиграции при старте. Если CRM начнёт
   писать в ту же базу, любая ошибка в схеме ломает бота, а не панель.
2. CRM нужна для истории «кому что отправлено» — это данные, которые дороже
   любой выгрузки, и их лучше изолировать.

Цели импортируются из таблицы `organizations` основной базы: это компании с
карт. Для рассылки годятся те, у кого нет сайта — им и продаём сайт.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Iterable

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
# выдерживает 10–15 холодных сообщений в день, прогретый — 30–40.
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
}


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DEFAULT_CRM_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | None = None) -> None:
    """Создаёт схему и дописывает недостающие настройки."""
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )


def get_settings(path: str | None = None) -> dict[str, str]:
    with connect(path) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULT_SETTINGS)
    out.update({r["key"]: r["value"] for r in rows})
    return out


def set_settings(values: dict[str, Any], path: str | None = None) -> None:
    with connect(path) as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


def log_event(kind: str, text: str, path: str | None = None) -> None:
    with connect(path) as conn:
        conn.execute("INSERT INTO events (kind, text) VALUES (?, ?)", (kind, text))


# --------------------------------------------------------------------------
# Цели
# --------------------------------------------------------------------------

def import_targets(scraper_db: str | None = None, crm_db: str | None = None) -> int:
    """Подтягивает компании с карт в цели.

    Берём только тех, у кого **нет сайта**: им и продаём сайт. Компании с
    сайтом попадают в базу как `skip` — чтобы не выпадали из виду совсем,
    но и в очередь не лезли.
    """
    scraper_db = scraper_db or DEFAULT_SCRAPER_DB
    if not os.path.exists(scraper_db):
        raise FileNotFoundError(f"нет основной базы: {scraper_db}")

    src = sqlite3.connect(scraper_db)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT org_id, session_id, name, phone, email, website, socials, address,"
        "       city, category, rating, reviews, source FROM organizations"
    ).fetchall()
    src.close()

    added = 0
    with connect(crm_db) as conn:
        for r in rows:
            org_key = f"{r['org_id']}|{r['session_id']}"
            has_site = bool((r["website"] or "").strip())
            status = "skip" if has_site else "new"
            cur = conn.execute(
                "INSERT OR IGNORE INTO targets"
                " (org_key, name, phone, email, website, socials, address, city,"
                "  category, rating, reviews, source, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    org_key, r["name"] or "", r["phone"] or "", r["email"] or "",
                    r["website"] or "", r["socials"] or "", r["address"] or "",
                    r["city"] or "", r["category"] or "", r["rating"] or 0,
                    r["reviews"] or 0, r["source"] or "", status,
                ),
            )
            added += cur.rowcount
    return added


def import_groups(crm_db: str | None = None, niches_module=None) -> int:
    """Заливает проверенный каталог чатов из data/niches.py в таблицу groups."""
    if niches_module is None:
        from data.niches import NICHES as niches_module

    added = 0
    with connect(crm_db) as conn:
        for n in niches_module:
            for chat in n.tg_chats:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO groups (username, niche) VALUES (?, ?)",
                    (chat, n.label),
                )
                added += cur.rowcount
    return added


def list_targets(
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
    with connect(crm_db) as conn:
        rows = conn.execute(
            f"SELECT * FROM targets {clause} ORDER BY reviews DESC, rating DESC"
            f" LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def count_targets(crm_db: str | None = None) -> dict[str, int]:
    with connect(crm_db) as conn:
        rows = conn.execute(
            "SELECT status, count(*) AS n FROM targets GROUP BY status"
        ).fetchall()
    out = {s: 0 for s in TARGET_STATUSES}
    for r in rows:
        out[r["status"]] = r["n"]
    out["all"] = sum(out.values())
    return out


def get_target(target_id: int, crm_db: str | None = None) -> dict | None:
    with connect(crm_db) as conn:
        row = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
    return dict(row) if row else None


def set_target_status(
    target_id: int, status: str, note: str | None = None, crm_db: str | None = None
) -> None:
    if status not in TARGET_STATUSES:
        raise ValueError(f"неизвестный статус: {status}")
    with connect(crm_db) as conn:
        if note is None:
            conn.execute(
                "UPDATE targets SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, target_id),
            )
        else:
            conn.execute(
                "UPDATE targets SET status = ?, note = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (status, note, target_id),
            )


def save_target_username(target_id: int, username: str, crm_db: str | None = None) -> None:
    with connect(crm_db) as conn:
        conn.execute(
            "UPDATE targets SET username = ?, updated_at = datetime('now') WHERE id = ?",
            (username.lstrip("@"), target_id),
        )


# --------------------------------------------------------------------------
# Шаблоны
# --------------------------------------------------------------------------

def list_templates(crm_db: str | None = None) -> list[dict]:
    with connect(crm_db) as conn:
        rows = conn.execute("SELECT * FROM templates ORDER BY category, name").fetchall()
    return [dict(r) for r in rows]


def save_template(
    name: str, category: str, body: str, template_id: int | None = None,
    crm_db: str | None = None,
) -> int:
    with connect(crm_db) as conn:
        if template_id:
            conn.execute(
                "UPDATE templates SET name = ?, category = ?, body = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (name, category, body, template_id),
            )
            return template_id
        cur = conn.execute(
            "INSERT INTO templates (name, category, body) VALUES (?, ?, ?)",
            (name, category, body),
        )
        return int(cur.lastrowid)


def delete_template(template_id: int, crm_db: str | None = None) -> None:
    with connect(crm_db) as conn:
        conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))


# --------------------------------------------------------------------------
# Очередь и журнал
# --------------------------------------------------------------------------

def queue_message(
    target_id: int | None,
    chat: str,
    body: str,
    kind: str = "dm",
    crm_db: str | None = None,
) -> int:
    with connect(crm_db) as conn:
        cur = conn.execute(
            "INSERT INTO messages (target_id, chat, kind, body, status)"
            " VALUES (?, ?, ?, ?, 'queued')",
            (target_id, chat.lstrip("@"), kind, body),
        )
        if target_id:
            conn.execute(
                "UPDATE targets SET status = 'queued', updated_at = datetime('now')"
                " WHERE id = ? AND status = 'new'",
                (target_id,),
            )
        return int(cur.lastrowid)


def next_queued(crm_db: str | None = None) -> dict | None:
    with connect(crm_db) as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def mark_message(
    message_id: int,
    status: str,
    error: str = "",
    tg_id: int | None = None,
    crm_db: str | None = None,
) -> None:
    with connect(crm_db) as conn:
        conn.execute(
            "UPDATE messages SET status = ?, error = ?, tg_id = ?,"
            " sent_at = CASE WHEN ? = 'sent' THEN datetime('now') ELSE sent_at END"
            " WHERE id = ?",
            (status, error[:500], tg_id, status, message_id),
        )
        if status == "sent":
            row = conn.execute(
                "SELECT target_id FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row and row["target_id"]:
                conn.execute(
                    "UPDATE targets SET status = 'sent', updated_at = datetime('now')"
                    " WHERE id = ?",
                    (row["target_id"],),
                )


def sent_today(crm_db: str | None = None, tz_offset: int = 3) -> int:
    """Сколько отправлено за сегодня в местном времени."""
    modifier = f"{tz_offset:+d} hours"
    with connect(crm_db) as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM messages"
            " WHERE status = 'sent' AND date(sent_at, ?) = date('now', ?)",
            (modifier, modifier),
        ).fetchone()
    return row["n"]


def list_messages(limit: int = 100, crm_db: str | None = None) -> list[dict]:
    with connect(crm_db) as conn:
        rows = conn.execute(
            "SELECT m.*, t.name AS target_name FROM messages m"
            " LEFT JOIN targets t ON t.id = m.target_id"
            " ORDER BY m.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def summary(crm_db: str | None = None) -> dict:
    settings = get_settings(crm_db)
    tz = int(settings.get("timezone_offset", "3"))
    cap = int(settings.get("daily_cap", "10"))
    today = sent_today(crm_db, tz)
    counts = count_targets(crm_db)
    with connect(crm_db) as conn:
        queued = conn.execute(
            "SELECT count(*) AS n FROM messages WHERE status = 'queued'"
        ).fetchone()["n"]
        failed = conn.execute(
            "SELECT count(*) AS n FROM messages WHERE status = 'failed'"
        ).fetchone()["n"]
        groups_total = conn.execute("SELECT count(*) AS n FROM groups").fetchone()["n"]
        groups_posted = conn.execute(
            "SELECT count(*) AS n FROM groups WHERE posted_at IS NOT NULL"
        ).fetchone()["n"]
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

def list_groups(
    niche: str | None = None, only_unposted: bool = False, crm_db: str | None = None
) -> list[dict]:
    where, args = [], []
    if niche:
        where.append("niche = ?")
        args.append(niche)
    if only_unposted:
        where.append("posted_at IS NULL")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with connect(crm_db) as conn:
        rows = conn.execute(
            f"SELECT * FROM groups {clause} ORDER BY members DESC", args
        ).fetchall()
    return [dict(r) for r in rows]


def mark_group_posted(
    group_id: int, note: str = "", crm_db: str | None = None
) -> None:
    with connect(crm_db) as conn:
        conn.execute(
            "UPDATE groups SET posted_at = datetime('now'), posts = posts + 1,"
            " note = ? WHERE id = ?",
            (note, group_id),
        )


def update_group_meta(
    username: str, title: str, members: int, crm_db: str | None = None
) -> None:
    with connect(crm_db) as conn:
        conn.execute(
            "UPDATE groups SET title = ?, members = ? WHERE username = ?",
            (title, members, username),
        )


def niche_options(crm_db: str | None = None) -> list[str]:
    with connect(crm_db) as conn:
        rows = conn.execute(
            "SELECT DISTINCT niche FROM groups WHERE niche != '' ORDER BY niche"
        ).fetchall()
    return [r["niche"] for r in rows]
