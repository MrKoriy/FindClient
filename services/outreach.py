"""Telegram outreach engine ("свой Instantly"): sequences, per-account warm-up quotas, reply handling.

Safety model: few personalised messages from the user's own accounts, working hours only,
random pauses, warm-up ramp, stop-list, sequence stops on any reply. Hitting a Telegram
limit (PeerFlood/FloodWait) pauses the account instead of retrying from another one.
"""

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.classifier import ReplyClass, classify_reply
from services.demo_site import _in_city
from services.crm import CRM, STATUS_RU, next_time, stop_keys, utcnow
from services.llm import LLM, LLMError

log = logging.getLogger(__name__)

TG_USER_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{4,31})(?![A-Za-z0-9_/])")
TG_PHONE_RE = re.compile(r"(?:https?://)?t\.me/\+?(\d{10,15})")
PLACEHOLDER_RE = re.compile(r"\{([a-z_]{2,20})\}")  # unknown names render as empty

ReplyCallback = Callable[[dict, str, ReplyClass], Awaitable[None]]
Notify = Callable[[str], Awaitable[None]]


@dataclass
class OutreachLimits:
    daily_new_max: int = 20      # new dialogs per account per day after warm-up
    warmup_start: int = 5        # first day
    warmup_step: int = 3         # + per active day
    followups_daily: int = 40
    phone_resolves_daily: int = 10  # importing contacts by phone is a strong spam signal
    min_delay: int = 240         # seconds between messages of one account
    max_delay: int = 720
    work_start: int = 10
    work_end: int = 19
    weekdays_only: bool = True
    tz: str = "Europe/Moscow"


