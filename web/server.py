"""Serves generated demo sites at /d/{slug}; runs in the bot's event loop next to polling."""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from services.demo_site import DemoSiteStore

log = logging.getLogger(__name__)

STORE_KEY: web.AppKey[DemoSiteStore] = web.AppKey("demo_store", DemoSiteStore)
_NOT_FOUND = Path(__file__).resolve().parent / "templates" / "not_found.html"

# Pages are static and script-free; lock them down in case copy ever slips past escaping.
_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src data:; base-uri 'none'; form-action 'none'"
    ),
}


def _not_found() -> web.Response:
    try:
        body = _NOT_FOUND.read_text(encoding="utf-8")
    except OSError:
        body = "<h1>404</h1>"
    return web.Response(text=body, status=404, content_type="text/html", headers=_HEADERS)


async def demo_page(request: web.Request) -> web.Response:
    html_text = request.app[STORE_KEY].get(request.match_info["slug"])
    if html_text is None:
        return _not_found()
    return web.Response(text=html_text, content_type="text/html", headers=_HEADERS)


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def make_app(store: DemoSiteStore) -> web.Application:
    app = web.Application()
    app[STORE_KEY] = store
    app.router.add_get("/d/{slug}", demo_page)
    app.router.add_get("/health", health)
    return app


async def start_web(store: DemoSiteStore, host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """Start without blocking; call `await runner.cleanup()` on shutdown."""
    runner = web.AppRunner(make_app(store), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("demo sites served on http://%s:%s/d/<slug>", host, port)
    return runner
