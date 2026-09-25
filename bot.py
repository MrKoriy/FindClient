"""Bot entry point: wires services, starts order polling and long polling."""

import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from config import Settings
from db.database import Database
from handlers import register_routers
from handlers.common import AccessMiddleware
from handlers.outreach import lead_card
from handlers.tg_leads import WATCH_KEY
from models.organization import Organization
from services.crm import CRM
from services.demo_site import DemoSiteStore, build_demo
from services.llm import LLM
from services.offers import OfferLibrary
from services.orders_service import OrdersService
from services.outreach import OutreachLimits, OutreachService
from services.scrape_service import ScrapeService
from services.telegram_service import TelegramUserService
from web.server import start_web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="scrape", description="Компании с карт"),
    BotCommand(command="find", description="Быстрый поиск: ниша | город | кол-во"),
    BotCommand(command="niches", description="Денежные ниши"),
    BotCommand(command="tg", description="Лиды из Telegram"),
    BotCommand(command="orders", description="Автопоиск заказов"),
    BotCommand(command="outreach", description="Рассылки и CRM"),
    BotCommand(command="offers", description="Офферы от креаторов"),
    BotCommand(command="history", description="История сборов"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="cancel", description="Отменить шаг"),
]


async def main() -> None:
    settings = Settings.from_env()
    if not settings.OWNER_IDS:
        log.warning("OWNER_IDS is empty — anyone who finds the bot can use it")

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()
    access = AccessMiddleware(settings.OWNER_IDS)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    register_routers(dp)

    db = Database(settings.DB_PATH)
    await db.connect()

    llm = LLM(settings.BAI_API_KEY, settings.BAI_MODEL, jev_key=settings.TYPESAFE_API_KEY,
              jev_model=settings.JEV_MODEL)
    crm = CRM(db)
    await crm.init()
    offer_lib = OfferLibrary(db)
    await offer_lib.init()

    tg_service = TelegramUserService(settings.TG_API_ID, settings.TG_API_HASH, settings.TG_SESSION)
    accounts = [tg_service] + [
        TelegramUserService(settings.TG_API_ID, settings.TG_API_HASH, s) for s in settings.TG_EXTRA_SESSIONS
    ]
    for i, acc in enumerate(accounts, 1):
        if await acc.start():
            log.info("Telegram account #%d connected", i)
        else:
            log.info("Telegram account #%d disabled: %s", i, acc.error)

    store = DemoSiteStore(os.path.join(os.path.dirname(os.path.abspath(settings.DB_PATH)), "demo_sites"))
    web_runner = None
    try:
        web_runner = await start_web(store, port=settings.WEB_PORT)
    except OSError as exc:
        log.warning("demo web server not started on port %s: %s", settings.WEB_PORT, exc)

    async def demo_builder(lead: dict, campaign: dict) -> str:
        if not settings.DEMO_BASE_URL:
            return ""
        ex = lead.get("extra") or {}
        org = Organization(
            id=str(ex.get("id") or lead["id"]), name=lead["company"] or lead["name"], phone=lead["phone"],
            address=ex.get("address") or "", rating=float(ex.get("rating") or 0), reviews=int(ex.get("reviews") or 0),
            socials=ex.get("socials") or "", website=ex.get("website") or "", city=lead["city"],
            category=lead["category"],
        )
        return await build_demo(org, llm, store, settings.DEMO_BASE_URL, campaign["offer"])

    async def watched_chats() -> list[str]:
        chats: list[str] = []
        for value in await db.all_values(WATCH_KEY):
            chats += [c for c in value if c not in chats]
        return chats

    async def send(chat_id: int, text: str) -> None:
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)

    orders_service = OrdersService(
        db, sender=send, interval=settings.ORDERS_POLL_INTERVAL,
        tg_groups_fetcher=tg_service.make_site_requests_fetcher(watched_chats) if tg_service.enabled else None,
        llm=llm,
    )

    async def on_reply(lead: dict, text: str, cls) -> None:
        campaign = await crm.get_campaign(lead["campaign_id"])
        chat_id = campaign["owner_chat_id"] if campaign else next(iter(settings.OWNER_IDS), 0)
        if not chat_id:
            return
        fire = "🔥 " if cls.hot >= 0.5 else ""
        card, markup = await lead_card(crm, lead["id"], header=f"{fire}<b>Ответ:</b> {cls.label_ru} ({cls.backend})")
        await bot.send_message(chat_id, card, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)

    async def notify_owners(text: str) -> None:
        targets = set(settings.OWNER_IDS) or {c["owner_chat_id"] for c in await crm.list_campaigns()}
        for chat_id in targets:
            await bot.send_message(chat_id, text)

    start_h, _, end_h = settings.OUTREACH_WORK_HOURS.partition("-")
    outreach = OutreachService(
        crm, accounts, llm=llm,
        limits=OutreachLimits(daily_new_max=settings.OUTREACH_DAILY_MAX,
                              work_start=int(start_h or 10), work_end=int(end_h or 19)),
        demo_builder=demo_builder, on_reply=on_reply, notify=notify_owners,
    )
    scrape_service = ScrapeService(
        db=db, request_delay=settings.REQUEST_DELAY,
        yandex_api_key=settings.YANDEX_API_KEY, proxy=settings.HTTP_PROXY,
    )

    await bot.set_my_commands(COMMANDS)
    orders_service.start()
    if outreach.active_accounts():
        outreach.start()
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            db=db,
            settings=settings,
            scrape_service=scrape_service,
            orders_service=orders_service,
            tg_service=tg_service,
            llm=llm,
            crm=crm,
            offer_lib=offer_lib,
            outreach=outreach,
        )
    finally:
        await outreach.stop()
        await orders_service.stop()
        for acc in accounts:
            await acc.stop()
        if web_runner:
            await web_runner.cleanup()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
