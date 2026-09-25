"""Helpers shared by source clients: browser headers, URL/phone normalisation, lead scoring."""

import math
import re
from urllib.parse import urlparse

# 2GIS redirects old browsers (Chrome <=131) to an "update your browser" page,
# so keep these current.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0",
]

SOCIAL_DOMAINS = (
    "vk.com", "vk.ru", "vkontakte.ru", "instagram.com", "facebook.com", "fb.com", "ok.ru",
    "odnoklassniki.ru", "t.me", "telegram.me", "wa.me", "whatsapp.com", "youtube.com",
    "youtu.be", "rutube.ru", "dzen.ru", "zen.yandex.ru", "tiktok.com", "twitter.com", "x.com",
    "max.ru", "viber.com", "viber.click", "avito.ru", "taplink.cc", "taplink.ru", "linktr.ee", "2gis.ru",
    "yandex.ru/maps", "jivo.chat", "jivosite.com", "api.whatsapp.com", "prodoctorov.ru", "zoon.ru", "flamp.ru", "yell.ru", "profi.ru",
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
