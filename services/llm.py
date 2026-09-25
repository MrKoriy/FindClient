"""LLM access: DeepSeek via B.AI (OpenAI-compatible) for text, TypeSafe Jev for typed decisions."""

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

BAI_URL = "https://api.b.ai/v1/chat/completions"
JEV_URL = "https://api.typesafe.ai/v1/systemone"


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> Any:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if m:
            return json.loads(m.group(1))
        raise


class LLM:
    """Thin async client. `enabled` is False without a key; callers must have fallbacks."""

    def __init__(
        self,
        api_key: str = "",
        model: str = "DeepSeek-V4.1-Flash",
        base_url: str = BAI_URL,
        jev_key: str = "",
        jev_model: str = "jev-latest",
        timeout: float = 120,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.jev_key = jev_key
        self.jev_model = jev_model
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def jev_enabled(self) -> bool:
        return bool(self.jev_key)

    async def chat(
        self, system: str, user: str, *, json_mode: bool = False,
        temperature: float = 0.7, max_tokens: int = 2000,
    ) -> str:
        if not self.enabled:
            raise LLMError("LLM не настроен (BAI_API_KEY)")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            # Copywriting/classification don't need long hidden reasoning; keeps it fast and cheap.
            "reasoning_effort": "low",
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last = ""
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as s:
                    async with s.post(self.base_url, json=body, headers=headers) as r:
                        data = await r.json(content_type=None)
                        if r.status == 400 and "reasoning_effort" in body:
                            body.pop("reasoning_effort")  # model/endpoint may not accept it
                            continue
                        if r.status in (429, 500, 502, 503, 529):
                            last = f"HTTP {r.status}"
                            await asyncio.sleep(2 ** attempt * 2)
                            continue
                        if r.status != 200:
                            raise LLMError(f"B.AI {r.status}: {str(data)[:300]}")
                return (data["choices"][0]["message"].get("content") or "").strip()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = str(exc)
                await asyncio.sleep(2 ** attempt * 2)
        raise LLMError(f"B.AI недоступен: {last}")

    async def chat_json(self, system: str, user: str, **kw) -> Any:
        text = await self.chat(system + "\nОтвечай только валидным JSON.", user, json_mode=True, **kw)
        try:
            return _extract_json(text)
        except ValueError as exc:
            raise LLMError(f"модель вернула не JSON: {text[:200]}") from exc

    async def jev(self, state: Any, questions: dict[str, dict]) -> dict[str, dict]:
        """TypeSafe System One call: typed answers (noul / choice / score) with probabilities."""
        if not self.jev_enabled:
            raise LLMError("Jev не настроен (TYPESAFE_API_KEY)")
        body = {"model": self.jev_model, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {self.jev_key}", "Content-Type": "application/json"}
        for attempt in range(3):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.post(JEV_URL, json=body, headers=headers) as r:
                    data = await r.json(content_type=None)
                    if r.status in (429, 529):
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if r.status != 200:
                        raise LLMError(f"Jev {r.status}: {str(data)[:300]}")
                    return data.get("answers") or {}
        raise LLMError("Jev: превышен лимит запросов")
