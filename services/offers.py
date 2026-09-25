"""Offer library: learn outreach techniques from creators' content, generate own offers per niche."""

import asyncio
import html as html_lib
import json
import logging
import random
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

import aiohttp

from api.common import USER_AGENTS
from api.orders import fetch_tg_channel_posts
from services.llm import LLMError

log = logging.getLogger(__name__)

KINDS = ("offer_structure", "hook", "pain", "guarantee", "cta", "followup", "personalization", "objection", "other")
PLACEHOLDERS = ("name", "company", "city", "demo_url", "niche")
OPT_OUT = "Если неактуально — просто напишите, больше не побеспокою."
MAX_FIRST_MESSAGE = 450
MAX_PAGE_CHARS = 60_000
CHUNK_CHARS = 12_000
MAX_CHUNKS = 8
TG_MAX_POSTS = 60
YT_TIMEOUT = 60


class OfferSourceError(RuntimeError):
    """User-facing (Russian) error about a source that couldn't be read."""


# ---------- source fetching ----------

_YT_URL_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TG_RE = re.compile(
    r"^(?:@|(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?)([A-Za-z][A-Za-z0-9_]{3,31})(?:/\d+)?/?(?:\?.*)?$"
)


def youtube_id(ref: str) -> str:
    ref = ref.strip()
    m = _YT_URL_RE.search(ref)
    if m:
        return m.group(1)
    # Bare IDs: avoid treating an ordinary 11-letter word as a video ID.
    if _YT_ID_RE.match(ref) and not ref.isalpha():
        return ref
    return ""


def tg_channel(ref: str) -> str:
    m = _TG_RE.match(ref.strip())
    return m.group(1) if m else ""


def html_to_text(page: str) -> tuple[str, str]:
    """(title, visible text) from an HTML page."""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    title = html_lib.unescape(re.sub(r"\s+", " ", title_m.group(1))).strip() if title_m else ""
    page = re.sub(r"<!--.*?-->", " ", page, flags=re.S)
    page = re.sub(r"<(script|style|noscript|svg|template|head|iframe)\b.*?</\1\s*>", " ", page, flags=re.S | re.I)
    page = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr|/section|/article)\b[^>]*>", "\n", page, flags=re.I)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", page))
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text[:MAX_PAGE_CHARS]


