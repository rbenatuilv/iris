"""Claude API client over plain HTTP (no SDK: lighter on the Pi).

- structured outputs via a forced tool call
- optional server-side web search (with pause_turn handling)
- exact cost accounting from `usage`, persisted per day, with a hard daily cap
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import httpx

log = logging.getLogger("iris.llm")
API = "https://api.anthropic.com/v1/messages"

# USD per million tokens: (input, output, cache_write_5m, cache_read)
DEFAULT_PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.10),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
}
WEB_SEARCH_USD = 0.01
WEB_SEARCH_TYPES = ["web_search_20260318", "web_search_20250305"]


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, client: httpx.AsyncClient, api_key: str | None, store, cfg):
        self.client = client
        self.key = api_key
        self.store = store
        self.cfg = cfg
        lc = cfg.get("llm", {}) or {}
        self.triage_model = lc.get("triage_model", "claude-haiku-5-5")
        self.verify_model = lc.get("verify_model", "claude-haiku-5-5")
        self.critical_verify_model = lc.get("critical_verify_model", "claude-sonnet-5-5")
        self.context_model = lc.get("context_model", "claude-haiku-5-5")
        self.daily_budget = float(lc.get("daily_budget_usd", 0.40))
        self.web_search = bool(lc.get("web_search", True))
        self.prices = {**DEFAULT_PRICES, **{k: tuple(v) for k, v in (lc.get("prices") or {}).items()}}
        self._ws_type_idx = 0

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    # ------------------------------------------------------------- budget
    def _day(self) -> str:
        return datetime.now(self.cfg.tz).date().isoformat()

    def spent_today(self) -> float:
        return float(self.store.kv_get(f"llm_spend:{self._day()}", 0.0) or 0.0)

    def spend_stats(self) -> dict:
        return self.store.kv_get(f"llm_stats:{self._day()}", {}) or {}

    def budget_left(self) -> float:
        return self.daily_budget - self.spent_today()

    def _record(self, model: str, usage: dict, purpose: str) -> float:
        p_in, p_out, p_cw, p_cr = self.prices.get(model, (5.0, 25.0, 6.25, 0.5))
        searches = (usage.get("server_tool_use") or {}).get("web_search_requests", 0) or 0
        cost = (usage.get("input_tokens", 0) * p_in + usage.get("output_tokens", 0) * p_out
                + (usage.get("cache_creation_input_tokens", 0) or 0) * p_cw
                + (usage.get("cache_read_input_tokens", 0) or 0) * p_cr) / 1e6 + searches * WEB_SEARCH_USD
        day = self._day()
        self.store.kv_set(f"llm_spend:{day}", self.spent_today() + cost)
        stats = self.spend_stats()
        s = stats.setdefault(purpose, {"calls": 0, "usd": 0.0, "searches": 0})
        s["calls"] += 1
        s["usd"] = round(s["usd"] + cost, 5)
        s["searches"] += searches
        self.store.kv_set(f"llm_stats:{day}", stats)
        month = day[:7]
        self.store.kv_set(f"llm_spend_month:{month}", (self.store.kv_get(f"llm_spend_month:{month}", 0.0) or 0.0) + cost)
        return cost

    def spent_month(self) -> float:
        return float(self.store.kv_get(f"llm_spend_month:{self._day()[:7]}", 0.0) or 0.0)

    # ------------------------------------------------------------- raw call
    async def _post(self, body: dict) -> dict:
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        for attempt in range(3):
            r = await self.client.post(API, headers=headers, json=body, timeout=httpx.Timeout(120.0, connect=10.0))
            if r.status_code in (429, 500, 502, 503, 529):
                wait = float(r.headers.get("retry-after", 0) or (5 * (attempt + 1)))
                log.warning("Claude API %s, retrying in %.0fs", r.status_code, wait)
                await asyncio.sleep(min(wait, 30))
                continue
            data = r.json()
            if r.status_code != 200:
                err = (data.get("error") or {})
                raise LLMError(f"{r.status_code} {err.get('type')}: {err.get('message')}")
            return data
        raise LLMError("Claude API unavailable after retries")

    async def call(self, *, model: str, system: str, messages: list, purpose: str, max_tokens: int = 2000,
                   tools: list | None = None, tool_choice: dict | None = None, search: int = 0) -> dict:
        """One logical request. Handles pause_turn continuation for server tools. Returns final response."""
        if not self.enabled:
            raise LLMError("no ANTHROPIC_API_KEY configured")
        if self.budget_left() <= 0:
            raise BudgetExceeded(f"daily LLM budget ${self.daily_budget:.2f} reached")
        all_tools = list(tools or [])
        if search and self.web_search:
            ws = {"type": WEB_SEARCH_TYPES[self._ws_type_idx], "name": "web_search", "max_uses": search}
            if self._ws_type_idx == 0:
                ws["allowed_callers"] = ["direct"]
            all_tools.append(ws)
        msgs = list(messages)
        body = {"model": model, "max_tokens": max_tokens,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "messages": msgs}
        if all_tools:
            body["tools"] = all_tools
        if tool_choice:
            body["tool_choice"] = tool_choice
        for _ in range(4):
            try:
                data = await self._post(body)
            except LLMError as e:
                # Older/newer web search tool version not accepted: fall back once
                if search and "web_search" in str(e) and self._ws_type_idx + 1 < len(WEB_SEARCH_TYPES):
                    self._ws_type_idx += 1
                    log.warning("web search tool version rejected, falling back to %s", WEB_SEARCH_TYPES[self._ws_type_idx])
                    return await self.call(model=model, system=system, messages=messages, purpose=purpose,
                                           max_tokens=max_tokens, tools=tools, tool_choice=tool_choice, search=search)
                raise
            self._record(model, data.get("usage", {}), purpose)
            if data.get("stop_reason") == "pause_turn":
                msgs = msgs + [{"role": "assistant", "content": data["content"]}]
                body["messages"] = msgs
                continue
            data["_messages"] = msgs
            return data
        return data

    async def structured(self, *, model: str, system: str, user: str, tool_name: str, schema: dict,
                         purpose: str, max_tokens: int = 2000, search: int = 0) -> dict:
        """Ask for a JSON object matching `schema`, returned through a tool call.
        With search>0 the model may search the web first, then must call the tool."""
        tool = {"name": tool_name, "description": "Record your answer. Always call this exactly once.",
                "input_schema": schema}
        messages = [{"role": "user", "content": user}]
        if search:
            data = await self.call(model=model, system=system, messages=messages, purpose=purpose,
                                   max_tokens=max_tokens, tools=[tool], tool_choice={"type": "auto"}, search=search)
            out = _tool_input(data, tool_name)
            if out is not None:
                return out
            # The model answered in prose: ask it to record the answer now, without more searching
            msgs = data["_messages"] + [{"role": "assistant", "content": data["content"]},
                                        {"role": "user", "content": f"Now call {tool_name} with your answer."}]
            data = await self.call(model=model, system=system, messages=msgs, purpose=purpose, max_tokens=max_tokens,
                                   tools=[tool], tool_choice={"type": "tool", "name": tool_name})
        else:
            data = await self.call(model=model, system=system, messages=messages, purpose=purpose,
                                   max_tokens=max_tokens, tools=[tool], tool_choice={"type": "tool", "name": tool_name})
        out = _tool_input(data, tool_name)
        if out is None:
            raise LLMError(f"model did not return {tool_name}")
        return out

    async def text(self, *, model: str, system: str, user: str, purpose: str, max_tokens: int = 600,
                   search: int = 0) -> str:
        data = await self.call(model=model, system=system, messages=[{"role": "user", "content": user}],
                               purpose=purpose, max_tokens=max_tokens, search=search)
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def _tool_input(data: dict, name: str):
    for b in data.get("content", []):
        if b.get("type") == "tool_use" and b.get("name") == name:
            inp = b.get("input")
            if isinstance(inp, str):
                try:
                    inp = json.loads(inp)
                except ValueError:
                    return None
            return inp
    return None
