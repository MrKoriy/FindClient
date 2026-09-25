"""SQLite database for scrape history and deduplication."""

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
"""


class Database:
    """Async SQLite wrapper for scrape history and dedup."""

    def __init__(self, path: str = "scraper.db") -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def get_known_org_ids(self, niche: str) -> set[str]:
        """Return org IDs previously scraped for this niche."""
        assert self._db
        cursor = await self._db.execute(
            """
            SELECT DISTINCT o.org_id
            FROM organizations o
            JOIN scrape_sessions s ON o.session_id = s.id
            WHERE s.niche = ?
            """,
            (niche,),
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}

    async def save_session(
        self, niche: str, organizations: list[dict],
    ) -> int:
        """Save a scrape session and its organizations. Returns session ID."""
        assert self._db
        cursor = await self._db.execute(
            "INSERT INTO scrape_sessions (niche, count) VALUES (?, ?)",
            (niche, len(organizations)),
        )
        session_id = cursor.lastrowid
        assert session_id is not None

        for org in organizations:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO organizations
                    (org_id, session_id, name, phone, email, website, address, rating, socials)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    org["id"],
                    session_id,
                    org.get("name", ""),
                    org.get("phone", ""),
                    org.get("email", ""),
                    org.get("website", ""),
                    org.get("address", ""),
                    org.get("rating", 0.0),
                    org.get("socials", ""),
                ),
            )

        await self._db.commit()
        return session_id

    async def get_history(self, limit: int = 20) -> list[dict]:
        """Return recent scrape sessions."""
        assert self._db
        cursor = await self._db.execute(
            """
            SELECT id, niche, count, created_at
            FROM scrape_sessions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {"id": r[0], "niche": r[1], "count": r[2], "date": r[3]}
            for r in rows
        ]

    async def get_stats(self) -> dict:
        """Return overall collection statistics."""
        assert self._db
        cur = await self._db.execute("SELECT COUNT(*) FROM scrape_sessions")
        total_sessions = (await cur.fetchone())[0]

        cur = await self._db.execute("SELECT COUNT(DISTINCT org_id) FROM organizations")
        total_orgs = (await cur.fetchone())[0]

        cur = await self._db.execute(
            "SELECT COUNT(DISTINCT org_id) FROM organizations WHERE phone != ''"
        )
        with_phone = (await cur.fetchone())[0]

        cur = await self._db.execute(
            "SELECT COUNT(DISTINCT org_id) FROM organizations WHERE email != ''"
        )
        with_email = (await cur.fetchone())[0]

        cur = await self._db.execute(
            "SELECT COUNT(DISTINCT org_id) FROM organizations WHERE website != ''"
        )
        with_website = (await cur.fetchone())[0]

        cur = await self._db.execute(
            """
            SELECT s.niche, COUNT(DISTINCT o.org_id) as cnt
            FROM organizations o
            JOIN scrape_sessions s ON o.session_id = s.id
            GROUP BY s.niche
            ORDER BY cnt DESC
            LIMIT 5
            """
        )
        top_niches = [{"niche": r[0], "count": r[1]} for r in await cur.fetchall()]

        return {
            "total_sessions": total_sessions,
            "total_orgs": total_orgs,
            "with_phone": with_phone,
            "with_email": with_email,
            "with_website": with_website,
            "top_niches": top_niches,
        }