async def _http_get(url: str, timeout: float = 20) -> str:
    headers = {"User-Agent": random.choice(USER_AGENTS), "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.get(url, headers=headers) as r:
            if r.status != 200:
                raise OfferSourceError(f"Страница ответила HTTP {r.status}. Скопируйте текст и пришлите его сообщением.")
            raw = await r.content.read(5_000_000)
            return raw.decode(r.charset or "utf-8", errors="replace")


async def _youtube_title(video_id: str) -> str:
    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(url) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    author = data.get("author_name") or ""
                    title = data.get("title") or ""
                    return f"{title} — {author}" if author and title else title
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        log.info("oembed %s failed: %s", video_id, exc)
    return ""


def _youtube_transcript_sync(video_id: str) -> str:
    try:
        import requests
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api import _errors as yt_errors
    except ImportError as exc:
        raise OfferSourceError(
            "Для YouTube нужен пакет youtube-transcript-api. Пока можно прислать текст видео сообщением."
        ) from exc

    class _TimeoutSession(requests.Session):
        # The library passes no timeout to requests; without this a blocked IP can hang forever.
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", 20)
            return super().request(*args, **kwargs)

    api = YouTubeTranscriptApi(http_client=_TimeoutSession())
    try:
        listing = api.list(video_id)
        try:
            transcript = listing.find_transcript(["ru", "en"])
        except yt_errors.NoTranscriptFound:
            transcript = next(iter(listing))  # any language beats nothing; the LLM reads it anyway
        snippets = transcript.fetch()
    except StopIteration:
        raise OfferSourceError("У этого видео нет субтитров. Пришлите его текст или конспект сообщением.")
    except (yt_errors.RequestBlocked, yt_errors.PoTokenRequired) as exc:
        raise OfferSourceError(
            "YouTube заблокировал запросы с этого сервера. Скопируйте расшифровку видео "
            "(«...» → «Показать текст видео») и пришлите её сообщением."
        ) from exc
    except (yt_errors.TranscriptsDisabled, yt_errors.NoTranscriptFound) as exc:
        raise OfferSourceError("У этого видео нет субтитров. Пришлите его текст или конспект сообщением.") from exc
    except (yt_errors.VideoUnavailable, yt_errors.VideoUnplayable, yt_errors.AgeRestricted,
            yt_errors.InvalidVideoId) as exc:
        raise OfferSourceError("Видео недоступно (удалено, приватное или с ограничением по возрасту).") from exc
    except (yt_errors.CouldNotRetrieveTranscript, requests.RequestException) as exc:
        raise OfferSourceError(
            f"Не удалось получить субтитры YouTube ({type(exc).__name__}). Пришлите текст видео сообщением."
        ) from exc
    text = " ".join(s.text.replace("\n", " ") for s in snippets)
    return re.sub(r"\s+", " ", text).strip()


async def _fetch_youtube(video_id: str) -> tuple[str, str]:
    try:
        text, title = await asyncio.wait_for(
            asyncio.gather(asyncio.to_thread(_youtube_transcript_sync, video_id), _youtube_title(video_id)),
            timeout=YT_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        raise OfferSourceError("YouTube не ответил вовремя. Пришлите текст видео сообщением.") from exc
    if not text:
        raise OfferSourceError("Субтитры видео пустые. Пришлите текст сообщением.")
    return title or f"YouTube {video_id}", text


async def _fetch_tg(channel: str, max_posts: int = TG_MAX_POSTS) -> tuple[str, str]:
    posts: list[dict] = []
    before = ""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            for _ in range(max(1, max_posts // 15)):
                batch = await fetch_tg_channel_posts(session, f"{channel}?before={before}" if before else channel)
                ids = [int(p["id"]) for p in batch if str(p["id"]).isdigit()]
                if not batch or not ids:
                    break
                posts += batch
                if len(posts) >= max_posts or min(ids) <= 1:
                    break
                before = str(min(ids))
                await asyncio.sleep(random.uniform(0.5, 1.0))
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        if not posts:
            raise OfferSourceError(f"Не удалось открыть t.me/s/{channel}: {exc}") from exc
    seen: set[str] = set()
    texts = []
    # t.me/s returns oldest-first within a page; sort newest-first across pages.
    for p in sorted(posts, key=lambda p: int(p["id"]) if str(p["id"]).isdigit() else 0, reverse=True):
        if p["text"] and p["id"] not in seen:
            seen.add(p["id"])
            texts.append(p["text"])
    if not texts:
        raise OfferSourceError(
            f"У @{channel} нет открытой веб-версии или текстовых постов. Это должен быть публичный канал."
        )
    return f"Telegram @{channel}", "\n\n---\n\n".join(texts[:max_posts])


async def fetch_source(ref: str) -> tuple[str, str]:
    """(title, text) from a YouTube video, a public Telegram channel, a web page or pasted text."""
    ref = (ref or "").strip()
    if not ref:
        raise OfferSourceError("Пустой источник.")
    vid = youtube_id(ref) if " " not in ref else ""
    if vid:
        return await _fetch_youtube(vid)
    channel = tg_channel(ref) if " " not in ref else ""
    if channel:
        return await _fetch_tg(channel)
    if re.match(r"^https?://\S+$", ref):
        try:
            page = await _http_get(ref)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise OfferSourceError(f"Не удалось открыть страницу: {exc or type(exc).__name__}") from exc
        title, text = html_to_text(page)
        if len(text) < 200:
            raise OfferSourceError("На странице почти нет текста (возможно, он грузится скриптом). Пришлите текст сообщением.")
        return title or ref, text
    return "Текст", ref


# ---------- technique extraction ----------

EXTRACT_SYSTEM = """Ты — аналитик продаж. Тебе дают материал эксперта (расшифровку видео, посты канала или статью)
про холодные продажи, офферы и первые сообщения. Выдели из него ПЕРЕИСПОЛЬЗУЕМЫЕ приёмы, которые веб-разработчик
сможет применить, продавая сайты и лендинги малому и среднему бизнесу через личные сообщения в Telegram.

Верни JSON: {"techniques": [{"kind": "...", "title": "...", "description": "...", "example": "..."}]}
kind — одно из: offer_structure (структура оффера), hook (зацепка/первая фраза), pain (боль клиента),
guarantee (гарантия/снятие риска), cta (призыв/вопрос в конце), followup (дожим/повторное касание),
personalization (персонализация), objection (работа с возражением), other.
title — короткое название приёма (3–8 слов). description — суть и когда применять (1–3 предложения).
example — короткий пример фразы, адаптированный под продажу сайтов (или пустая строка).
Только конкретные приёмы из материала, без воды, общих советов и рекламы автора. Не больше 12 приёмов.
Если полезных приёмов нет — верни пустой список. Пиши по-русски."""


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (title or "").lower().replace("ё", "е"))).strip()


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split on paragraph/sentence boundaries into chunks of at most ~size chars."""
    text = text.strip()
    chunks: list[str] = []
    while len(text) > size:
        cut = max(text.rfind("\n", 0, size), text.rfind(". ", 0, size))
        if cut < size // 2:
            cut = text.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(text[:cut + 1].strip())
        text = text[cut + 1:].strip()
    if text:
        chunks.append(text)
    return chunks


def _clean_technique(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    description = str(raw.get("description") or "").strip()
    if not title or not description:
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    return {
        "kind": kind if kind in KINDS else "other",
        "title": title[:120],
        "description": description[:1000],
        "example": str(raw.get("example") or "").strip()[:600],
    }


def merge_techniques(items: list[dict]) -> list[dict]:
    """Dedupe by normalized title, keeping the richer description/example."""
    merged: dict[str, dict] = {}
    for item in items:
        t = _clean_technique(item)
        if not t:
            continue
        key = normalize_title(t["title"])
        prev = merged.get(key)
        if not prev:
            merged[key] = t
            continue
        if len(t["description"]) > len(prev["description"]):
            prev["description"] = t["description"]
        if not prev["example"]:
            prev["example"] = t["example"]
        if prev["kind"] == "other":
            prev["kind"] = t["kind"]
    return list(merged.values())


async def extract_techniques(llm, title: str, text: str) -> list[dict]:
    if not getattr(llm, "enabled", False):
        raise LLMError("Для библиотеки офферов нужен LLM (BAI_API_KEY)")
    chunks = chunk_text(text)[:MAX_CHUNKS]
    found: list[dict] = []
    last_error: LLMError | None = None
    for idx, chunk in enumerate(chunks, 1):
        part = f" (часть {idx}/{len(chunks)})" if len(chunks) > 1 else ""
        try:
            data = await llm.chat_json(
                EXTRACT_SYSTEM, f"Источник: {title}{part}\n\n{chunk}", temperature=0.3, max_tokens=3000,
            )
        except LLMError as exc:
            log.warning("technique extraction failed on chunk %s: %s", idx, exc)
            last_error = exc
            continue
        items = data.get("techniques", []) if isinstance(data, dict) else data
        if isinstance(items, list):
            found += items
    result = merge_techniques(found)
    if not result and last_error:
        raise last_error
    return result


# ---------- storage ----------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS offer_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offer_techniques (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES offer_sources(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'other',
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    example TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    niche TEXT NOT NULL,
    offer_text TEXT NOT NULL,
    variants_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offers_niche ON offers(niche, id);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class OfferLibrary:
    """Tables live in the bot's SQLite file; uses the already-open Database connection."""

    def __init__(self, db) -> None:
        self.db = db

    @property
    def _conn(self):
        assert self.db._db, "Database.connect() must be called first"
        return self.db._db

    async def init(self) -> None:
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def add_source(self, ref: str, title: str) -> int:
        cur = await self._conn.execute(
            "INSERT INTO offer_sources (ref, title, created_at) VALUES (?, ?, ?)", (ref[:2000], title[:300], _now()),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def add_techniques(self, source_id: int, techniques: list[dict]) -> int:
        """Insert new techniques, skipping titles already in the library. Returns inserted count."""
        cur = await self._conn.execute("SELECT title FROM offer_techniques")
        known = {normalize_title(r[0]) for r in await cur.fetchall()}
        rows = []
        for t in merge_techniques(techniques):
            key = normalize_title(t["title"])
            if key in known:
                continue
            known.add(key)
            rows.append((source_id, t["kind"], t["title"], t["description"], t["example"]))
        if rows:
            await self._conn.executemany(
                "INSERT INTO offer_techniques (source_id, kind, title, description, example) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            await self._conn.commit()
        return len(rows)

    async def list_techniques(self, limit: int = 200) -> list[dict]:
        cur = await self._conn.execute(
            """SELECT t.id, t.source_id, t.kind, t.title, t.description, t.example, COALESCE(s.title, '')
               FROM offer_techniques t LEFT JOIN offer_sources s ON s.id = t.source_id
               ORDER BY t.id DESC LIMIT ?""",
            (limit,),
        )
        keys = ("id", "source_id", "kind", "title", "description", "example", "source_title")
        return [dict(zip(keys, row)) for row in await cur.fetchall()]

    async def count_by_kind(self) -> dict[str, int]:
        cur = await self._conn.execute("SELECT kind, COUNT(*) FROM offer_techniques GROUP BY kind ORDER BY 2 DESC")
        return {kind: n for kind, n in await cur.fetchall()}

    async def list_sources(self, limit: int = 50) -> list[dict]:
        cur = await self._conn.execute(
            """SELECT s.id, s.ref, s.title, s.created_at, COUNT(t.id) FROM offer_sources s
               LEFT JOIN offer_techniques t ON t.source_id = s.id GROUP BY s.id ORDER BY s.id DESC LIMIT ?""",
            (limit,),
        )
        keys = ("id", "ref", "title", "created_at", "techniques")
        return [dict(zip(keys, row)) for row in await cur.fetchall()]

    async def save_offer(self, niche: str, offer: str, variants: list[dict], angles: list[str] | None = None) -> int:
        payload = json.dumps({"variants": variants, "angles": angles or []}, ensure_ascii=False)
        cur = await self._conn.execute(
            "INSERT INTO offers (niche, offer_text, variants_json, created_at) VALUES (?, ?, ?, ?)",
            (niche, offer, payload, _now()),
        )
        await self._conn.commit()
        return cur.lastrowid

    @staticmethod
    def _offer_row(row) -> dict:
        try:
            payload = json.loads(row[3] or "{}")
        except ValueError:
            payload = {}
        if isinstance(payload, list):  # tolerate a bare variants list
            payload = {"variants": payload}
        return {
            "id": row[0], "niche": row[1], "offer": row[2],
            "variants": payload.get("variants", []), "angles": payload.get("angles", []), "created_at": row[4],
        }

    async def get_offer(self, offer_id: int) -> dict | None:
        cur = await self._conn.execute(
            "SELECT id, niche, offer_text, variants_json, created_at FROM offers WHERE id = ?", (offer_id,),
        )
        row = await cur.fetchone()
        return self._offer_row(row) if row else None

    async def latest_offer(self, niche: str) -> dict | None:
        cur = await self._conn.execute(
            "SELECT id, niche, offer_text, variants_json, created_at FROM offers WHERE niche = ? ORDER BY id DESC LIMIT 1",
            (niche,),
        )
        row = await cur.fetchone()
        return self._offer_row(row) if row else None

    async def list_offers(self, limit: int = 20) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT id, niche, offer_text, variants_json, created_at FROM offers ORDER BY id DESC LIMIT ?", (limit,),
        )
        return [self._offer_row(row) for row in await cur.fetchall()]


# ---------- offer generation ----------

OFFER_SYSTEM = f"""Ты — сильный B2B-копирайтер. Пишешь для веб-разработчика, который продаёт сайты и лендинги
владельцам бизнеса через личные сообщения в Telegram. Опирайся на приёмы из библиотеки, но пиши своими словами.

Правила для всех текстов:
- Только русский язык, живой разговорный тон, на «вы», уважительно. Никакого давления и манипуляций.
- Коротко: первое сообщение не длиннее {MAX_FIRST_MESSAGE} символов, дожимы — до 300 символов.
- Без ссылок, кроме плейсхолдера {{demo_url}}. Без эмодзи, КАПСА, восклицательных цепочек.
- Без спам-слов: «уникальное предложение», «только сегодня», «скидка», «акция», «бесплатно», «гарантируем 100%», «выгодно».
- Можно использовать плейсхолдеры {{name}}, {{company}}, {{city}}, {{demo_url}} — ровно в таком виде, другие не придумывай.
- Первое сообщение: персональный повод (почему пишу именно им), конкретная польза для их бизнеса,
  и в самом конце ОДИН простой вопрос, на который легко ответить «да/нет» (например, «Прислать?»).
- В сообщениях дайте лёгкий выход, например: «{OPT_OUT}»
  В последнем дожиме (followup_2) эта фраза обязательна.

Верни JSON:
{{"offer": "ядро оффера в 2–4 предложениях: конкретный результат, срок, ориентир цены, гарантия и снятие риска",
  "variants": [
    {{"name": "A", "first_message": "...", "followup_1": "через 2–3 дня", "followup_2": "через неделю, последнее"}},
    {{"name": "B", ...}}, {{"name": "C", ...}}
  ],
  "angles": ["3–5 коротких углов подачи для этой ниши"]}}
Варианты A, B, C должны заходить с разных углов (например: готовое демо, потеря заявок, конкуренты)."""

_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.I)


def _niche_dict(niche_info: Any) -> dict:
    if is_dataclass(niche_info) and not isinstance(niche_info, type):
        return asdict(niche_info)
    return dict(niche_info or {})


def _format_techniques(techniques: list[dict], limit: int = 40, max_chars: int = 9000) -> str:
    by_kind: dict[str, list[dict]] = {}
    for t in techniques:
        by_kind.setdefault(t.get("kind") or "other", []).append(t)
    # Round-robin across kinds so one prolific kind doesn't crowd out guarantees/CTAs.
    picked: list[dict] = []
    while len(picked) < limit and any(by_kind.values()):
        for kind in list(by_kind):
            if by_kind[kind] and len(picked) < limit:
                picked.append(by_kind[kind].pop(0))
    lines, total = [], 0
    for t in picked:
        line = f"- [{t.get('kind', 'other')}] {t.get('title', '')}: {t.get('description', '')}"
        if t.get("example"):
            line += f" Пример: «{t['example']}»"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def _clean_message(text: Any, limit: int) -> str:
    text = str(text or "").strip()
    text = _URL_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), cut.rfind("\n"))
        text = cut[: end + 1].strip() if end > limit // 2 else cut.rsplit(" ", 1)[0].rstrip(",;: ") + "…"
    return text


