"""Helpers shared by source clients: browser headers, URL/phone normalisation, lead scoring."""

import math
import re
from urllib.parse import urlparse

# 2GIS redirects old browsers (Chrome <=131) to an "update your browser" page,
# so keep these current.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0",
]

SOCIAL_DOMAINS = (
    "vk.com", "vk.ru", "vkontakte.ru", "instagram.com", "facebook.com", "fb.com", "ok.ru",
    "odnoklassniki.ru", "t.me", "telegram.me", "wa.me", "whatsapp.com", "youtube.com",
    "youtu.be", "rutube.ru", "dzen.ru", "zen.yandex.ru", "tiktok.com", "twitter.com", "x.com",
    "max.ru", "viber.com", "viber.click", "avito.ru", "taplink.cc", "taplink.ru", "linktr.ee", "2gis.ru",
    "yandex.ru/maps", "jivo.chat", "jivosite.com", "api.whatsapp.com", "prodoctorov.ru",
    "zoon.ru", "flamp.ru", "yell.ru", "profi.ru",
)


def is_social_url(url: str) -> bool:
    """True for social networks, messengers and aggregators (not an own website)."""
    if not url:
        return False
    u = url.strip().lower()
    if "://" not in u:
        u = "http://" + u
    parsed = urlparse(u)
    host = parsed.netloc.removeprefix("www.")
    full = host + parsed.path
    return any(host == d or host.endswith("." + d) or full.startswith(d) for d in SOCIAL_DOMAINS)


_TRACKING = re.compile(r"^(utm_\w+|yclid|gclid|fbclid|_openstat|from|ref)$", re.I)


def clean_url(url: str) -> str:
    """Strip tracking parameters that maps append (utm_*, yclid...)."""
    url = url.strip()
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    kept = [p for p in query.split("&") if p and not _TRACKING.match(p.split("=", 1)[0])]
    return base + ("?" + "&".join(kept) if kept else "")


def clean_social(url: str) -> str:
    """Messenger links often carry a long prefilled ?text=... — keep just the address."""
    url = url.strip()
    if re.match(r"https?://(wa\.me|api\.whatsapp\.com|t\.me|max\.ru)/", url):
        url = url.split("?", 1)[0]
    return url


def phone_key(phone: str) -> str:
    """Last 10 digits of the first phone — used to match the same company across sources."""
    first = phone.split(",")[0]
    digits = re.sub(r"\D", "", first)
    return digits[-10:] if len(digits) >= 10 else ""


def _digits(part: str) -> str:
    return "".join(ch for ch in part if ch.isdigit())


def is_mobile_phone(phone: str) -> bool:
    """Есть ли среди номеров мобильный: последние 10 цифр начинаются на «9».

    Городские (495/499/496 и любые коды городов), 8-800 и зарубежные
    отсекаются: по ним до ЛПР в мессенджере не достучаться.
    """
    for part in (phone or "").split(","):
        digits = _digits(part)
        if len(digits) >= 10 and digits[-10] == "9":
            return True
    return False


def mobile_numbers(phone: str) -> str:
    """Только мобильные номера из строки телефонов (для колонки «Телефон»)."""
    kept = [
        part.strip() for part in (phone or "").split(",")
        if len(_digits(part)) >= 10 and _digits(part)[-10] == "9"
    ]
    return ", ".join(kept)


_MESSENGER_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|wa\.me)/[A-Za-z0-9_]+", re.I
)


def messenger_link(socials: str) -> str:
    """Первая ссылка t.me/wa.me из соцсетей карточки — приоритетный контакт.

    У малого бизнеса такую ссылку почти всегда оставляет сам хозяин:
    это ближайший путь к ЛПР без звонков и секретарей.
    """
    m = _MESSENGER_RE.search(socials or "")
    if not m:
        return ""
    link = m.group(0)
    return link if link.startswith("http") else "https://" + link


def name_key(name: str, address: str = "") -> str:
    text = f"{name}|{address}".lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9|]", "", text)


def lead_score(*, has_website: bool, phone: str, reviews: int, branches: int, rating: float) -> int:
    """0..100: how promising a company is as a website buyer.

    Maps expose no revenue data, so review volume and branch count serve as
    proxies for size/turnover; missing site and reachable phone matter most.
    """
    score = 0 if has_website else 35
    score += 15 if phone else 0
    score += min(25, int(math.log10(reviews + 1) * 10)) if reviews > 0 else 0
    if branches >= 5:
        score += 15
    elif branches >= 2:
        score += 8
    if rating >= 4.5:
        score += 10
    elif rating >= 4.0:
        score += 5
    return min(100, score)


# ---------------------------------------------------------------- site-check

_PARKING_MARKERS = (
    "домен продается",
    "домен продаётся",
    "parking",
    "заглушка",
    "скоро открытие",
    "coming soon",
    "domain for sale",
)


async def check_site_alive(url: str, session) -> bool:
    """HEAD/GET -> жив ли сайт. Парковка/заглушка считается мёртвой.

    Неблокирующая: любой сбой = False (лучше пропустить проверку, чем упасть).
    """
    if not url or not (url := url.strip()):
        return False
    if "://" not in url:
        url = "https://" + url
    try:
        async with session.head(url, allow_redirects=True, timeout=10) as r:
            if r.status >= 400:
                return False
            # 2xx/3xx -> пробуем быстро глянуть на парковку через GET title
            if r.status < 400:
                try:
                    async with session.get(url, allow_redirects=True, timeout=10) as rg:
                        if rg.status >= 400:
                            return False
                        ct = (rg.headers.get("Content-Type") or "").lower()
                        if "text/html" not in ct and ct:
                            return True
                        text = await rg.text(errors="ignore")
                        low = text.lower()
                        # ищем маркеры парковки в первых 5кб
                        snippet = low[:5000]
                        if any(m in snippet for m in _PARKING_MARKERS):
                            return False
                        return True
                except Exception:
                    # HEAD прошёл — считаем живым
                    return True
            return True
    except Exception:
        return False


async def enrich_site_check(orgs, session, sem: int = 10) -> dict[str, bool]:
    """Проверить `website` у списка orgs. Возвращает {url: alive}.

    Не меняет orgs in-place — только возвращает карту. Вызывать опционально
    в scrape_service после merge.
    """
    import asyncio as _asyncio

    semaphore = _asyncio.Semaphore(sem)
    out: dict[str, bool] = {}

    async def one(url: str) -> None:
        async with semaphore:
            out[url] = await check_site_alive(url, session)

    urls = [o.website for o in orgs if getattr(o, "website", "").strip()]
    # dedup
    uniq = list(dict.fromkeys(urls))
    if not uniq:
        return {}
    await _asyncio.gather(*(one(u) for u in uniq))
    return out
