"""Разбор contact_groups 2GIS: телефон/почта/сайт/соцсети.

Раньше это делали api/twogis_api.py и api/twogis_client.py по отдельности,
и реализации разъехались: одна дедупила значения, другая нет, редиректы
link.2gis.ru разворачивались по-разному. Теперь источник правды один.
"""

from __future__ import annotations

from api.common import clean_social, clean_url, is_social_url

# Типы контактов, ведущие в мессенджеры/соцсети, а не на собственный сайт.
MESSENGER_TYPES = frozenset((
    "vkontakte", "vk", "instagram", "facebook", "twitter", "youtube", "telegram",
    "whatsapp", "viber", "odnoklassniki", "ok", "max", "skype", "icq",
))


def split_contacts(contact_groups: list[dict] | None) -> dict[str, list[str]]:
    """Разложить contact_groups по бакетам phone/email/website/socials.

    Возвращает списки без дублей. Ссылки-редиректы link.2gis.ru
    разворачиваются: настоящий адрес стоит после «?».
    """
    phones: list[str] = []
    emails: list[str] = []
    websites: list[str] = []
    socials: list[str] = []

    for group in contact_groups or []:
        for c in group.get("contacts") or []:
            ctype = c.get("type", "")
            value = c.get("value", "")
            if ctype == "phone":
                if value and value not in phones:
                    phones.append(value)
            elif ctype == "email":
                if value and value not in emails:
                    emails.append(value)
            elif ctype == "website":
                url = c.get("url") or c.get("alias") or value
                if "link.2gis.ru" in url and "?" in url:
                    url = url.split("?", 1)[1]
                if is_social_url(url):
                    url = clean_social(url)
                    if url and url not in socials:
                        socials.append(url)
                else:
                    url = clean_url(url)
                    if url and url not in websites:
                        websites.append(url)
            elif ctype in MESSENGER_TYPES:
                link = clean_social(c.get("url") or value)
                if link and link not in socials:
                    socials.append(link)

    return {"phone": phones, "email": emails, "website": websites, "socials": socials}


def join_contacts(contact_groups: list[dict] | None) -> dict[str, str]:
    """То же, но значения склеены запятыми (формат полей Organization)."""
    return {k: ", ".join(v) for k, v in split_contacts(contact_groups).items()}