def _has_opt_out(text: str) -> bool:
    low = text.lower()
    return "не побеспокою" in low or "неактуально" in low or "не актуально" in low


def _fallback_variants(niche_label: str) -> list[dict]:
    niche = niche_label.lower()
    return [
        {
            "name": "A",
            "first_message": (
                "Здравствуйте, {name}! Я делаю сайты для компаний в сфере «" + niche + "». "
                "Посмотрел {company} и набросал, как мог бы выглядеть ваш сайт с формой заявки и расчётом стоимости. "
                "Прислать демо посмотреть?"
            ),
            "followup_1": "{name}, добрый день. Демо уже готово: {demo_url} — посмотрите, если будет минута. Что скажете?",
            "followup_2": "Последний раз напомню о себе. " + OPT_OUT,
        },
        {
            "name": "B",
            "first_message": (
                "Здравствуйте, {name}! Клиенты в {city} сначала ищут подрядчика в интернете и сравнивают сайты. "
                "Без сайта часть заявок уходит конкурентам. Могу сделать продающий лендинг за 7–10 дней, "
                "оплата после того, как вам понравится результат. Рассказать подробнее?"
            ),
            "followup_1": "{name}, могу показать пару примеров из вашей ниши и примерный расчёт. Интересно?",
            "followup_2": "Не хочу надоедать. " + OPT_OUT,
        },
        {
            "name": "C",
            "first_message": (
                "Здравствуйте, {name}! Короткий вопрос по {company}: заявки сейчас приходят в основном по "
                "сарафану или из интернета? Помогаю бизнесу в сфере «" + niche + "» получать заявки с сайта. "
                "Можно задам пару вопросов?"
            ),
            "followup_1": "{name}, если удобнее, пришлю короткий разбор: что добавить, чтобы сайт приносил заявки. Прислать?",
            "followup_2": "Понимаю, что сейчас может быть не до этого. " + OPT_OUT,
        },
    ]