def render(text: str, **values: str) -> str:
    """Fill {name} {company} {city} {demo_url} {niche}; drop lines whose demo link is missing."""
    lines = []
    for line in text.splitlines():
        if "{demo_url}" in line and not values.get("demo_url"):
            # Drop only the sentence with the missing link, keep the greeting etc.
            line = " ".join(s for s in re.split(r"(?<=[.!?])\s+", line) if "{demo_url}" not in s)
            if not line.strip():
                continue
        lines.append(PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1)) or ""), line))
    out = "\n".join(lines)
    out = re.sub(r"[ \t]+([,.!?])", r"\1", out)
    out = re.sub(r"(Здравствуйте|Добрый день|Привет),\s*!", r"\1!", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def leads_from_orgs(orgs: list[dict]) -> list[dict]:
    """Map search results -> CRM leads (Telegram username from socials, else phone)."""
    out = []
    for o in orgs:
        socials = o.get("socials", "") or ""
        user = next((u for u in TG_USER_RE.findall(socials) if u.lower() not in ("joinchat", "share", "addstickers")), "")
        phone_link = TG_PHONE_RE.search(socials)
        phone = (o.get("phone", "") or "").split(",")[0].strip()
        if phone_link and not phone:
            phone = "+" + phone_link.group(1)
        if not user and not phone:
            continue
        out.append({
            "source": "maps", "ref_id": f"org:{o.get('id')}", "company": o.get("name", ""),
            "city": o.get("city", ""), "category": o.get("category", ""), "phone": phone,
            "tg_username": user,
            "extra": {k: o.get(k) for k in ("id", "address", "rating", "reviews", "website", "socials", "url", "email")},
        })
    return out


def leads_from_tg(rows: list[dict]) -> list[dict]:
    return [{
        "source": "tg", "ref_id": f"tg:{r.get('user_id') or r.get('username')}", "name": r.get("name", ""),
        "tg_username": r.get("username", ""), "tg_user_id": int(r.get("user_id") or 0),
        "phone": r.get("phone", ""), "extra": {"chats": r.get("chats", ""), "sample": r.get("sample", "")[:400]},
    } for r in rows if r.get("username") or r.get("user_id") or r.get("phone")]


class OutreachService:
    def __init__(
        self,
        crm: CRM,
        accounts: list,  # list[TelegramUserService]; index = account id
        llm: LLM | None = None,
        limits: OutreachLimits | None = None,
        demo_builder: Callable[[dict, dict], Awaitable[str]] | None = None,
        on_reply: ReplyCallback | None = None,
        notify: Notify | None = None,
    ) -> None:
        self.crm = crm
        self.accounts = accounts
        self.llm = llm
        self.limits = limits or OutreachLimits()
        self.demo_builder = demo_builder
        self.on_reply = on_reply
        self.notify = notify
        self.paused_until: dict[int, datetime] = {}
        self.next_send: dict[int, datetime] = {}
        self._task: asyncio.Task | None = None
        self._handlers_installed: set[int] = set()

    # ------------------------------------------------------------------
    # Accounts & limits
    # ------------------------------------------------------------------

    def active_accounts(self) -> list[int]:
        return [i for i, a in enumerate(self.accounts) if getattr(a, "enabled", False)]

    async def daily_limit(self, account: int, now: datetime | None = None) -> int:
        days = await self.crm.account_active_days(account, before=self._today(now or utcnow()))
        lim = self.limits
        return min(lim.daily_new_max, lim.warmup_start + lim.warmup_step * days)

    def in_work_hours(self, now: datetime | None = None) -> bool:
        local = (now or utcnow()).astimezone(ZoneInfo(self.limits.tz))
        if self.limits.weekdays_only and local.weekday() >= 5:
            return False
        return self.limits.work_start <= local.hour < self.limits.work_end

    def _today(self, now: datetime) -> str:
        return now.astimezone(ZoneInfo(self.limits.tz)).strftime("%Y-%m-%d")

    async def account_status(self) -> list[dict]:
        now = utcnow()
        out = []
        for i, acc in enumerate(self.accounts):
            day = await self.crm.account_day(i, self._today(now))
            paused = self.paused_until.get(i)
            out.append({
                "account": i, "enabled": getattr(acc, "enabled", False), "error": getattr(acc, "error", ""),
                "new_sent": day["new_sent"], "followups": day["followups"],
                "limit": await self.daily_limit(i),
                "paused_until": paused if paused and paused > now else None,
            })
        return out

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> int:
        """One scheduler pass: at most one message per account. Returns messages sent."""
        now = now or utcnow()
        if not self.in_work_hours(now):
            return 0
        sent = 0
        today = self._today(now)
        for acc in self.active_accounts():
            if self.paused_until.get(acc, now) > now or self.next_send.get(acc, now) > now:
                continue
            day = await self.crm.account_day(acc, today)
            new_ok = day["new_sent"] < await self.daily_limit(acc, now)
            fu_ok = day["followups"] < self.limits.followups_daily
            for lead in await self.crm.due_leads(now, limit=30, account=acc):
                is_new = lead["status"] == "new"
                if (is_new and not new_ok) or (not is_new and not fu_ok):
                    continue
                if is_new and not lead["tg_username"] and not lead["tg_user_id"] \
                        and day["resolves"] >= self.limits.phone_resolves_daily:
                    continue
                if await self.process_lead(acc, lead, now):
                    sent += 1
                    self.next_send[acc] = now + timedelta(
                        seconds=random.randint(self.limits.min_delay, self.limits.max_delay))
                break  # one attempt per account per tick keeps pacing human-like
        return sent

    @staticmethod
    def template_for(lead: dict, campaign: dict) -> str:
        """Pick (and remember on the lead) the A/B variant, return the template of the current step."""
        variants = campaign["variants"] or [{"name": "A", "first_message": campaign["offer"]}]
        variant = next((v for v in variants if v.get("name") == lead["variant"]), None)
        if not variant:
            variant = variants[lead["id"] % len(variants)]
            lead["variant"] = variant.get("name", "A")
        key = ["first_message", "followup_1", "followup_2"][min(lead["step"], 2)]
        return variant.get(key) or ""

    async def compose(self, lead: dict, campaign: dict) -> str:
        template = self.template_for(lead, campaign)
        # "в {city}" -> proper locative ("в Казани").
        template = re.sub(r"\bв \{city\}", "{in_city}", template)
        values = {"name": (lead["name"] or "").split(" ")[0], "company": lead["company"],
                  "city": lead["city"], "in_city": _in_city(lead["city"] or "").strip(),
                  "demo_url": lead["demo_url"], "niche": campaign["niche"]}
        text = render(template, **values)
        if lead["step"] == 0 and campaign["personalize"] and self.llm and self.llm.enabled:
            try:
                text = await self.personalize(text, lead, campaign)
            except LLMError as exc:
                log.warning("personalize failed: %s", exc)
        return text

    async def personalize(self, text: str, lead: dict, campaign: dict) -> str:
        ex = lead.get("extra") or {}
        facts = "; ".join(f"{k}: {v}" for k, v in {
            "компания": lead["company"], "имя": lead["name"], "город": lead["city"],
            "рубрика": lead["category"], "рейтинг": ex.get("rating"), "отзывов": ex.get("reviews"),
            "сайт": ex.get("website") or "нет", "пример сообщения": ex.get("sample"),
        }.items() if v)
        out = await self.llm.chat(
            "Ты адаптируешь холодное сообщение веб-разработчика под конкретного получателя в Telegram. "
            "Сохрани смысл, длину (±20%), ссылки и фразу про отказ. Добавь одну конкретную деталь о "
            "получателе, если она есть. Без эмодзи, без капса, без выдуманных фактов. Верни только текст.",
            f"Факты о получателе: {facts}\n\nСообщение:\n{text}",
            temperature=0.6, max_tokens=600,
        )
        out = out.strip().strip('"')
        # Guard against the model dropping the demo link or ballooning the text.
        if lead["demo_url"] and lead["demo_url"] in text and lead["demo_url"] not in out:
            return text
        return out if 20 < len(out) < len(text) * 1.6 + 100 else text

    async def _resolve(self, acc: int, lead: dict, today: str):
        client = self.accounts[acc].client
        if lead["tg_username"]:
            return await client.get_entity(lead["tg_username"])
        if lead["tg_user_id"]:
            return await client.get_entity(lead["tg_user_id"])
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact

        await self.crm.bump_account(acc, today, "resolves")
        res = await client(ImportContactsRequest([InputPhoneContact(
            client_id=random.randint(1, 2**31), phone=lead["phone"],
            first_name=(lead["company"] or lead["name"] or "Клиент")[:60], last_name="")]))
        if not res.users:
            raise LookupError("у номера нет Telegram или он скрыт настройками приватности")
        return res.users[0]

    async def process_lead(self, acc: int, lead: dict, now: datetime | None = None) -> bool:
        from telethon import errors

        now = now or utcnow()
        today = self._today(now)
        campaign = await self.crm.get_campaign(lead["campaign_id"])
        if not campaign:
            return False
        stopped = await self.crm.stopped_keys()
        if any(k in stopped for k in stop_keys(lead["tg_username"], lead["tg_user_id"], lead["phone"])):
            await self.crm.update_lead(lead["id"], status="stopped")
            return False
        try:
            if lead["step"] == 0 and campaign["use_demo"] and self.demo_builder and lead["source"] == "maps" \
                    and not lead["demo_url"] and "{demo_url}" in self.template_for(lead, campaign):
                try:
                    lead["demo_url"] = await self.demo_builder(lead, campaign)
                except Exception as exc:
                    log.warning("demo build failed for lead %s: %s", lead["id"], exc)
            text = await self.compose(lead, campaign)
            entity = await self._resolve(acc, lead, today)
            await self.accounts[acc].client.send_message(entity, text, link_preview=bool(lead["demo_url"]))
        except errors.PeerFloodError:
            self.paused_until[acc] = now + timedelta(hours=24)
            await self._notify(f"⛔ Аккаунт #{acc + 1}: Telegram ограничил первые сообщения (PeerFlood). "
                               "Пауза 24 ч. Снизьте дневной лимит и проверьте @SpamBot.")
            return False
        except errors.FloodWaitError as exc:
            self.paused_until[acc] = now + timedelta(seconds=exc.seconds + 60)
            await self._notify(f"⏸ Аккаунт #{acc + 1}: FloodWait {exc.seconds} с — пауза.")
            return False
        except (errors.UserPrivacyRestrictedError, errors.UserIsBlockedError, errors.InputUserDeactivatedError,
                errors.UsernameNotOccupiedError, errors.UsernameInvalidError, errors.PeerIdInvalidError,
                LookupError, ValueError) as exc:
            await self.crm.update_lead(lead["id"], status="failed", note=str(exc)[:200], account=acc)
            return False
        except Exception as exc:
            log.exception("send failed for lead %s", lead["id"])
            # Back off so one broken lead never blocks the queue; give up after 3 attempts.
            extra = {**(lead.get("extra") or {}), "attempts": int((lead.get("extra") or {}).get("attempts", 0)) + 1}
            if extra["attempts"] >= 3:
                await self.crm.update_lead(lead["id"], status="failed", note=f"ошибка: {exc}"[:200], extra=extra)
            else:
                await self.crm.update_lead(lead["id"], note=f"ошибка: {exc}"[:200], extra=extra,
                                           next_at=next_time(now, 1 / 24))
            return False

        delays = campaign["delays"]
        step = lead["step"] + 1
        has_next = step <= len(delays)
        await self.crm.update_lead(
            lead["id"], step=step, status="sent" if has_next else "finished", account=acc,
            tg_user_id=getattr(entity, "id", 0) or lead["tg_user_id"], variant=lead["variant"],
            demo_url=lead["demo_url"], next_at=next_time(now, delays[step - 1]) if has_next else "",
        )
        await self.crm.log_message(lead["id"], "out", text)
        await self.crm.bump_account(acc, today, "new_sent" if lead["step"] == 0 else "followups")
        return True

    async def send_manual(self, lead_id: int, text: str) -> None:
        lead = await self.crm.get_lead(lead_id)
        if not lead:
            raise LookupError("лид не найден")
        acc = lead["account"] if lead["account"] >= 0 else (self.active_accounts() or [0])[0]
        if not getattr(self.accounts[acc], "enabled", False):
            raise RuntimeError(f"аккаунт #{acc + 1} не подключён")
        client = self.accounts[acc].client
        target = lead["tg_user_id"] or lead["tg_username"]
        await client.send_message(target, text)
        await self.crm.log_message(lead_id, "out", text, label="manual")

    async def draft_reply(self, lead_id: int) -> str:
        lead = await self.crm.get_lead(lead_id)
        campaign = await self.crm.get_campaign(lead["campaign_id"]) if lead else None
        if not lead or not campaign:
            raise LookupError("лид не найден")
        if not (self.llm and self.llm.enabled):
            raise LLMError("для черновиков нужен BAI_API_KEY")
        history = "\n".join(
            f"{'Я' if m['direction'] == 'out' else 'Клиент'}: {m['text']}" for m in await self.crm.messages(lead_id))
        return await self.llm.chat(
            "Ты веб-разработчик, ведёшь переписку с потенциальным клиентом в Telegram. Ответь на последнее "
            "сообщение клиента: коротко (до 400 символов), по-человечески, без давления. Цель — созвон на "
            "10–15 минут или бриф. Если спрашивают цену — дай вилку из оффера и что в неё входит. "
            "Если отказ — вежливо попрощайся. Верни только текст ответа.",
            f"Оффер:\n{campaign['offer']}\n\nПереписка:\n{history}",
            temperature=0.6, max_tokens=500,
        )

    # ------------------------------------------------------------------
    # Replies
    # ------------------------------------------------------------------

    async def handle_incoming(self, acc: int, sender_id: int, text: str) -> tuple[dict, ReplyClass] | None:
        lead = await self.crm.find_lead_by_tg(sender_id, account=acc) or await self.crm.find_lead_by_tg(sender_id)
        if not lead or not text:
            return None
        msgs = await self.crm.messages(lead["id"])
        last_out = next((m["text"] for m in reversed(msgs) if m["direction"] == "out"), "")
        cls = await classify_reply(self.llm, text, last_out)
        await self.crm.log_message(lead["id"], "in", text, label=cls.label)
        status = {
            "stop": "stopped", "not_interested": "lost", "has_site": "lost", "wrong_person": "lost",
            "interested": "interested", "price": "interested", "question": "interested",
        }.get(cls.label, "replied")
        # Never downgrade a deal the user already moved forward by hand.
        if lead["status"] in ("meeting", "won") and status not in ("stopped",):
            status = lead["status"]
        await self.crm.update_lead(lead["id"], status=status, last_label=cls.label, next_at="")
        if cls.is_stop:
            await self.crm.add_stop(stop_keys(lead["tg_username"], sender_id, lead["phone"]), reason="просил не писать")
        lead = await self.crm.get_lead(lead["id"])
        if self.on_reply:
            try:
                await self.on_reply(lead, text, cls)
            except Exception:
                log.exception("on_reply callback failed")
        return lead, cls

    def install_reply_handlers(self) -> None:
        from telethon import events

        for acc in self.active_accounts():
            if acc in self._handlers_installed:
                continue
            client = self.accounts[acc].client

            async def handler(event, _acc=acc):
                if event.is_private and not event.out:
                    await self.handle_incoming(_acc, event.sender_id, event.raw_text or "")

            client.add_event_handler(handler, events.NewMessage(incoming=True))
            self._handlers_installed.add(acc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _notify(self, text: str) -> None:
        if self.notify:
            try:
                await self.notify(text)
            except Exception:
                log.exception("notify failed")

    async def run_forever(self, interval: int = 30) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("outreach tick failed")
            await asyncio.sleep(interval)

    def start(self) -> None:
        self.install_reply_handlers()
        if not self._task:
            self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def funnel_text(stats: dict) -> str:
    by = stats["by_status"]
    parts = [f"{STATUS_RU.get(k, k)}: {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])]
    rate = round(stats["replied"] * 100 / stats["contacted"]) if stats["contacted"] else 0
    return f"Всего {stats['total']} · написали {stats['contacted']} · ответили {stats['replied']} ({rate}%)\n" + \
        " · ".join(parts)

