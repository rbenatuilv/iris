"""v0.4.0 UI: Recent ordering/grouping/filters, menu, alert buttons, muted stories, feedback."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import respx

from iris import ui
from iris.app import Iris
from iris.models import Event
from iris.store import Store
from test_news import cfg  # noqa: F401

NOW = datetime.now(timezone.utc)


def ev(key, kind, title, hours_ago, lat=None, lon=None, fixed=None, details=None, mag=None, ongoing=False):
    return Event(key=key, kind=kind, title=title, time=NOW - timedelta(hours=hours_ago), lat=lat, lon=lon,
                 fixed_levels=fixed, details=details or {}, magnitude=mag, ongoing=ongoing, sources=["x"])


def harness(cfg, steps):
    calls = []

    def tg(req):
        method = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content and req.headers.get("content-type", "").startswith("application/json") else {}
        calls.append((method, body))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    async def go():
        with respx.mock(assert_all_called=False) as m:
            m.post(url__regex=r"https://api.telegram.org/.*").mock(side_effect=tg)
            async with httpx.AsyncClient() as c:
                app = Iris(cfg, store=Store(":memory:"), client=c)
                cfg.raw["quiet_hours"] = {}
                for s in ("usgs", "dpc", "news", "gdacs", "strikes"):
                    app.store.kv_set(f"baseline:{s}", True)
                out = {}
                for s in steps:
                    await s(app, out)
                return app, out
    app, out = asyncio.run(go())
    return app, out, calls


EVENTS = [
    ev("quake:1", "quake", "M3.9 earthquake — near Milan", 2, 45.40, 9.30, mag=3.9, details={"depth_km": 10}),
    ev("dpc:milan:x:thunderstorm", "warning", "Orange alert: severe thunderstorms — Milan", 5,
       fixed={"milan": "high"}, ongoing=True, details={"expires": (NOW + timedelta(hours=10)).isoformat()}),
    ev("news:7", "news", "Armed robbery in via Padova", 1, 45.50, 9.22,
       details={"severity": 2, "category": "crime", "country": "IT", "verified": "confirmed", "precision": "street"}),
    ev("quake:2", "quake", "M5.8 earthquake — Maule, Chile", 3, -35.40, -71.90, mag=5.8, details={"depth_km": 30}),
    ev("gdacs:TC:9", "gdacs", "Red tropical cyclone ZETA — Philippines", 60, 14.0, 125.0, ongoing=True,
       details={"alertlevel": "red", "eventtype": "TC"}),
    ev("quake:3", "quake", "M7.2 earthquake — Japan", 1, 38.0, 142.0, mag=7.2, details={}),     # aged below
]


async def load(app, out):
    for e in EVENTS:
        src = e.key.split(":")[0]
        await app.process({"quake": "usgs", "dpc": "dpc", "news": "news", "gdacs": "gdacs"}[src], [e])
    # the Japan quake happened 30 h ago (seen when fresh, now old)
    app.store.db.execute("UPDATE events SET t=? WHERE key='quake:3'", (time.time() - 30 * 3600,))


def test_recent_groups_orders_and_filters(cfg):
    async def view(app, out):
        out["all"], out["kb"] = ui.render_recent(app, ui.parse_recent_args("", cfg))
        out["crime"], _ = ui.render_recent(app, ui.parse_recent_args("crime", cfg))
        out["chile"], _ = ui.render_recent(app, ui.parse_recent_args("chile quakes", cfg))
        out["alerts3d"], _ = ui.render_recent(app, ui.parse_recent_args("3d alerts", cfg))
        out["world7d"], _ = ui.render_recent(app, ui.parse_recent_args("world 7d", cfg))
    app, out, _ = harness(cfg, [load, view])
    text = out["all"]
    # groups in order: Milan & you, Chile, World
    assert text.index("Milan &amp; near you") < text.index("Santiago") < text.index("World &amp; elsewhere")
    # inside Milan: more serious first (HIGH thunderstorm + quake before the INFO robbery), then newest
    milan = text[text.index("Milan &amp; near you"):text.index("Santiago")]
    assert milan.index("thunderstorms") < milan.index("robbery") and milan.index("M3.9") < milan.index("robbery")
    # 24 h: the 30 h old Japan quake is out; the 60 h old cyclone is ongoing → still listed
    assert "Japan" not in text and "ZETA" in text
    assert "Armed robbery" in out["crime"] and "M3.9" not in out["crime"] and "🚨" in out["crime"]
    assert "Maule" in out["chile"] and "Milan" not in out["chile"].split("\n", 1)[1]
    assert "robbery" not in out["alerts3d"] and "Japan" in out["alerts3d"]           # Info hidden, 3 days
    assert "Japan" in out["world7d"] and "Maule" not in out["world7d"]
    # keyboard: time / place / level rows + category + menu
    labels = [b["text"] for row in out["kb"]["inline_keyboard"] for b in row]
    assert "✓ 24h" in labels and "✓ All" in labels and any("Category" in l for l in labels) and "⬅️ Menu" in labels
    assert all(len(b["callback_data"].encode()) <= 64 for row in out["kb"]["inline_keyboard"] for b in row)


def test_reorder_bug_is_fixed(cfg):
    """An old event that keeps being re-checked must not jump to the top or into a 6 h list."""
    async def recheck(app, out):
        old = ev("quake:50", "quake", "M4.1 earthquake — near Milan", 20, 45.40, 9.30, mag=4.1)
        await app.process("usgs", [old])
        app.store.db.execute("UPDATE events SET last_update=? WHERE key='quake:50'", (time.time(),))
        out["6h"], _ = ui.render_recent(app, ui.parse_recent_args("6h", cfg))
    app, out, _ = harness(cfg, [load, recheck])
    assert "M4.1" not in out["6h"] and "M3.9" in out["6h"]


def test_alert_buttons_mute_and_feedback(cfg):
    async def steps(app, out):
        e = ev("news:7", "news", "Armed robbery in via Padova", 0.2, 45.50, 9.22,
               details={"severity": 3, "category": "crime", "country": "IT", "verified": "confirmed", "precision": "street"})
        await app.process("news", [e])
        eid = app.store.get_event("news:7")["id"]
        msg = {"message_id": 5, "chat": {"id": 42}}
        await app.on_button({"id": "q1", "data": f"f:{eid}:0", "message": msg})
        await app.on_button({"id": "q2", "data": f"m:{eid}", "message": msg})
        e.details["severity"] = 4                                          # escalation of a muted story
        await app.process("news", [e])
        await app.on_button({"id": "q3", "data": "r:72:all:crime:h", "message": msg})
        await app.on_button({"id": "q4", "data": "r:72:all:crime:h", "message": {"message_id": 5, "chat": {"id": 666}}})
        out["fb"] = app.store.db.execute("SELECT useful, kind FROM feedback").fetchall()
    app, out, calls = harness(cfg, [steps])
    sends = [b for m, b in calls if m == "sendMessage"]
    alert = next(b for b in sends if "Armed robbery" in b["text"])
    texts = [b["text"] for row in alert["reply_markup"]["inline_keyboard"] for b in row]
    assert texts == ["ℹ️ Details", "🔕 Mute story", "👍 Useful", "👎 Not useful"]
    assert len([b for b in sends if "Armed robbery" in b["text"]]) == 1    # muted: escalation not sent
    assert [tuple(r) for r in out["fb"]] == [(0, "news")]
    edits = [b for m, b in calls if m == "editMessageText"]
    assert len(edits) == 1 and "Crime &amp; security" in edits[0]["text"]   # stranger's tap ignored
    assert "🔕" in edits[0]["text"] and "Armed robbery" in edits[0]["text"]  # muted stays listed, marked
    assert sum(1 for m, _ in calls if m == "answerCallbackQuery") == 4


def test_menu_and_commands(cfg):
    async def steps(app, out):
        out["menu"] = await app.handle("/menu")
        out["e"] = await app.handle("/e1")
        await app.tg.set_commands(ui.COMMANDS)
    app, out, calls = harness(cfg, [load, steps])
    text, kb = out["menu"]
    assert "Iris" in text and any("Recent" in b["text"] for row in kb["inline_keyboard"] for b in row)
    assert out["e"].startswith("<b>#E1 ")
    cmds = next(b for m, b in calls if m == "setMyCommands")["commands"]
    assert {"command": "recent", "description": ui.COMMANDS[1][1]} in cmds
