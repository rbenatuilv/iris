import asyncio, json
import httpx, respx
from datetime import datetime, timezone
from iris.app import Iris
from iris.store import Store
from iris.models import Event
from test_news import cfg, FakeClaude  # noqa: F401


def test_context_followup_and_digest_overview(cfg):
    cfg.raw["llm"]["context_followups"] = True
    cfg.raw["llm"]["context_delay_minutes"] = 0
    sent, claude = [], FakeClaude()

    async def go():
        with respx.mock(assert_all_called=False) as m:
            m.post(url__regex=r".*/sendMessage").mock(side_effect=lambda r: (sent.append(json.loads(r.content)["text"]),
                                                                             httpx.Response(200, json={"ok": True, "result": {}}))[1])
            m.post("https://api.anthropic.com/v1/messages").mock(side_effect=claude)
            async with httpx.AsyncClient() as client:
                app = Iris(cfg, store=Store(":memory:"), client=client)
                app.store.kv_set("baseline:usgs", True)
                ev = Event(key="quake:1", kind="quake", title="M6.8 earthquake — Maule, Chile",
                           time=datetime.now(timezone.utc), lat=-35.4, lon=-71.9, magnitude=6.8, sources=["USGS M6.8"])
                await app.process("usgs", [ev])
                await asyncio.gather(*list(app._bg))
                app.store.add_digest("x", "info", "🔵 <b>Something</b>", held=False)
                await app.send_digest()
                return app
    app = asyncio.run(go())
    assert "CRITICAL" in sent[0]
    assert sent[1].startswith("ℹ️ <b>Context · #E1</b>")
    search_calls = [c for c in claude.calls if any(t.get("name") == "web_search" for t in c.get("tools", []))]
    assert search_calls and search_calls[0]["tools"][0]["allowed_callers"] == ["direct"]
    assert "<i>Quiet night overall.</i>" in sent[2]