def _fallback_offer(niche_label: str, niche: dict) -> str:
    check = f" при среднем чеке {niche['avg_check']} ₽" if niche.get("avg_check") else ""
    return (
        f"Сайт-лендинг для бизнеса «{niche_label}» за 7–10 дней, который собирает заявки{check}. "
        "Сначала показываю демо на ваших материалах, оплата — только если результат устраивает. "
        "Правки в течение месяца после запуска бесплатно."
    )


def validate_offer(data: Any, niche_label: str, niche: dict | None = None) -> dict:
    """Normalize LLM output to {offer, variants[A,B,C], angles}; fill gaps from templates."""
    data = data if isinstance(data, dict) else {}
    offer = _clean_message(data.get("offer"), 1200) or _fallback_offer(niche_label, niche or {})
    raw_variants = data.get("variants") if isinstance(data.get("variants"), list) else []
    fallback = _fallback_variants(niche_label)
    variants = []
    for raw in raw_variants:
        if not isinstance(raw, dict):
            continue
        first = _clean_message(raw.get("first_message"), MAX_FIRST_MESSAGE)
        if not first:
            continue
        variants.append({
            "first_message": first,
            "followup_1": _clean_message(raw.get("followup_1"), 400),
            "followup_2": _clean_message(raw.get("followup_2"), 400),
        })
        if len(variants) == 3:
            break
    while len(variants) < 3:
        variants.append(dict(fallback[len(variants)]))
    for idx, v in enumerate(variants):
        v["name"] = "ABC"[idx]
        v["followup_1"] = v["followup_1"] or fallback[idx]["followup_1"]
        if not v["followup_2"]:
            v["followup_2"] = OPT_OUT
        elif not _has_opt_out(v["followup_2"]):
            v["followup_2"] = f"{v['followup_2']} {OPT_OUT}"
    variants = [{k: v[k] for k in ("name", "first_message", "followup_1", "followup_2")} for v in variants]
    angles = [str(a).strip() for a in data.get("angles") or [] if str(a).strip()][:6] \
        if isinstance(data.get("angles"), list) else []
    return {"offer": offer, "variants": variants, "angles": angles}


