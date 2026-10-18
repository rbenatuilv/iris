"""Minimal Telegram Bot API client (send + long-poll), no extra dependencies."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("iris.telegram")
LIMIT = 4000


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += line[:limit] + "\n"
    if cur:
        out.append(cur)
    return out


class Telegram:
    def __init__(self, client: httpx.AsyncClient, token: str, chat_id: str | None):
        self.client = client
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id) if chat_id else None

    async def call(self, method: str, http_timeout: float = 30, **params):
        r = await self.client.post(f"{self.base}/{method}", json=params, timeout=http_timeout)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]

    async def send(self, text: str, chat_id: str | None = None, silent: bool = False) -> None:
        for part in chunks(text):
            await self.call("sendMessage", chat_id=chat_id or self.chat_id, text=part, parse_mode="HTML",
                            disable_web_page_preview=True, disable_notification=silent)

    async def send_document(self, path, caption: str = "", silent: bool = True) -> None:
        with open(path, "rb") as fh:
            r = await self.client.post(f"{self.base}/sendDocument", timeout=120,
                                       data={"chat_id": self.chat_id, "caption": caption,
                                             "disable_notification": str(silent).lower()},
                                       files={"document": (getattr(path, "name", str(path)).split("/")[-1], fh)})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram sendDocument failed: {data.get('description')}")

    async def updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", http_timeout=timeout + 15, **params)
