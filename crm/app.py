"""CRM для FindClient: HTTP-панель и API.

Запуск:

    python -m crm.app

Отдаёт одностраничную панель и JSON-API. Аутентификация — HTTP Basic,
логин и пароль из `CRM_USER` / `CRM_PASS`. Без пароля панель не поднимется:
в ней лежит история переписки и список клиентов, это не то, что стоит
выставлять в интернет открытым.

Слушает `CRM_HOST`:`CRM_PORT` (по умолчанию 127.0.0.1:8787). Наружу
выставляется через nginx — так пароль не идёт по открытому HTTP.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import pathlib
import sys

from aiohttp import web

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from crm import db as crm_db
from crm import templates as tpl

log = logging.getLogger("crm")

STATIC = pathlib.Path(__file__).resolve().parent / "static"
HOST = os.environ.get("CRM_HOST", "127.0.0.1")
PORT = int(os.environ.get("CRM_PORT", "8787"))
USER = os.environ.get("CRM_USER", "")
PASSWORD = os.environ.get("CRM_PASS", "")
LINK = os.environ.get("CRM_LINK", "https://leonidautomations.ru/demo/")


# --------------------------------------------------------------------------
# Аутентификация
# --------------------------------------------------------------------------

def _check_basic(header: str | None) -> bool:
    if not USER or not PASSWORD:
        return False
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        return False
    got_user, _, got_pass = raw.partition(":")
    return hmac.compare_digest(got_user, USER) and hmac.compare_digest(got_pass, PASSWORD)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not USER or not PASSWORD:
        return web.json_response(
            {"error": "CRM_USER/CRM_PASS не заданы — панель не поднимется"},
            status=500,
        )
    if not _check_basic(request.headers.get("Authorization")):
        return web.Response(
            status=401,
            text="Требуется авторизация",
            headers={"WWW-Authenticate": 'Basic realm="FindClient CRM"'},
        )
    return await handler(request)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

async def api_summary(request: web.Request) -> web.Response:
    return web.json_response(crm_db.summary())


async def api_targets(request: web.Request) -> web.Response:
    q = request.query
    limit = min(int(q.get("limit", 200)), 1000)
    offset = int(q.get("offset", 0))
    items = crm_db.list_targets(
        status=q.get("status"),
        city=q.get("city") or None,
        search=q.get("search") or None,
        limit=limit,
        offset=offset,
    )
    return web.json_response({
        "items": items,
        "counts": crm_db.count_targets(),
        "limit": limit,
        "offset": offset,
    })


async def api_import_targets(request: web.Request) -> web.Response:
    try:
        added = crm_db.import_targets()
    except FileNotFoundError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    crm_db.log_event("import", f"целей добавлено: {added}")
    return web.json_response({"added": added, "counts": crm_db.count_targets()})


async def api_target_status(request: web.Request) -> web.Response:
    target_id = int(request.match_info["id"])
    data = await request.json()
    try:
        crm_db.set_target_status(target_id, data.get("status", ""), data.get("note"))
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"ok": True, "target": crm_db.get_target(target_id)})


async def api_target_username(request: web.Request) -> web.Response:
    target_id = int(request.match_info["id"])
    data = await request.json()
    crm_db.save_target_username(target_id, (data.get("username") or "").strip())
    return web.json_response({"ok": True, "target": crm_db.get_target(target_id)})


async def api_preview(request: web.Request) -> web.Response:
    """Показывает, что именно уйдёт цели. Ничего не отправляет."""
    target_id = int(request.query.get("target_id", "0"))
    body = request.query.get("body")
    with_link = request.query.get("with_link", "0") == "1"

    if body is None:
        template_id = int(request.query.get("template_id", "0"))
        found = [t for t in crm_db.list_templates() if t["id"] == template_id]
        if not found:
            return web.json_response({"error": "шаблон не найден"}, status=404)
        body = found[0]["body"]

    target = crm_db.get_target(target_id) if target_id else {}
    if target is None:
        return web.json_response({"error": "цель не найдена"}, status=404)

    text = tpl.render(body, target or {}, link=LINK, with_link=with_link)
    return web.json_response({
        "text": text,
        "problems": tpl.check(body, with_link=with_link),
        "words": len(text.split()),
    })


async def api_queue(request: web.Request) -> web.Response:
    """Ставит сообщение в очередь. Отправляет уже воркер, не панель."""
    data = await request.json()
    target_id = data.get("target_id")
    chat = (data.get("chat") or "").strip()
    body = (data.get("body") or "").strip()
    kind = data.get("kind", "dm")

    if not body:
        return web.json_response({"error": "пустой текст"}, status=400)

    if not chat and target_id:
        target = crm_db.get_target(int(target_id))
        if not target:
            return web.json_response({"error": "цель не найдена"}, status=404)
        chat = target.get("username") or ""

    if kind == "dm" and not chat:
        return web.json_response(
            {"error": "некуда отправлять: у цели нет @юзернейма, "
                      "а в личку по телефону Telegram не пишет"},
            status=400,
        )

    message_id = crm_db.queue_message(
        int(target_id) if target_id else None, chat, body, kind=kind
    )
    return web.json_response({"ok": True, "message_id": message_id})


async def api_messages(request: web.Request) -> web.Response:
    limit = min(int(request.query.get("limit", 100)), 1000)
    return web.json_response({"items": crm_db.list_messages(limit)})


async def api_templates(request: web.Request) -> web.Response:
    if request.method == "GET":
        return web.json_response({"items": crm_db.list_templates()})
    data = await request.json()
    template_id = crm_db.save_template(
        data.get("name", ""), data.get("category", ""), data.get("body", ""),
        data.get("id"),
    )
    return web.json_response({"ok": True, "id": template_id})


async def api_template_delete(request: web.Request) -> web.Response:
    crm_db.delete_template(int(request.match_info["id"]))
    return web.json_response({"ok": True})


async def api_groups(request: web.Request) -> web.Response:
    items = crm_db.list_groups(
        niche=request.query.get("niche") or None,
        only_unposted=request.query.get("only_unposted") == "1",
    )
    return web.json_response({
        "items": items,
        "niches": crm_db.niche_options(),
        "total": len(items),
    })


async def api_import_groups(request: web.Request) -> web.Response:
    from data.niches import NICHES
    added = crm_db.import_groups(niches_module=NICHES)
    crm_db.log_event("import", f"групп добавлено: {added}")
    return web.json_response({"added": added})


async def api_group_posted(request: web.Request) -> web.Response:
    data = await request.json()
    crm_db.mark_group_posted(int(request.match_info["id"]), data.get("note", ""))
    return web.json_response({"ok": True})


async def api_settings(request: web.Request) -> web.Response:
    if request.method == "GET":
        return web.json_response(crm_db.get_settings())
    data = await request.json()
    allowed = set(crm_db.DEFAULT_SETTINGS)
    values = {k: v for k, v in data.items() if k in allowed}
    if not values:
        return web.json_response({"error": "нечего сохранять"}, status=400)
    crm_db.set_settings(values)
    return web.json_response({"ok": True, "settings": crm_db.get_settings()})


async def index(request: web.Request) -> web.Response:
    path = STATIC / "index.html"
    if not path.exists():
        return web.Response(status=500, text="нет static/index.html")
    return web.Response(text=path.read_text(encoding="utf-8"), content_type="text/html")


def create_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.add_routes([
        web.get("/", index),
        web.get("/api/summary", api_summary),
        web.get("/api/targets", api_targets),
        web.post("/api/targets/import", api_import_targets),
        web.post("/api/targets/{id}/status", api_target_status),
        web.post("/api/targets/{id}/username", api_target_username),
        web.get("/api/preview", api_preview),
        web.post("/api/queue", api_queue),
        web.get("/api/messages", api_messages),
        web.route("*", "/api/templates", api_templates),
        web.delete("/api/templates/{id}", api_template_delete),
        web.get("/api/groups", api_groups),
        web.post("/api/groups/import", api_import_groups),
        web.post("/api/groups/{id}/posted", api_group_posted),
        web.route("*", "/api/settings", api_settings),
    ])
    return app


def seed_templates() -> None:
    """Кладёт шаблоны по умолчанию, если своих ещё нет."""
    if crm_db.list_templates():
        return
    for t in tpl.DEFAULT_TEMPLATES:
        crm_db.save_template(t["name"], t["category"], t["body"])


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    crm_db.init_db()
    seed_templates()
    if not USER or not PASSWORD:
        log.error("CRM_USER и CRM_PASS не заданы — панель не будет отдавать данные")
    log.info("CRM на http://%s:%s", HOST, PORT)
    web.run_app(create_app(), host=HOST, port=PORT, access_log=None)


if __name__ == "__main__":
    main()
