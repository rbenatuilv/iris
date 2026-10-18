"""Live location: Telegram share → 'you are here' place; trips; timezone; expiry; near home."""
import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import respx

from iris import policy
from iris.app import Iris
from iris.models import Event
from iris.store import Store
from test_news import cfg  # noqa: F401

PLACES = {  # (lat, lon) -> reverse geocode answer
    "lisbon": {"address": {"city": "Lisboa", "country_code": "pt"}},
    "milan": {"address": {"city": "Milano", "country_code": "it"}},
    "bergamo": {"address": {"city": "Bergamo", "country_code": "it"}},
}


def run(cfg, steps):
    sent, geo_calls = [], []

    def reverse(req):
        lat = float(req.url.params["lat"])
        geo_calls.append(lat)
        key = "lisbon" if lat < 40 else ("bergamo" if lat > 45.6 else "milan")
        return httpx.Response(200, json=PLACES[key])

    def tz(req):
        lat = float(req.url.params["latitude"])
        return httpx.Response(200, json={"timezone": "Europe/Lisbon" if lat < 40 else "Europe/Rome"})

    async def go():
        with respx.mock(assert_all_called=False) as m:
            m.get("https://nominatim.openstreetmap.org/reverse").mock(side_effect=reverse)
            m.get("https://nominatim.openstreetmap.org/search").mock(return_value=httpx.Response(200, json=[
                {"lat": "38.7223", "lon": "-9.1393", "display_name": "Lisboa, Portugal"}]))
            m.get("https://api.open-meteo.com/v1/forecast").mock(side_effect=tz)
            m.post(url__regex=r".*/sendMessage").mock(side_effect=lambda r: (sent.append(json.loads(r.content)["text"]),
                                                      httpx.Response(200, json={"ok": True, "result": {}}))[1])
            async with httpx.AsyncClient() as c:
                app = Iris(cfg, store=Store(":memory:"), client=c)
                for s in steps:
                    await s(app)
                return app
    return asyncio.run(go()), sent, geo_calls


def live(lat, lon, period=0x7FFFFFFF):
    return {"date": int(time.time()), "location": {"latitude": lat, "longitude": lon, "live_period": period}}


def test_live_location_abroad(cfg):
    out = {}

    async def share(app):
        out["msg"] = await app.here.on_telegram_location(live(38.7223, -9.1393))

    async def move_a_bit(app):
        out["msg2"] = await app.here.on_telegram_location(live(38.7300, -9.1400))     # < 3 km: no new lookup

    async def quake(app):
        ev = Event(key="quake:9", kind="quake", title="M4.6 earthquake — off Portugal", time=datetime.now(timezone.utc),
                   lat=38.9, lon=-9.6, magnitude=4.6, sources=["EMSC M4.6"])
        out["eval"] = policy.evaluate(ev, app.cfg)
    app, sent, geo = run(cfg, [share, move_a_bit, quake])
    assert "Lisboa, PT" in out["msg"] and "until you stop sharing" in out["msg"]
    assert out["msg2"] is None and len(geo) == 1
    here = app.cfg.location("here")
    assert here and here.name == "Lisboa (you)" and here.country == "PT" and here.comune is None
    assert str(app.cfg.tz) == "Europe/Lisbon"                       # quiet hours follow you
    lvl, why = out["eval"]
    assert lvl == "high" and "Lisboa (you)" in why                  # home profile: M4.6 at ~45 km → HIGH


def test_near_home_is_quiet_and_trip_and_off(cfg):
    out = {}

    async def at_home(app):
        out["home"] = await app.here.on_telegram_location(live(45.47, 9.19))
        out["loc_home"] = app.cfg.location("here")

    async def trip(app):
        out["trip"] = await app.handle("/trip Lisbon 3")
        out["loc_trip"] = app.cfg.location("here")
        out["here"] = await app.handle("/here")

    async def off(app):
        out["off"] = await app.handle("/here off")
        out["loc_off"] = app.cfg.location("here")
    app, sent, _ = run(cfg, [at_home, trip, off])
    assert "near Milan" in out["home"] and out["loc_home"] is None    # no duplicate of Milan
    assert "Lisboa" in out["trip"] and out["loc_trip"].name == "Lisboa (you)"
    assert "trip" in out["here"] and "Europe/Lisbon" in out["here"]
    assert out["loc_off"] is None and str(app.cfg.tz) == "Europe/Rome"


def test_italian_town_gets_civil_protection_zone_and_expiry(cfg):
    out = {}

    async def bergamo(app):
        await app.here.on_telegram_location(live(45.698, 9.677, period=3600))
        out["loc"] = app.cfg.location("here")

    async def later(app):
        s = app.store.kv_get("here")
        s["expires"] = time.time() - 1
        app.store.kv_set("here", s)
        out["note"] = await app.here.expire_check()
        out["after"] = app.cfg.location("here")
    app, sent, _ = run(cfg, [bergamo, later])
    assert out["loc"].comune == "Bergamo" and out["loc"].country == "IT"
    assert "Stopped watching Bergamo" in out["note"] and out["after"] is None
