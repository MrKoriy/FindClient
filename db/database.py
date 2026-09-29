"""SQLite storage: scrape history/dedup, freelance orders, subscriptions, Telegram leads."""

import json

import aiosqlite

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS scrape_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    niche       TEXT    NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS organizations (
    org_id      TEXT NOT NULL,
    session_id  INTEGER NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL DEFAULT '',
    website     TEXT NOT NULL DEFAULT '',
    address     TEXT NOT NULL DEFAULT '',
    rating      REAL NOT NULL DEFAULT 0.0,
    socials     TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (session_id) REFERENCES scrape_sessions(id),
    PRIMARY KEY (org_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_org_id ON organizations(org_id);
CREATE INDEX IF NOT EXISTS idx_session_niche ON scrape_sessions(niche);
CREATE INDEX IF NOT EXISTS idx_org_phone ON organizations(phone);

CREATE TABLE IF NOT EXISTS seen_orders (
    uid         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    budget      TEXT NOT NULL DEFAULT '',
    matched     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_seen_matched ON seen_orders(matched);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id     INTEGER NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);

CREATE TABLE IF NOT EXISTS tg_leads (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    niche       TEXT NOT NULL DEFAULT '',
    chats       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Columns added after the first release; applied with ALTER TABLE on old databases.
_MIGRATIONS = {
    "scrape_sessions": [
        ("city", "TEXT NOT NULL DEFAULT ''"),
        ("sources", "TEXT NOT NULL DEFAULT ''"),
        ("filters", "TEXT NOT NULL DEFAULT ''"),
    ],
    "organizations": [
        ("source", "TEXT NOT NULL DEFAULT '2gis'"),
        ("city", "TEXT NOT NULL DEFAULT ''"),
        ("category", "TEXT NOT NULL DEFAULT ''"),
        ("reviews", "INTEGER NOT NULL DEFAULT 0"),
        ("branches", "INTEGER NOT NULL DEFAULT 0"),
        ("url", "TEXT NOT NULL DEFAULT ''"),
        ("score", "INTEGER NOT NULL DEFAULT 0"),
        ("director", "TEXT NOT NULL DEFAULT ''"),
        ("inn", "TEXT NOT NULL DEFAULT ''"),
    ],
}

_ORG_FIELDS = (
    "name", "phone", "email", "website", "address", "rating", "socials",
    "source", "city", "category", "reviews", "branches", "url", "score",
    "director", "inn",
)
_ORG_DEFAULTS = {"rating": 0.0, "reviews": 0, "branches": 0, "score": 0, "source": "2gis"}


class Database:
    """Async SQLite wrapper."""

    def __init__(self, path: str = "scraper.db") -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        """Гард вместо assert: под `python -O` assert испаряется, и «не
        подключено» превращается в голый AttributeError вместо понятной ошибки."""
        if self._db is None:
            raise RuntimeError("база не подключена: сначала await connect()")
        return self._db

    async def connect(self) -> None:
        db = await aiosqlite.connect(self.path)
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.executescript(_SCHEMA)
            self._db = db
            await self._migrate()
            await self._conn.commit()
        except Exception:
            await db.close()
            raise

    async def _migrate(self) -> None:
        db = self._conn
        for table, columns in _MIGRATIONS.items():
            cur = await db.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in await cur.fetchall()}
            for name, ddl in columns:
                if name not in existing:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_niche_city ON scrape_sessions(niche, city)"
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_org_phone ON organizations(phone)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_seen_matched ON seen_orders(matched)")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # Organizations
    # ------------------------------------------------------------------

    async def get_known_org_ids(self, niche: str, city: str | None = None) -> set[str]:
        """Return org IDs previously scraped for this niche (optionally in a city)."""
        sql = """
            SELECT DISTINCT o.org_id
            FROM organizations o
            JOIN scrape_sessions s ON o.session_id = s.id
            WHERE s.niche = ?
        """
        params: tuple = (niche,)
        if city is not None:
            sql += " AND s.city = ?"
            params = (niche, city)
        cursor = await self._conn.execute(sql, params)
        return {row[0] for row in await cursor.fetchall()}

    async def get_known_phone_keys(self, niche: str, city: str | None = None) -> set[str]:
        """Normalised phones already collected — catches the same company from another source."""
        sql = """
            SELECT DISTINCT o.phone FROM organizations o
            JOIN scrape_sessions s ON o.session_id = s.id
            WHERE s.niche = ? AND o.phone != ''
        """
        params: tuple = (niche,)
        if city is not None:
            sql += " AND s.city = ?"
            params = (niche, city)
        cur = await self._conn.execute(sql, params)
        keys = set()
        for (phone,) in await cur.fetchall():
            for part in phone.split(","):
                digits = "".join(ch for ch in part if ch.isdigit())
                if len(digits) >= 10:
                    keys.add(digits[-10:])
        return keys

    async def save_session(
        self,
        niche: str,
        organizations: list[dict],
        city: str = "",
        sources: str = "",
        filters: str = "",
    ) -> int:
        """Save a scrape session and its organizations. Returns session ID."""
        cursor = await self._conn.execute(
            "INSERT INTO scrape_sessions (niche, count, city, sources, filters) VALUES (?, ?, ?, ?, ?)",
            (niche, len(organizations), city, sources, filters),
        )
        session_id = cursor.lastrowid
        assert session_id is not None

        cols = ", ".join(_ORG_FIELDS)
        marks = ", ".join("?" for _ in _ORG_FIELDS)
        await self._conn.executemany(
            f"INSERT OR IGNORE INTO organizations (org_id, session_id, {cols}) "
            f"VALUES (?, ?, {marks})",
            [
                (org["id"], session_id, *[org.get(f, _ORG_DEFAULTS.get(f, "")) for f in _ORG_FIELDS])
                for org in organizations
            ],
        )

        await self._conn.commit()
        return session_id

    async def get_session_orgs(self, session_id: int) -> list[dict]:
        """Return organizations of a past session (for re-download)."""
        cols = ", ".join(_ORG_FIELDS)
        cur = await self._conn.execute(
            f"SELECT org_id, {cols} FROM organizations WHERE session_id = ?", (session_id,)
        )
        rows = await cur.fetchall()
        return [dict(zip(("id", *_ORG_FIELDS), r, strict=True)) for r in rows]

    async def get_history(self, limit: int = 20) -> list[dict]:
        """Return recent scrape sessions."""
        cursor = await self._conn.execute(
            """
            SELECT id, niche, count, created_at, city, sources
            FROM scrape_sessions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {"id": r[0], "niche": r[1], "count": r[2], "date": r[3], "city": r[4], "sources": r[5]}
            for r in rows
        ]

    async def get_stats(self) -> dict:
        """Return overall collection statistics."""

        async def scalar(sql: str) -> int:
            cur = await self._conn.execute(sql)
            return (await cur.fetchone())[0]

        top = await self._conn.execute(
            """
            SELECT s.niche, COUNT(DISTINCT o.org_id) as cnt
            FROM organizations o
            JOIN scrape_sessions s ON o.session_id = s.id
            GROUP BY s.niche
            ORDER BY cnt DESC
            LIMIT 5
            """
        )
        top_niches = [{"niche": r[0], "count": r[1]} for r in await top.fetchall()]

        return {
            "total_sessions": await scalar("SELECT COUNT(*) FROM scrape_sessions"),
            "total_orgs": await scalar("SELECT COUNT(DISTINCT org_id) FROM organizations"),
            "with_phone": await scalar(
                "SELECT COUNT(DISTINCT org_id) FROM organizations WHERE phone != ''"
            ),
            "with_email": await scalar(
                "SELECT COUNT(DISTINCT org_id) FROM organizations WHERE email != ''"
            ),
            "with_website": await scalar(
                "SELECT COUNT(DISTINCT org_id) FROM organizations WHERE website != ''"
            ),
            "without_website": await scalar(
                "SELECT COUNT(DISTINCT org_id) FROM organizations WHERE website = ''"
            ),
            "orders_seen": await scalar("SELECT COUNT(*) FROM seen_orders"),
            "orders_matched": await scalar("SELECT COUNT(*) FROM seen_orders WHERE matched = 1"),
            "tg_leads": await scalar("SELECT COUNT(*) FROM tg_leads"),
            "top_niches": top_niches,
        }

    # ------------------------------------------------------------------
    # Freelance orders
    # ------------------------------------------------------------------

    async def filter_new_order_uids(self, uids: list[str]) -> set[str]:
        """Return the subset of uids that have not been seen yet."""
        if not uids:
            return set()
        marks = ",".join("?" for _ in uids)
        cur = await self._conn.execute(f"SELECT uid FROM seen_orders WHERE uid IN ({marks})", uids)
        seen = {r[0] for r in await cur.fetchall()}
        return set(uids) - seen

    async def mark_orders_seen(self, orders: list[dict]) -> None:
        await self._conn.executemany(
            "INSERT OR IGNORE INTO seen_orders (uid, source, title, url, budget, matched) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (o["uid"], o["source"], o.get("title", ""), o.get("url", ""),
                 o.get("budget", ""), int(bool(o.get("matched"))))
                for o in orders
            ],
        )
        await self._conn.commit()

    async def count_orders_by_source(self) -> dict[str, int]:
        cur = await self._conn.execute("SELECT source, COUNT(*) FROM seen_orders GROUP BY source")
        return {r[0]: r[1] for r in await cur.fetchall()}

    async def recent_matched_orders(self, limit: int = 200) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT source, title, budget, created_at, url FROM seen_orders "
            "WHERE matched = 1 ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [
            {"source": r[0], "title": r[1], "budget": r[2], "published": r[3], "url": r[4]}
            for r in await cur.fetchall()
        ]

    # ------------------------------------------------------------------
    # Per-chat settings (JSON values)
    # ------------------------------------------------------------------

    async def get_setting(self, chat_id: int, key: str, default=None):
        cur = await self._conn.execute(
            "SELECT value FROM chat_settings WHERE chat_id = ? AND key = ?", (chat_id, key)
        )
        row = await cur.fetchone()
        return json.loads(row[0]) if row else default

    async def get_many_settings(self, chat_id: int, keys: list[str]) -> dict:
        """Несколько настроек чата одним запросом (вместо N одиночных)."""
        if not keys:
            return {}
        marks = ",".join("?" for _ in keys)
        cur = await self._conn.execute(
            f"SELECT key, value FROM chat_settings WHERE chat_id = ? AND key IN ({marks})",
            (chat_id, *keys),
        )
        return {k: json.loads(v) for k, v in await cur.fetchall()}

    async def set_setting(self, chat_id: int, key: str, value) -> None:
        await self._conn.execute(
            "INSERT INTO chat_settings (chat_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, key) DO UPDATE SET value = excluded.value",
            (chat_id, key, json.dumps(value, ensure_ascii=False)),
        )
        await self._conn.commit()

    async def all_values(self, key: str) -> list:
        """Values of a setting across all chats."""
        cur = await self._conn.execute("SELECT value FROM chat_settings WHERE key = ?", (key,))
        return [json.loads(r[0]) for r in await cur.fetchall()]

    async def chats_with_setting(self, key: str, value) -> list[int]:
        cur = await self._conn.execute(
            "SELECT chat_id FROM chat_settings WHERE key = ? AND value = ?",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        return [r[0] for r in await cur.fetchall()]

    # ------------------------------------------------------------------
    # Telegram leads
    # ------------------------------------------------------------------

    async def get_known_tg_user_ids(self) -> set[int]:
        cur = await self._conn.execute("SELECT user_id FROM tg_leads")
        return {r[0] for r in await cur.fetchall()}

    async def save_tg_leads(self, leads: list[dict], niche: str = "") -> None:
        await self._conn.executemany(
            "INSERT OR IGNORE INTO tg_leads (user_id, username, name, niche, chats) VALUES (?, ?, ?, ?, ?)",
            [
                (lead["user_id"], lead.get("username", ""), lead.get("name", ""), niche,
                 ", ".join(sorted(lead.get("chats", []))))
                for lead in leads
            ],
        )
        await self._conn.commit()
