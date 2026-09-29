"""Order reranker: heuristic gate + B.AI LLM fallback for precise freelance intent.

Pipelines:
  1. matches() / _NEGATIVE / _DESC_RE remain fast gate (Title keyword etc).
  2. When LLM key is configured (CRM B.AI settings or BAI_* env), borderline
     cases go to LLM: projects where title is ambiguous but description hints
     at site creation. Hard rejects (vacancy, offer) skip LLM.

Contract with orders_service:
  - score_order(order, keywords, minus) -> (relevant: bool, score: 0..100, reason: str, budget_rub: int|None)
  - rerank_orders(orders, ...) -> list[(Order, meta)] sorted by score
  - matches_llm() preserves old matches() signature (bool)

Fails open: LLM down / no key / timeout -> fallback to matches().
Cost: ~ $0.001/order, batch 10 orders/call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from api.orders import _DESC_RE, _NEGATIVE, DEFAULT_KEYWORDS
from api.orders import matches as heuristic_matches
from crm.http import post_json
from models.order import Order

log = logging.getLogger(__name__)

# Cheap pre-filter: must mention site-ish token anywhere to warrant LLM.
_SITE_LIKE_RE = re.compile(
    r"сайт|лендинг|landing|интернет[-\s]?магазин|tilda|тильд|"
    r"wordpress|битрикс|bitrix|веб-?сайт|одностранич|многостранич",
    re.I,
)
_BUDGET_RE = re.compile(r"(\d[\d\s]*)\s*(?:₽|руб|р\.|k\b|к\b|тыс)", re.I)

# Hard-rejects that never go to LLM.
_HARD_REJECT_RE = re.compile(
    r"ваканси[яюи]|в\s+штат|оклад|з/?п\b|зарплат|полная занятость|"
    r"ищу\s+работ[уа]|предлагаю\s+услуги|без\s+опыта|#резюме|резюме|"
    r"делаю\s+сайты|создаю\s+сайты",
    re.I,
)

SYSTEM_RERANKER = """Ты — классификатор заказов на создание сайта.
Вход: заголовок и описание заказа (рус). Выход строго JSON.
Критерий релевантности: заказчик ПРОСИТ СДЕЛАТЬ сайт/лендинг/интернет-магазин/квиз/переверстать.
НЕ релевантно: вакансия/штат, ищу работу, предлагаю услуги, мнение о сайте,
тексты/SEO/дизайн без создания сайта, правки ≤ 2 часов.
Верни JSON: {"relevant": bool, "score": 0..100, "reason": "коротко почему",
  "budget_rub": null|int, "deadline": null|string, "stack": null|string}
