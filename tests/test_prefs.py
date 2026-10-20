"""v0.5.0 plain-text preferences: proposal → Apply → effect → /prefs → Undo; safety rules."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import respx

from iris import policy
from iris.app import Iris
from iris.models import Event
from iris.store import Store
from test_news import cfg  # noqa: F401

NOW = datetime.now(timezone.utc)
YELLOW_MILAN = Event(key="dpc:milan:d:thunderstorm", kind="warning", title="Yellow alert: thunderstorms — Milan",
                     time=NOW, fixed_levels={"milan": "info"}, ongoing=True, details={})
WIND_TALCA = Event(key="om:talca:d:gust", kind="forecast", title="Forecast: wind gusts 95 km/h in Talca",
                   time=NOW, fixed_levels={"talca": "info"}, ongoing=True, details={})
GDACS_RED = Event(key="gdacs:TC:1", kind="gdacs", title="Red tropical cyclone — Philippines", time=NOW, lat=14, lon=125,
                  ongoing=True, details={"alertlevel": "red", "eventtype": "TC"})


def run(cfg, answers, steps, store=None):
    """answers: list of record_settings tool inputs returned by the fake Haiku, in order."""
    sent, edits, claude_calls = [], [], []

    def claude(req):
        body = json.loads(req.content)
        claude_calls.append(body)
        out = answers.pop(0)
        return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "x", "name": "record_settings",
                                                      "input": out}], "stop_reason": "tool_use",
                                         "usage": {"input_tokens": 500, "output_tokens": 60}})

    def tg(req):
        method = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content)
        (sent if method == "sendMessage" else edits).append(body)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    async def go():
        with respx.mock(assert_all_called=False) as m:
            m.post("https://api.anthropic.com/v1/messages").mock(side_effect=claude)
            m.post(url__regex=r"https://api.telegram.org/.*").mock(side_effect=tg)
            m.get("https://nominatim.openstreetmap.org/search").mock(return_value=httpx.Response(200, json=[
                {"lat": "41.9028", "lon": "12.4964", "display_name": "Roma, Lazio, Italia"}]))
            m.get("https://nominatim.openstreetmap.org/reverse").mock(return_value=httpx.Response(200, json={
                "address": {"city": "Roma", "country_code": "it", "state": "Lazio", "county": "Roma Capitale"}}))
            m.get("https://api.open-meteo.com/v1/forecast").mock(return_value=httpx.Response(200, json={"timezone": "Europe/Rome"}))
            async with httpx.AsyncClient() as c:
                app = Iris(cfg, store=store or Store(":memory:"), client=c)
                out = {}
                for s in steps:
                    await s(app, out)
                return app, out
    app, out = asyncio.run(go())
    return app, out, sent, edits, claude_calls


async def write(app, text, out, key):
    out[key] = await app.handle(text)


async def tap(app, data):
    await app.on_button({"id": "q", "data": data, "message": {"message_id": 7, "chat": {"id": 42}}})


def pid_of(reply):
    return next(b["callback_data"] for row in reply[1]["inline_keyboard"] for b in row if b["callback_data"].startswith("p:a:"))


def test_more_weather_in_milan(cfg):
    ans = [{"is_settings_request": True, "changes": [{"type": "sensitivity", "category": "weather", "area": "home", "shift": "more"}]}]

    async def steps(app, out):
        assert policy.evaluate(YELLOW_MILAN, app.cfg)[0] == "info"
        await write(app, "quiero seguir el clima más de cerca en Milán", out, "proposal")
        await tap(app, pid_of(out["proposal"]))
        out["milan"] = policy.evaluate(YELLOW_MILAN, app.cfg)
        out["talca"] = policy.evaluate(WIND_TALCA, app.cfg)[0]
        out["listing"] = await app.handle("/prefs")
        undo = next(b["callback_data"] for row in out["listing"][1]["inline_keyboard"] for b in row if b["callback_data"].startswith("p:u:"))
        await tap(app, undo)
        out["after_undo"] = policy.evaluate(YELLOW_MILAN, app.cfg)[0]
    app, out, sent, edits, calls = run(cfg, ans, [steps])
    text, kb = out["proposal"]
    assert "Proposed change" in text and "Weather &amp; warnings" in text and "more sensitive" in text
    assert "CRITICAL alerts are never changed" in text
    assert "Applied" in edits[0]["text"]
    assert out["milan"][0] == "high" and "your setting" in out["milan"][1]     # yellow Milan warning now alerts
    assert out["talca"] == "info"                                                 # Chile unaffected
    assert "Weather" in out["listing"][0] and out["after_undo"] == "info"
    # the AI got the menu of allowed knobs and current values
    assert "rain_mm" in calls[0]["system"][0]["text"] and "home" in calls[0]["system"][0]["text"]


def test_mute_world_but_critical_untouched(cfg):
    ans = [{"is_settings_request": True, "changes": [{"type": "sensitivity", "category": "all", "area": "world", "shift": "mute"}]}]

    async def steps(app, out):
        await write(app, "no world news please", out, "p")
        await tap(app, pid_of(out["p"]))
        out["gdacs"] = policy.evaluate(GDACS_RED, app.cfg)[0]
        quake = Event(key="quake:1", kind="quake", title="M6.3 — near Milan", time=NOW, lat=45.5, lon=9.2, magnitude=6.3)
        out["quake"] = policy.evaluate(quake, app.cfg)[0]
    app, out, *_ = run(cfg, ans, [steps])
    assert out["gdacs"] == "none" and out["quake"] == "critical"


def test_threshold_quiet_hours_and_validation(cfg):
    ans = [{"is_settings_request": True, "changes": [
        {"type": "threshold", "knob": "rain_mm", "area": "home", "value": 30},
        {"type": "threshold", "knob": "rain_mm", "area": "home", "value": 9999},         # out of range → dropped
        {"type": "threshold", "knob": "delete_everything", "value": 1},                   # not a knob → dropped
        {"type": "quiet_hours", "start": "00:00", "end": "08:00"},
        {"type": "digest_time", "time": "08:15"}]}]

    async def steps(app, out):
        await write(app, "rain alerts above 30 mm, quiet hours midnight to 8, digest at 8:15", out, "p")
        await tap(app, pid_of(out["p"]))
    app, out, *_ = run(cfg, ans, [steps])
    text = out["p"][0]
    assert text.count("• ") == 3 and "50 → <b>30</b> mm/day" in text and "00:00–08:00" in text
    assert app.cfg.raw["profiles"]["home"]["forecast"]["rain_mm"] == 30
    assert app.cfg.raw["profiles"]["watch"]["forecast"]["rain_mm"] == 60                # Chile untouched
    assert app.cfg.get("quiet_hours") == {"start": "00:00", "end": "08:00"} and app.cfg.get("digest_time") == "08:15"


def test_temporary_setting_expires(cfg):
    ans = [{"is_settings_request": True, "changes": [{"type": "sensitivity", "category": "weather", "area": "home",
                                                       "shift": "more", "days": 1}]}]

    async def steps(app, out):
        await write(app, "follow the weather closely today", out, "p")
        await tap(app, pid_of(out["p"]))
        out["during"] = policy.evaluate(YELLOW_MILAN, app.cfg)[0]
        prefs = app.store.kv_get("prefs")
        prefs[0]["expires"] = time.time() - 1
        app.store.kv_set("prefs", prefs)
        out["ended"] = app.prefs.expire()
        out["after"] = policy.evaluate(YELLOW_MILAN, app.cfg)[0]
    app, out, *_ = run(cfg, ans, [steps])
    assert "temporary, until" in out["p"][0]
    assert out["during"] == "high" and out["after"] == "info" and len(out["ended"]) == 1


def test_add_rome_remove_talca_and_persistence(cfg):
    ans = [{"is_settings_request": True, "changes": [{"type": "add_place", "query": "Rome", "profile": "watch"},
                                                      {"type": "remove_place", "place_id": "talca"}]}]
    store = Store(":memory:")

    async def steps(app, out):
        await write(app, "also watch Rome, and stop watching Talca", out, "p")
        await tap(app, pid_of(out["p"]))
        out["ids"] = [l.id for l in app.cfg.locations]
        out["rome"] = app.cfg.location("p_roma")
    app, out, *_ = run(cfg, ans, [steps], store=store)
    assert "Also watch <b>Roma</b>" in out["p"][0] and "Stop watching <b>Talca</b>" in out["p"][0]
    assert "talca" not in out["ids"] and "p_roma" in out["ids"]
    assert out["rome"].comune == "Roma" and out["rome"].region == "Lazio" and out["rome"].tz == "Europe/Rome"

    async def check(app, out):                                    # restart: settings come back from the DB
        out["ids"] = [l.id for l in app.cfg.locations]
    from test_news import load_config  # noqa: F401
    app2, out2, *_ = run(cfg, [], [check], store=store)
    assert "p_roma" in out2["ids"] and "talca" not in out2["ids"]


def test_not_a_setting_and_unverified_news_never_promoted(cfg):
    ans = [{"is_settings_request": False, "changes": []},
           {"is_settings_request": True, "changes": [{"type": "sensitivity", "category": "crime", "area": "home", "shift": "more"}]}]

    async def steps(app, out):
        await write(app, "what's happening in Talca?", out, "q")
        await write(app, "more crime alerts in Milan", out, "p")
        await tap(app, pid_of(out["p"]))
        news = lambda verified: Event(key="news:1", kind="news", title="Robbery", time=NOW, lat=45.47, lon=9.19,
                                      details={"severity": 2, "category": "crime", "country": "IT", "verified": verified,
                                               "precision": "street"})
        out["confirmed"] = policy.evaluate(news("confirmed"), app.cfg)[0]
        out["unconfirmed"] = policy.evaluate(news(None), app.cfg)[0]
    app, out, *_ = run(cfg, ans, [steps])
    assert "settings" in out["q"] and "Questions come later" in out["q"]
    assert out["confirmed"] == "high" and out["unconfirmed"] == "info"
