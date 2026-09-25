"""Outreach CRM storage: campaigns, leads with pipeline status, message log, stop-list, account quotas."""

import json
from datetime import datetime, timedelta, timezone

from db.database import Database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_chat_id INTEGER NOT NULL,
    name          TEXT NOT NULL,
    niche         TEXT NOT NULL DEFAULT '',
    offer         TEXT NOT NULL DEFAULT '',
    variants      TEXT NOT NULL DEFAULT '[]',
    delays        TEXT NOT NULL DEFAULT '[3, 5]',
    use_demo      INTEGER NOT NULL DEFAULT 0,
    personalize   INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'paused',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS crm_leads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  INTEGER NOT NULL,
    source       TEXT NOT NULL DEFAULT '',
    ref_id       TEXT NOT NULL DEFAULT '',
    name         TEXT NOT NULL DEFAULT '',
    company      TEXT NOT NULL DEFAULT '',
    city         TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT '',
    phone        TEXT NOT NULL DEFAULT '',
    tg_username  TEXT NOT NULL DEFAULT '',
    tg_user_id   INTEGER NOT NULL DEFAULT 0,
    account      INTEGER NOT NULL DEFAULT -1,
    variant      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'new',
    step         INTEGER NOT NULL DEFAULT 0,
    next_at      TEXT NOT NULL DEFAULT '',
    demo_url     TEXT NOT NULL DEFAULT '',
    last_label   TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    extra        TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (campaign_id, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_crm_due ON crm_leads(status, next_at);
CREATE INDEX IF NOT EXISTS idx_crm_tg ON crm_leads(tg_user_id);

CREATE TABLE IF NOT EXISTS crm_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL,
    direction   TEXT NOT NULL,
    text        TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_crm_msg_lead ON crm_messages(lead_id);

CREATE TABLE IF NOT EXISTS stoplist (
    key         TEXT PRIMARY KEY,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS account_stats (
    account     INTEGER NOT NULL,
    day         TEXT NOT NULL,
    new_sent    INTEGER NOT NULL DEFAULT 0,
    followups   INTEGER NOT NULL DEFAULT 0,
    resolves    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account, day)
);
"""

# Pipeline: new -> sent (sequence running) -> finished (no reply after last step)
# replied/interested/meeting/won/lost/stopped are set by replies or by hand; failed = could not deliver.
ACTIVE = ("new", "sent")
STATUS_RU = {
    "new": "в очереди", "sent": "написали", "finished": "без ответа", "replied": "ответил",
    "interested": "интерес", "meeting": "созвон", "won": "сделка", "lost": "отказ",
    "stopped": "не писать", "failed": "не доставлено",
}
LEAD_FIELDS = ("id", "campaign_id", "source", "ref_id", "name", "company", "city", "category", "phone",
               "tg_username", "tg_user_id", "account", "variant", "status", "step", "next_at",
               "demo_url", "last_label", "note", "extra")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def stop_keys(tg_username: str = "", tg_user_id: int = 0, phone: str = "") -> list[str]:
    keys = []
    if tg_username:
        keys.append("u:" + tg_username.lower().lstrip("@"))
    if tg_user_id:
        keys.append(f"id:{tg_user_id}")
    digits = "".join(ch for ch in phone.split(",")[0] if ch.isdigit())
    if len(digits) >= 10:
        keys.append("p:" + digits[-10:])
    return keys


class CRM:
    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def _c(self):
        assert self.db._db
        return self.db._db

    async def init(self) -> None:
        await self._c.executescript(_SCHEMA)
        await self._c.commit()

    # ------------------------------------------------------------------
    # Campaigns
    # ------------------------------------------------------------------

    async def create_campaign(
        self, owner_chat_id: int, name: str, niche: str, offer: str, variants: list[dict],
        delays: list[int] | None = None, use_demo: bool = False, personalize: bool = True,
    ) -> int:
        cur = await self._c.execute(
            "INSERT INTO campaigns (owner_chat_id, name, niche, offer, variants, delays, use_demo, personalize) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_chat_id, name, niche, offer, json.dumps(variants, ensure_ascii=False),
             json.dumps(delays if delays is not None else [3, 5]), int(use_demo), int(personalize)),
        )
        await self._c.commit()
        return cur.lastrowid

    async def get_campaign(self, cid: int) -> dict | None:
        cur = await self._c.execute(
            "SELECT id, owner_chat_id, name, niche, offer, variants, delays, use_demo, personalize, status, created_at "
            "FROM campaigns WHERE id = ?", (cid,))
        r = await cur.fetchone()
        if not r:
            return None
        return {"id": r[0], "owner_chat_id": r[1], "name": r[2], "niche": r[3], "offer": r[4],
                "variants": json.loads(r[5]), "delays": json.loads(r[6]), "use_demo": bool(r[7]),
                "personalize": bool(r[8]), "status": r[9], "created_at": r[10]}

    async def list_campaigns(self) -> list[dict]:
        cur = await self._c.execute("SELECT id FROM campaigns ORDER BY id DESC")
        return [await self.get_campaign(r[0]) for r in await cur.fetchall()]

    async def update_campaign(self, cid: int, **fields) -> None:
        allowed = {"name", "niche", "offer", "variants", "delays", "use_demo", "personalize", "status"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(k)
            if k in ("variants", "delays"):
                v = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, bool):
                v = int(v)
            sets.append(f"{k} = ?")
            vals.append(v)
        if sets:
            await self._c.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE id = ?", (*vals, cid))
            await self._c.commit()

    async def campaign_stats(self, cid: int) -> dict:
        cur = await self._c.execute(
            "SELECT status, COUNT(*) FROM crm_leads WHERE campaign_id = ? GROUP BY status", (cid,))
        by_status = {r[0]: r[1] for r in await cur.fetchall()}
        cur = await self._c.execute(
            """SELECT variant,
                      SUM(CASE WHEN step > 0 OR status NOT IN ('new', 'failed') THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status IN ('replied','interested','meeting','won','lost','stopped') THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status IN ('interested','meeting','won') THEN 1 ELSE 0 END)
               FROM crm_leads WHERE campaign_id = ? AND variant != '' GROUP BY variant ORDER BY variant""",
            (cid,))
        variants = [{"variant": r[0], "contacted": r[1] or 0, "replied": r[2] or 0, "positive": r[3] or 0}
                    for r in await cur.fetchall()]
        total = sum(by_status.values())
        contacted = total - by_status.get("new", 0) - by_status.get("failed", 0)
        replied = sum(by_status.get(s, 0) for s in ("replied", "interested", "meeting", "won", "lost", "stopped"))
        return {"total": total, "by_status": by_status, "contacted": contacted, "replied": replied,
                "variants": variants}

    # ------------------------------------------------------------------
    # Leads
    # ------------------------------------------------------------------

    async def add_leads(self, cid: int, leads: list[dict]) -> tuple[int, int]:
        """Insert leads; skip stop-listed and duplicates. Returns (added, skipped)."""
        stopped = await self.stopped_keys()
        added = skipped = 0
        for lead in leads:
            keys = stop_keys(lead.get("tg_username", ""), lead.get("tg_user_id", 0), lead.get("phone", ""))
            if not keys or any(k in stopped for k in keys) or await self._contacted_elsewhere(keys):
                skipped += 1
                continue
            cur = await self._c.execute(
                "INSERT OR IGNORE INTO crm_leads (campaign_id, source, ref_id, name, company, city, category, "
                "phone, tg_username, tg_user_id, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, lead.get("source", ""), lead.get("ref_id") or keys[0], lead.get("name", ""),
                 lead.get("company", ""), lead.get("city", ""), lead.get("category", ""),
                 lead.get("phone", ""), lead.get("tg_username", "").lstrip("@"), lead.get("tg_user_id", 0),
                 json.dumps(lead.get("extra", {}), ensure_ascii=False)),
            )
            if cur.rowcount:
                added += 1
            else:
                skipped += 1
        await self._c.commit()
        return added, skipped

    async def _contacted_elsewhere(self, keys: list[str]) -> bool:
        """Never message the same person from two campaigns."""
        for k in keys:
            kind, val = k.split(":", 1)
            if kind == "u":
                sql, arg = "SELECT 1 FROM crm_leads WHERE lower(tg_username) = ? AND status != 'new' LIMIT 1", val
            elif kind == "id":
                sql, arg = "SELECT 1 FROM crm_leads WHERE tg_user_id = ? AND status != 'new' LIMIT 1", int(val)
            else:
                sql, arg = "SELECT 1 FROM crm_leads WHERE substr(replace(replace(replace(replace(phone,'+',''),' ',''),'-',''),'(',''), -10) = ? AND status != 'new' LIMIT 1", val
            cur = await self._c.execute(sql, (arg,))
            if await cur.fetchone():
                return True
        return False

    def _row(self, r) -> dict:
        d = dict(zip(LEAD_FIELDS, r))
        d["extra"] = json.loads(d["extra"] or "{}")
        return d

    async def get_lead(self, lead_id: int) -> dict | None:
        cur = await self._c.execute(f"SELECT {', '.join(LEAD_FIELDS)} FROM crm_leads WHERE id = ?", (lead_id,))
        r = await cur.fetchone()
        return self._row(r) if r else None

    async def find_lead_by_tg(self, tg_user_id: int, account: int | None = None) -> dict | None:
        sql = f"SELECT {', '.join(LEAD_FIELDS)} FROM crm_leads WHERE tg_user_id = ?"
        params: tuple = (tg_user_id,)
        if account is not None:
            sql += " AND account = ?"
            params = (tg_user_id, account)
        cur = await self._c.execute(sql + " ORDER BY updated_at DESC LIMIT 1", params)
        r = await cur.fetchone()
        return self._row(r) if r else None

    async def due_leads(self, now: datetime, limit: int = 20, account: int | None = None) -> list[dict]:
        """Leads of active campaigns whose next step is due."""
        sql = (f"SELECT {', '.join('l.' + f for f in LEAD_FIELDS)} FROM crm_leads l "
               "JOIN campaigns c ON c.id = l.campaign_id "
               "WHERE c.status = 'active' AND l.status IN ('new', 'sent') AND (l.next_at = '' OR l.next_at <= ?)")
        params: list = [iso(now)]
        if account is not None:
            sql += " AND (l.account = ? OR l.account = -1)"
            params.append(account)
        # Follow-ups first (they keep conversations alive), then new leads in insertion order.
        sql += " ORDER BY CASE l.status WHEN 'sent' THEN 0 ELSE 1 END, l.id LIMIT ?"
        params.append(limit)
        cur = await self._c.execute(sql, params)
        return [self._row(r) for r in await cur.fetchall()]

    async def update_lead(self, lead_id: int, **fields) -> None:
        allowed = set(LEAD_FIELDS) - {"id", "campaign_id"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(k)
            if k == "extra":
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k} = ?")
            vals.append(v)
        sets.append("updated_at = datetime('now')")
        await self._c.execute(f"UPDATE crm_leads SET {', '.join(sets)} WHERE id = ?", (*vals, lead_id))
        await self._c.commit()

    async def list_leads(self, cid: int) -> list[dict]:
        cur = await self._c.execute(
            f"SELECT {', '.join(LEAD_FIELDS)} FROM crm_leads WHERE campaign_id = ? ORDER BY id", (cid,))
        return [self._row(r) for r in await cur.fetchall()]

    async def recent_replies(self, limit: int = 15) -> list[dict]:
        cur = await self._c.execute(
            """SELECT m.lead_id, m.text, m.label, m.created_at, l.company, l.name, l.tg_username, l.status
               FROM crm_messages m JOIN crm_leads l ON l.id = m.lead_id
               WHERE m.direction = 'in' ORDER BY m.id DESC LIMIT ?""", (limit,))
        return [{"lead_id": r[0], "text": r[1], "label": r[2], "date": r[3], "company": r[4], "name": r[5],
                 "tg_username": r[6], "status": r[7]} for r in await cur.fetchall()]

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def log_message(self, lead_id: int, direction: str, text: str, label: str = "") -> None:
        await self._c.execute(
            "INSERT INTO crm_messages (lead_id, direction, text, label) VALUES (?, ?, ?, ?)",
            (lead_id, direction, text, label))
        await self._c.commit()

    async def messages(self, lead_id: int) -> list[dict]:
        cur = await self._c.execute(
            "SELECT direction, text, label, created_at FROM crm_messages WHERE lead_id = ? ORDER BY id", (lead_id,))
        return [{"direction": r[0], "text": r[1], "label": r[2], "date": r[3]} for r in await cur.fetchall()]

    # ------------------------------------------------------------------
    # Stop-list
    # ------------------------------------------------------------------

    async def add_stop(self, keys: list[str], reason: str = "") -> None:
        await self._c.executemany("INSERT OR IGNORE INTO stoplist (key, reason) VALUES (?, ?)",
                                  [(k, reason) for k in keys])
        await self._c.commit()

    async def stopped_keys(self) -> set[str]:
        cur = await self._c.execute("SELECT key FROM stoplist")
        return {r[0] for r in await cur.fetchall()}

    # ------------------------------------------------------------------
    # Account quotas
    # ------------------------------------------------------------------

    async def account_day(self, account: int, day: str) -> dict:
        cur = await self._c.execute(
            "SELECT new_sent, followups, resolves FROM account_stats WHERE account = ? AND day = ?", (account, day))
        r = await cur.fetchone()
        return {"new_sent": r[0], "followups": r[1], "resolves": r[2]} if r else \
            {"new_sent": 0, "followups": 0, "resolves": 0}

    async def bump_account(self, account: int, day: str, field: str) -> None:
        if field not in ("new_sent", "followups", "resolves"):
            raise ValueError(field)
        await self._c.execute(
            f"INSERT INTO account_stats (account, day, {field}) VALUES (?, ?, 1) "
            f"ON CONFLICT(account, day) DO UPDATE SET {field} = {field} + 1", (account, day))
        await self._c.commit()

    async def account_active_days(self, account: int, before: str) -> int:
        """Days with outreach before `before` (YYYY-MM-DD) — drives the warm-up ramp."""
        cur = await self._c.execute(
            "SELECT COUNT(*) FROM account_stats WHERE account = ? AND new_sent > 0 AND day < ?", (account, before))
        return (await cur.fetchone())[0]


def next_time(now: datetime, days: float) -> str:
    return iso(now + timedelta(days=days))