Бюджет: вытяни из текста (₽/руб/k/тыс). Score: 90+ горячий, 60+ релевант, <40 шум.
Отвечай только JSON, без markdown.
"""

FEW_SHOTS: list[dict[str, str]] = [
    {
        "title": "Нужно создать сайт-визитку для стоматологии",
        "description": "Нужен сайт 5 страниц, Tilda, бюджет 40 000 ₽. Срок 7 дней.",
        "want": (
            '{"relevant": true, "score": 92, "reason": "прямой запрос на сайт-визитку", '
            '"budget_rub": 40000, "deadline": "7 дней", "stack": "Tilda"}'
        ),
    },
    {
        "title": "Вакансия: верстальщик в штат",
        "description": "Ищу верстальщика на постоянку, оклад 80к, удалёнка",
        "want": (
            '{"relevant": false, "score": 5, "reason": "вакансия в штат, не проект", '
            '"budget_rub": null, "deadline": null, "stack": null}'
        ),
    },
]

_LLM_TIMEOUT = 12
_LLM_RETRIES = 1
_BATCH = 10


def _heuristic_score(  # noqa: PLR0911
    order: Order, keywords: tuple[str, ...], minus: tuple[str, ...],
) -> tuple[bool, int, str, int | None]:
    """Return (relevant, score, reason, budget) via heuristics only."""
    relevant = heuristic_matches(order, keywords, minus)
    title = order.title.lower()
    desc = order.description.lower()
    text = f"{title} {desc}"
    # budget sniff
    budget_rub = _extract_budget(order.title + " " + order.description + " " + order.budget)
    if not relevant:
        # borderline: site-like in desc but not matched by Title/_DESC_RE
        if _SITE_LIKE_RE.search(text) and not any(n in text for n in _NEGATIVE):
            return False, 45, "heuristic: site-like but no Title/_DESC match — candidate for LLM", budget_rub
        return False, 15, "heuristic: не match", budget_rub
    # relevant: tier by signal strength
    if any(k.lower() in title for k in keywords):
        return True, 80, "heuristic: Title keyword", budget_rub
    if _DESC_RE.search(order.description):
        return True, 75, "heuristic: _DESC_RE", budget_rub
    return True, 65, "heuristic: custom keyword in text", budget_rub


def _extract_budget(text: str) -> int | None:
    if not text:
        return None
    for raw in re.findall(r"(\d[\d\s]*)\s*(?:₽|руб|р\.)", text, re.I):
        try:
            v = int(raw.replace(" ", "").replace("\u00a0", ""))
            if 500 <= v <= 5_000_000:
                return v
        except ValueError:
            continue
    # 30к / 15k without currency nearby
    for m in re.finditer(r"(\d+)\s*к\b", text, re.I):
        try:
            v = int(m.group(1)) * 1000
            if 1000 <= v <= 2_000_000:
                return v
        except ValueError:
            continue
    return None


def _should_call_llm(  # noqa: PLR0913
    order: Order,
    keywords: tuple[str, ...],
    minus: tuple[str, ...],
    heur_relevant: bool,
    heur_score: int,
) -> bool:
    text = f"{order.title}\n{order.description}".lower()
    if _HARD_REJECT_RE.search(text):
        return False
    if any(m.lower() in text for m in minus if m):
        return False
    # LLM for borderline and for relevant with weak signal (custom keywords etc)
    if not heur_relevant and heur_score >= 40:
        return True
    if heur_relevant and heur_score < 80:
        return True
    # otherwise heuristic is decisive
    return False


def _llm_credentials(bai_key: str = "", bai_url: str = "", bai_model: str = "") -> tuple[str, str, str] | None:
    if bai_key and bai_url and bai_model:
        return (bai_key.strip(), bai_url.strip().rstrip("/"), bai_model.strip())
    # fallback env / CRM settings already resolved upstream — if empty, skip
    import os

    k = (bai_key or os.environ.get("BAI_API_KEY", "") or "").strip()
    u = (bai_url or os.environ.get("BAI_BASE_URL", "https://api.b.ai/v1") or "").strip().rstrip("/")
    m = (bai_model or os.environ.get("BAI_MODEL", "qwen3.8-flash") or "").strip()
    if not k:
        return None
    return (k, u, m)


async def _call_llm_batch(
    batch: list[Order],
    bai_key: str,
    bai_url: str,
    bai_model: str,
) -> list[dict[str, Any]]:
    """Call B.AI chat/completions with batch, return per-order dict or fallback."""
    # Build user payload
    items = []
    for o in batch:
        items.append({"title": o.title[:200], "description": (o.description or "")[:800]})
    # include few-shots in system-ish user preamble to keep token low
    shot_examples = "\n".join(f"Title: {s['title']}\nDesc: {s['description']}\nWant: {s['want']}" for s in FEW_SHOTS)
    user_prompt = (
        "Примеры (title+desc -> JSON):\n" + shot_examples + "\n\n"
        "Оцени заказы (верни JSON-массив по порядку, один объект на заказ):\n"
        + json.dumps(items, ensure_ascii=False) +
        "\nВерни JSON-массив."
    )
    payload = {
        "model": bai_model,
        "messages": [
            {"role": "system", "content": SYSTEM_RERANKER},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    headers = {"Authorization": f"Bearer {bai_key}", "Content-Type": "application/json"}
    url = f"{bai_url}/chat/completions"
    try:
        resp = await post_json(url, payload, headers=headers, timeout=_LLM_TIMEOUT, retries=_LLM_RETRIES)
    except Exception as e:
        log.debug("reranker LLM batch failed: %s", e)
        return []

    # extract choice
    try:
        choices = resp.get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else ""
        if not content:
            return []
        # strip code fences
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.S).strip()
        # find JSON array
        m = re.search(r"\[.*\]", content, re.S)
        arr_text = m.group(0) if m else content
        arr = json.loads(arr_text)
        if isinstance(arr, dict):
            arr = [arr]
        if not isinstance(arr, list):
            return []
        return [x if isinstance(x, dict) else {} for x in arr]
    except Exception as e:
        log.debug("reranker parse failed: %s content=%.300r", e, content if 'content' in dir() else "")
        return []


async def score_orders_llm(
    orders: list[Order],
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
    minus: tuple[str, ...] = (),
    bai_key: str = "",
    bai_url: str = "",
    bai_model: str = "",
) -> list[tuple[Order, dict[str, Any]]]:
    """Score each order via LLM when warranted. Returns list (order, meta).

    meta = {relevant:bool, score:0..100, reason:str, budget_rub:int|None, source:"llm"|"heuristic"}
    Never raises — falls back to heuristic.
    """
    creds = _llm_credentials(bai_key, bai_url, bai_model)
    if not creds or not orders:
        return [(o, {**_meta_from_heur(o, keywords, minus), "source": "heuristic"}) for o in orders]

    bk, bu, bm = creds
    # partition
    metas: dict[str, dict[str, Any]] = {}
    to_llm: list[Order] = []
    for o in orders:
        rel, sc, rs, bud = _heuristic_score(o, keywords, minus)
        meta = {"relevant": rel, "score": sc, "reason": rs, "budget_rub": bud, "source": "heuristic"}
        metas[o.uid] = meta
        if _should_call_llm(o, keywords, minus, rel, sc):
            to_llm.append(o)

    if not to_llm:
        return [(o, metas[o.uid]) for o in orders]

    # batch llm calls
    uid_order = {o.uid: o for o in orders}
    # fire in parallel batches
    batches = [to_llm[i: i + _BATCH] for i in range(0, len(to_llm), _BATCH)]
    results = await asyncio.gather(*(_call_llm_batch(b, bk, bu, bm) for b in batches), return_exceptions=True)

    for batch, res in zip(batches, results, strict=False):
        if isinstance(res, BaseException) or not res:
            continue
        for order, llm_meta in zip(batch, res, strict=False):
            if not isinstance(llm_meta, dict):
                continue
            rel = llm_meta.get("relevant")
            if rel is None:
                continue
            rel_bool = bool(rel)
            sc = llm_meta.get("score")
            try:
                sc_int = int(sc) if sc is not None else (85 if rel_bool else 20)
            except (TypeError, ValueError):
                sc_int = 85 if rel_bool else 20
            sc_int = max(0, min(100, sc_int))
            bud = llm_meta.get("budget_rub")
            try:
                bud_int = int(bud) if bud is not None else metas[order.uid].get("budget_rub")
            except (TypeError, ValueError):
                bud_int = metas[order.uid].get("budget_rub")
            metas[order.uid] = {
                "relevant": rel_bool,
                "score": sc_int,
                "reason": str(llm_meta.get("reason") or "llm"),
                "budget_rub": bud_int,
                "deadline": llm_meta.get("deadline"),
                "stack": llm_meta.get("stack"),
                "source": "llm",
            }

    return [(uid_order[o.uid], metas[o.uid]) for o in orders]


def _meta_from_heur(order: Order, keywords: tuple[str, ...], minus: tuple[str, ...]) -> dict[str, Any]:
    rel, sc, rs, bud = _heuristic_score(order, keywords, minus)
    return {"relevant": rel, "score": sc, "reason": rs, "budget_rub": bud}


def matches_llm(order: Order, meta: dict[str, Any] | None = None, keywords=DEFAULT_KEYWORDS, minus=()) -> bool:
    """Drop-in bool for old matches(), using meta.relevant when available."""
    if meta is not None and "relevant" in meta:
        return bool(meta["relevant"])
    return heuristic_matches(order, keywords, minus)


async def rerank_orders(
    orders: list[Order],
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
    minus: tuple[str, ...] = (),
    bai_key: str = "",
    bai_url: str = "",
    bai_model: str = "",
) -> list[tuple[Order, dict[str, Any]]]:
    """Return orders with LLM/heuristic meta, sorted by score desc (relevant first)."""
    scored = await score_orders_llm(orders, keywords, minus, bai_key, bai_url, bai_model)
    scored.sort(key=lambda x: (0 if x[1].get("relevant") else 1, -int(x[1].get("score", 0))))
    return scored
