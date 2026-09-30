"""CRM для FindClient: HTTP-панель и API.

Запуск:

    python -m crm.app

Отдаёт одностраничную панель и JSON-API. Аутентификация - HTTP Basic,
логин и пароль из `CRM_USER` / `CRM_PASS`. Без пароля панель не поднимется:
в ней лежит история переписки и список клиентов, это не то, что стоит
выставлять в интернет открытым.

Слушает `CRM_HOST`:`CRM_PORT` (по умолчанию 127.0.0.1:8787). Наружу
выставляется через nginx - так пароль не идёт по открытому HTTP.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import os
import pathlib
import sys

from aiohttp import web

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from crm import bai as crm_bai
from crm import db as crm_db
from crm import offer as crm_offer
from crm import templates as tpl

load_dotenv()
load_dotenv("crm.env")

TRUSTED_ORIGINS = {o.strip() for o in os.environ.get("CRM_TRUSTED_ORIGINS", "").split(",") if o.strip()}

def _origin_allowed(origin: str, request: web.Request) -> bool:
    if not origin:
        return True
    # allow same-origin (host matches)
    try:
        from urllib.parse import urlparse as _up
        o = _up(origin)
        if o.netloc == request.headers.get("Host", ""):
            return True
        if o.netloc in TRUSTED_ORIGINS:
            return True
    except Exception:
        pass
    return False

log = logging.getLogger("crm")


def _int_or_400(raw: str | None, name: str, default: int | None = None) -> tuple[int | None, web.Response | None]:
    """Числовой query/path-параметр. Некорректный - это 400, а не сырой 500."""
    if raw is None:
        return default, None
    try:
        return int(raw), None
    except ValueError:
        return None, web.json_response({"error": f"{name} должен быть числом"}, status=400)

STATIC = pathlib.Path(__file__).resolve().parent / "static"
HOST = os.environ.get("CRM_HOST", "127.0.0.1")
PORT = int(os.environ.get("CRM_PORT", "8787"))
USER = os.environ.get("CRM_USER", "admin")
PASSWORD = os.environ.get("CRM_PASS", "admin")
LINK = os.environ.get("CRM_LINK", "https://leonidautomations.ru/demo/")


from crm import auth

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
    # CSRF: mutating methods require JSON content-type and (SameSite handles cross-site; check Origin for POST)
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        ctype = request.content_type or ""
        # allow form posts only to /auth; API must be json
        if not request.path.startswith("/auth") and "application/json" not in ctype:
            return web.json_response({"error": "Content-Type must be application/json"}, status=400)
        origin = request.headers.get("Origin", "")
        if origin and not _origin_allowed(origin, request):
            return web.json_response({"error": "Origin not allowed"}, status=403)
    if not USER or not PASSWORD or USER == "admin" and PASSWORD == "admin":
        return web.json_response(
            {"error": "CRM_USER/CRM_PASS не заданы или дефолтные - панель не поднимется"},
            status=500,
        )

    # 1. Пути авторизации по токену и выхода всегда доступны
    if request.path.startswith("/auth"):
        return await handler(request)

    # 2. Проверка сессионной куки crm_session (от входа через Telegram-бота)
    cookie_token = request.cookies.get("crm_session")
    if cookie_token and auth.verify_session_cookie(cookie_token) is not None:
        return await handler(request)

    # 2b. Проверка заголовка X-CRM-Token (для Telegram WebApp и localStorage)
    hdr_token = request.headers.get("X-CRM-Token")
    if hdr_token:
        uid = auth.verify_magic_token(hdr_token) or auth.verify_session_cookie(hdr_token)
        if uid is not None:
            return await handler(request)

    # 3. Проверка прямого токена в строке запроса (?token=...)
    url_token = request.query.get("token")
    if url_token:
        uid = auth.verify_magic_token(url_token)
        if uid is not None:
            resp = await handler(request)
            resp.set_cookie(
                "crm_session",
                auth.create_session_cookie(uid),
                max_age=auth.SESSION_TTL,
                httponly=True,
                samesite="Lax",
                path="/",
            )
            return resp

    # 4. Проверка HTTP Basic Auth (для ручного входа по логину и паролю)
    if _check_basic(request.headers.get("Authorization")):
        return await handler(request)

    return web.Response(
        status=401,
        text="Требуется авторизация",
        headers={"WWW-Authenticate": 'Basic realm="FindClient CRM"'},
    )


async def handle_auth(request: web.Request) -> web.Response:
    """Обрабатывает одноразовый токен авторизации из Telegram-бота."""
    token = request.query.get("token")
    if not token:
        return web.Response(status=400, text="Отсутствует токен авторизации")

    uid = auth.verify_magic_token(token)
    if uid is None:
        return web.Response(status=403, text="Недействительный или истекший токен авторизации")

    target = request.query.get("redirect", "/")
    if not target.startswith("/") or target.startswith("//"):
        target = "/"

    resp = web.HTTPFound(target)
    is_https = request.url.scheme == "https"
    resp.set_cookie(
        "crm_session",
        auth.create_session_cookie(uid),
        max_age=auth.SESSION_TTL,
        httponly=True,
        secure=is_https,
        samesite="Lax",
        path="/",
    )
    raise resp


async def handle_logout(request: web.Request) -> web.Response:
    """Сбрасывает сессионную куку авторизации."""
    resp = web.HTTPFound("/")
    resp.del_cookie("crm_session", path="/")
    raise resp


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

async def api_summary(request: web.Request) -> web.Response:
    return web.json_response(await crm_db.summary())


async def api_targets(request: web.Request) -> web.Response:
    q = request.query
    limit, err = _int_or_400(q.get("limit"), "limit", 200)
    if err:
        return err
    offset, err = _int_or_400(q.get("offset"), "offset", 0)
    if err:
        return err
    limit = min(limit, 1000)
    items = await crm_db.list_targets(
        status=q.get("status"),
        city=q.get("city") or None,
        search=q.get("search") or None,
        limit=limit,
        offset=offset,
    )
    return web.json_response({
        "items": items,
        "counts": await crm_db.count_targets(),
        "limit": limit,
        "offset": offset,
    })


async def api_import_targets(request: web.Request) -> web.Response:
    try:
        added = await crm_db.import_targets()
    except FileNotFoundError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    await crm_db.log_event("import", f"целей добавлено: {added}")
    return web.json_response({"added": added, "counts": await crm_db.count_targets()})


async def api_seed_demo_targets(request: web.Request) -> web.Response:
    added = await crm_db.seed_demo_targets()
    await crm_db.log_event("seed_demo", f"демо-целей добавлено: {added}")
    return web.json_response({"added": added, "counts": await crm_db.count_targets()})



async def api_target_status(request: web.Request) -> web.Response:
    target_id, err = _int_or_400(request.match_info["id"], "id")
    if err:
        return err
    data = await request.json()
    try:
        await crm_db.set_target_status(target_id, data.get("status", ""), data.get("note"))
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"ok": True, "target": await crm_db.get_target(target_id)})


async def api_target_username(request: web.Request) -> web.Response:
    target_id, err = _int_or_400(request.match_info["id"], "id")
    if err:
        return err
    data = await request.json()
    await crm_db.save_target_username(target_id, (data.get("username") or "").strip())
    return web.json_response({"ok": True, "target": await crm_db.get_target(target_id)})


async def api_preview(request: web.Request) -> web.Response:
    """Показывает, что именно уйдёт цели. Ничего не отправляет."""
    target_id, err = _int_or_400(request.query.get("target_id"), "target_id", 0)
    if err:
        return err
    body = request.query.get("body")
    with_link = request.query.get("with_link", "0") == "1"

    if body is None:
        template_id, terr = _int_or_400(request.query.get("template_id"), "template_id", 0)
        if terr:
            return terr
        found = [t for t in await crm_db.list_templates() if t["id"] == template_id]
        if not found:
            return web.json_response({"error": "шаблон не найден"}, status=404)
        body = found[0]["body"]

    target = await crm_db.get_target(target_id) if target_id else {}
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
        try:
            target = await crm_db.get_target(int(target_id))
        except (TypeError, ValueError):
            return web.json_response({"error": "target_id должен быть числом"}, status=400)
        if not target:
            return web.json_response({"error": "цель не найдена"}, status=404)
        chat = target.get("username") or ""

    try:
        target_id_int = int(target_id) if target_id else None
    except (TypeError, ValueError):
        return web.json_response({"error": "target_id должен быть числом"}, status=400)

    if kind == "dm" and not chat:
        return web.json_response(
            {"error": "некуда отправлять: у цели нет @юзернейма, "
                      "а в личку по телефону Telegram не пишет"},
            status=400,
        )

    message_id = await crm_db.queue_message(target_id_int, chat, body, kind=kind)
    return web.json_response({"ok": True, "message_id": message_id})


async def api_messages(request: web.Request) -> web.Response:
    limit, err = _int_or_400(request.query.get("limit"), "limit", 100)
    if err:
        return err
    limit = min(limit, 1000)
    return web.json_response({"items": await crm_db.list_messages(limit)})


async def api_templates(request: web.Request) -> web.Response:
    if request.method == "GET":
        return web.json_response({"items": await crm_db.list_templates()})
    data = await request.json()
    template_id = await crm_db.save_template(
        data.get("name", ""), data.get("category", ""), data.get("body", ""),
        data.get("id"),
    )
    return web.json_response({"ok": True, "id": template_id})


async def api_template_delete(request: web.Request) -> web.Response:
    template_id, err = _int_or_400(request.match_info["id"], "id")
    if err:
        return err
    await crm_db.delete_template(template_id)
    return web.json_response({"ok": True})


async def api_groups(request: web.Request) -> web.Response:
    items = await crm_db.list_groups(
        niche=request.query.get("niche") or None,
        only_unposted=request.query.get("only_unposted") == "1",
    )
    return web.json_response({
        "items": items,
        "niches": await crm_db.niche_options(),
        "total": len(items),
    })


async def api_import_groups(request: web.Request) -> web.Response:
    from data.niches import NICHES
    added = await crm_db.import_groups(niches_module=NICHES)
    await crm_db.log_event("import", f"групп добавлено: {added}")
    return web.json_response({"added": added})


async def api_group_posted(request: web.Request) -> web.Response:
    group_id, err = _int_or_400(request.match_info["id"], "id")
    if err:
        return err
    data = await request.json()
    await crm_db.mark_group_posted(group_id, data.get("note", ""))
    return web.json_response({"ok": True})


async def api_settings(request: web.Request) -> web.Response:
    if request.method == "GET":
        return web.json_response(await crm_db.get_settings())
    if request.content_type and "application/json" not in request.content_type:
        return web.json_response({"error": "Content-Type must be application/json"}, status=400)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "body must be object"}, status=400)
    allowed = set(crm_db.DEFAULT_SETTINGS)
    # validate numeric fields
    num_int = {"daily_cap": (1, 1000), "min_delay": (5, 3600), "max_delay": (5, 3600),
               "cooldown_every": (0, 100), "cooldown_seconds": (60, 3600),
               "work_from": (0, 23), "work_to": (0, 24), "timezone_offset": (-12, 14)}
    for k, rng in num_int.items():
        if k in data:
            try:
                v = int(str(data[k]).strip())
            except Exception:
                return web.json_response({"error": f"{k} must be integer"}, status=400)
            if not (rng[0] <= v <= rng[1]):
                return web.json_response({"error": f"{k} out of range {rng}"}, status=400)
            data[k] = str(v)
    values = {k: str(v) for k, v in data.items() if k in allowed}
    if not values:
        return web.json_response({"error": "нечего сохранять"}, status=400)
    await crm_db.set_settings(values)
    return web.json_response({"ok": True, "settings": await crm_db.get_settings()})


async def api_offer_strategies(request: web.Request) -> web.Response:
    return web.json_response({"items": crm_offer.get_strategies()})


async def api_models(request: web.Request) -> web.Response:
    return web.json_response({"items": crm_bai.get_supported_models()})


async def api_offer_generate(request: web.Request) -> web.Response:
    data = await request.json()
    target_id = data.get("target_id")
    target = data.get("target") or {}
    if target_id and not target:
        try:
            target = await crm_db.get_target(int(target_id)) or {}
        except (TypeError, ValueError):
            return web.json_response({"error": "target_id должен быть числом"}, status=400)

    strategy_id = data.get("strategy_id", "lost_traffic")
    with_link = bool(data.get("with_link", False))
    link = data.get("link", LINK)
    prompt_hint = data.get("prompt_hint", "")

    settings = await crm_db.get_settings()
    api_key = settings.get("typesafe_api_key") or os.environ.get("TYPESAFE_API_KEY", "")
    bai_key = settings.get("bai_api_key") or os.environ.get("BAI_API_KEY", "")
    bai_model = data.get("model") or settings.get("bai_model") or os.environ.get("BAI_MODEL", "qwen3.8-flash")
    bai_url = settings.get("bai_base_url") or os.environ.get("BAI_BASE_URL", "https://api.b.ai/v1")

    result = await crm_offer.generate_offer(
        target=target,
        strategy_id=strategy_id,
        link=link,
        with_link=with_link,
        prompt_hint=prompt_hint,
        api_key=api_key,
        bai_key=bai_key,
        bai_model=bai_model,
        bai_url=bai_url,
    )
    return web.json_response(result)


async def api_offer_classify(request: web.Request) -> web.Response:
    data = await request.json()
    text = (data.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "текст оффера пуст"}, status=400)

    target_id = data.get("target_id")
    target = data.get("target") or {}
    if target_id and not target:
        try:
            target = await crm_db.get_target(int(target_id)) or {}
        except (TypeError, ValueError):
            return web.json_response({"error": "target_id должен быть числом"}, status=400)

    with_link = bool(data.get("with_link", False))

    settings = await crm_db.get_settings()
    api_key = settings.get("typesafe_api_key") or os.environ.get("TYPESAFE_API_KEY", "")

    result = await crm_offer.classify_offer(
        text=text,
        target=target,
        with_link=with_link,
        api_key=api_key,
    )
    return web.json_response(result)


async def api_offer_improve(request: web.Request) -> web.Response:
    data = await request.json()
    text = (data.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "текст оффера пуст"}, status=400)

    target_id = data.get("target_id")
    target = data.get("target") or {}
    if target_id and not target:
        try:
            target = await crm_db.get_target(int(target_id)) or {}
        except (TypeError, ValueError):
            return web.json_response({"error": "target_id должен быть числом"}, status=400)

    settings = await crm_db.get_settings()
    api_key = settings.get("typesafe_api_key") or os.environ.get("TYPESAFE_API_KEY", "")
    bai_key = settings.get("bai_api_key") or os.environ.get("BAI_API_KEY", "")
    bai_model = data.get("model") or settings.get("bai_model") or os.environ.get("BAI_MODEL", "qwen3.8-flash")
    bai_url = settings.get("bai_base_url") or os.environ.get("BAI_BASE_URL", "https://api.b.ai/v1")

    improved = await crm_offer.auto_improve_offer(
        text,
        target=target,
        api_key=api_key,
        bai_key=bai_key,
        bai_model=bai_model,
        bai_url=bai_url,
    )

    classification = await crm_offer.classify_offer(
        text=improved,
        target=target,
        with_link=bool(data.get("with_link", False)),
        api_key=api_key,
    )
    return web.json_response({"text": improved, "classification": classification})


async def api_bulk_queue(request: web.Request) -> web.Response:
    data = await request.json()
    filt = data.get("filter") or {}
    status = filt.get("status", "new")
    city = filt.get("city") or None
    search = filt.get("search") or None
    limit_raw = data.get("limit", 20)
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        return web.json_response({"error": "limit должен быть числом"}, status=400)
    limit = max(1, min(limit, 100))

    # тело сообщения: template_id или bodies/body
    bodies: list[str] = []
    template_id = data.get("template_id")
    if template_id is not None:
        try:
            tid = int(template_id)
        except (TypeError, ValueError):
            return web.json_response({"error": "template_id должен быть числом"}, status=400)
        found = [t for t in await crm_db.list_templates() if t["id"] == tid]
        if not found:
            return web.json_response({"error": "шаблон не найден"}, status=404)
        bodies = [found[0]["body"]]
    elif data.get("bodies"):
        raw_bodies = data.get("bodies")
        if isinstance(raw_bodies, list):
            bodies = [str(b).strip() for b in raw_bodies if str(b).strip()]
        else:
            bodies = [str(raw_bodies).strip()]
    elif data.get("body"):
        bodies = [str(data.get("body")).strip()]

    if not bodies:
        return web.json_response({"error": "нужен template_id или bodies/body"}, status=400)

    body_text = bodies[0]
    with_link = bool(data.get("with_link", False))

    targets = await crm_db.list_targets(status=status, city=city, search=search, limit=limit)
    queued = 0
    skipped = 0
    for t in targets:
        chat = (t.get("username") or "").strip()
        if not chat:
            skipped += 1
            continue
        rendered = tpl.render(body_text, t, link=LINK, with_link=with_link)
        if not rendered.strip():
            skipped += 1
            continue
        await crm_db.queue_message(t["id"], chat, rendered)
        queued += 1
    return web.json_response({"queued": queued, "skipped": skipped, "total": len(targets)})


async def api_sequences_list(request: web.Request) -> web.Response:
    target_id, err = _int_or_400(request.query.get("target_id"), "target_id")
    if err:
        return err
    if target_id is None:
        return web.json_response({"error": "нужен target_id"}, status=400)
    items = await crm_db.get_target_sequences(target_id)
    return web.json_response({"items": items})


async def api_sequences_create(request: web.Request) -> web.Response:
    data = await request.json()
    target_id = data.get("target_id")
    try:
        tid = int(target_id)
    except (TypeError, ValueError):
        return web.json_response({"error": "target_id должен быть числом"}, status=400)
    target = await crm_db.get_target(tid)
    if not target:
        return web.json_response({"error": "цель не найдена"}, status=404)

    bodies = data.get("template_bodies") or data.get("bodies") or []
    if isinstance(bodies, str):
        bodies = [bodies]
    template_id = data.get("template_id")
    if template_id is not None and not bodies:
        try:
            tmpl_id = int(template_id)
        except (TypeError, ValueError):
            return web.json_response({"error": "template_id должен быть числом"}, status=400)
        found = [t for t in await crm_db.list_templates() if t["id"] == tmpl_id]
        if not found:
            return web.json_response({"error": "шаблон не найден"}, status=404)
        # если один шаблон - делаем одно касание; если нужен drip - клиент шлёт 5 bodies
        bodies = [found[0]["body"]]

    if not bodies:
        return web.json_response({"error": "нужен template_bodies/bodies или template_id"}, status=400)

    delays = data.get("delays_hours")
    if delays is not None:
        try:
            delays = [int(x) for x in delays]
        except (TypeError, ValueError):
            return web.json_response({"error": "delays_hours должны быть числами"}, status=400)
    else:
        delays = [0, 48, 96, 168, 240]

    # рендерим под цель если нужно
    with_link = bool(data.get("with_link", False))
    rendered_bodies = [tpl.render(b, target, link=LINK, with_link=with_link) for b in bodies]

    ids = await crm_db.create_drip_sequence(tid, rendered_bodies, delays_hours=delays)
    # первый шаг сразу в очередь сообщений — именно созданный ids[0]
    first = None
    if ids:
        items_tmp = await crm_db.get_target_sequences(tid)
        first = next((x for x in items_tmp if x["id"] == ids[0]), None)
    if first and first["status"] == "queued":
        chat = (target.get("username") or "").strip()
        if chat:
            mid = await crm_db.queue_message(tid, chat, first["body"])
            await crm_db.mark_sequence_queued(first["id"], mid)
    items = await crm_db.get_target_sequences(tid)
    return web.json_response({"ok": True, "ids": ids, "items": items})


async def api_sequences_cancel(request: web.Request) -> web.Response:
    seq_id, err = _int_or_400(request.match_info["id"], "id")
    if err:
        return err
    # пытаемся отменить конкретный шаг
    async with crm_db.connect() as conn:
        cur = await conn.execute("SELECT * FROM sequences WHERE id = ?", (seq_id,))
        row = await cur.fetchone()
        if not row:
            return web.json_response({"error": "шаг не найден"}, status=404)
        if row["status"] == "pending":
            await conn.execute(
                "UPDATE sequences SET status = 'cancelled' WHERE id = ?", (seq_id,)
            )
        # поддержка target_id-отмены: ?target_id=X отменяет все pending цели
    return web.json_response({"ok": True})


async def index(request: web.Request) -> web.Response:
    path = STATIC / "index.html"
    if not path.exists():
        return web.Response(status=500, text="нет static/index.html")
    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    return web.Response(text=text, content_type="text/html")


def create_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.add_routes([
        web.get("/", index),
        web.get("/auth", handle_auth),
        web.get("/auth/logout", handle_logout),
        web.get("/api/summary", api_summary),
        web.get("/api/targets", api_targets),
        web.post("/api/targets/import", api_import_targets),
        web.post("/api/targets/seed_demo", api_seed_demo_targets),
        web.post("/api/targets/bulk_queue", api_bulk_queue),
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
        web.get("/api/models", api_models),
        web.get("/api/offer/strategies", api_offer_strategies),
        web.post("/api/offer/generate", api_offer_generate),
        web.post("/api/offer/classify", api_offer_classify),
        web.post("/api/offer/improve", api_offer_improve),
        web.route("*", "/api/settings", api_settings),
        web.get("/api/sequences", api_sequences_list),
        web.post("/api/sequences", api_sequences_create),
        web.post("/api/sequences/{id}/cancel", api_sequences_cancel),
    ])
    return app


async def seed_templates() -> None:
    """Кладёт шаблоны и демо-цели по умолчанию, если своих ещё нет."""
    if not await crm_db.list_templates():
        for t in tpl.DEFAULT_TEMPLATES:
            await crm_db.save_template(t["name"], t["category"], t["body"])
    if (await crm_db.count_targets()).get("all", 0) == 0:
        await crm_db.seed_demo_targets()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    await crm_db.init_db()
    await seed_templates()
    if not USER or not PASSWORD:
        log.error("CRM_USER и CRM_PASS не заданы - панель не будет отдавать данные")
    log.info("CRM на http://%s:%s", HOST, PORT)
    runner = web.AppRunner(create_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