async def generate_offer(
    llm, niche_label: str, niche_info: dict, techniques: list[dict], my_profile: str = "",
) -> dict:
    """Own offer + 3 first-message variants (A/B/C) with two follow-ups each."""
    if not getattr(llm, "enabled", False):
        raise LLMError("Для генерации оффера нужен LLM (BAI_API_KEY)")
    niche = _niche_dict(niche_info)
    facts = [f"Ниша: {niche_label}"]
    for key, label in (("avg_check", "Средний чек клиента, ₽"), ("turnover", "Оборот компаний в месяц, ₽"),
                       ("why", "Зачем им сайт")):
        if niche.get(key):
            facts.append(f"{label}: {niche[key]}")
    parts = ["\n".join(facts)]
    if my_profile.strip():
        parts.append(f"Обо мне (разработчике): {my_profile.strip()[:1500]}")
    lib = _format_techniques(techniques)
    parts.append(f"Приёмы из библиотеки:\n{lib}" if lib else "Библиотека приёмов пуста — используй лучшие практики.")
    data = await llm.chat_json(OFFER_SYSTEM, "\n\n".join(parts), temperature=0.8, max_tokens=3000)
    return validate_offer(data, niche_label, niche)


# ---------- templates ----------

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def render_template(text: str, **values) -> str:
    """Single-pass {placeholder} substitution; unknown/missing placeholders become empty."""
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key in PLACEHOLDERS or key in values:
            val = values.get(key)
            return "" if val is None else str(val)
        return ""

    out = _PLACEHOLDER_RE.sub(sub, text or "")
    # Tidy artifacts of empty values: "Здравствуйте, !" -> "Здравствуйте!"
    out = re.sub(r"[ \t]+([,.!?;:])", r"\1", out)
    out = re.sub(r",([!?.])", r"\1", out)
    out = re.sub(r"«\s*»", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return "\n".join(line.strip() for line in out.split("\n")).strip()
