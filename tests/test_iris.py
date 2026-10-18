import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from iris import policy
from iris.app import Iris
from iris.collectors import feeds, quakes
from iris.config import load_config
from iris.models import Event
from iris.store import Store

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "defaults.yaml").write_text((ROOT / "config" / "defaults.yaml").read_text())
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=T\nTELEGRAM_CHAT_ID=42\n")
    c = load_config(tmp_path)
    c.raw["quiet_hours"] = {}           # tests decide quiet hours explicitly
    return c


def usgs_doc(mag, lat, lon, t=NOW, id_="us1"):
    return {"features": [{"id": id_, "properties": {"mag": mag, "place": "near Talca, Chile", "time": t.timestamp() * 1000,
                                                    "url": "https://usgs/x", "type": "earthquake"},
                          "geometry": {"coordinates": [lon, lat, 30.0]}}]}


def emsc_doc(mag, lat, lon, t=NOW, uid="20261017_0001"):
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "id": uid, "geometry": {"type": "Point", "coordinates": [lon, lat, -30]},
            "properties": {"unid": uid, "time": iso(t), "lat": lat, "lon": lon, "depth": 30, "mag": mag,
                           "flynn_region": "CENTRAL CHILE", "evtype": "ke"}}]}


INGV_TEXT = """#EventID|Time|Latitude|Longitude|Depth/Km|Author|Catalog|Contributor|ContributorID|MagType|Magnitude|MagAuthor|EventLocationName|EventType
4410001|{t}|45.40|9.30|10.2|SURVEY-INGV||||ML|3.9|--|5 km SE Milano (MI)|earthquake
"""

GDACS_XML = """<?xml version="1.0"?><rss xmlns:gdacs="http://www.gdacs.org" xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#" version="2.0"><channel>
<item><title>Red alert for tropical cyclone BIG-26</title><link>https://gdacs.org/1</link><pubDate>Sat, 17 Oct 2026 00:38:00 GMT</pubDate>
<gdacs:alertlevel>Red</gdacs:alertlevel><gdacs:eventtype>TC</gdacs:eventtype><gdacs:eventid>1001</gdacs:eventid><gdacs:iscurrent>true</gdacs:iscurrent>
<gdacs:severity>Category 5, 260 km/h</gdacs:severity><gdacs:country>Philippines</gdacs:country><geo:Point><geo:lat>14.0</geo:lat><geo:long>125.0</geo:long></geo:Point></item>
<item><title>Orange flood</title><link>https://gdacs.org/2</link><gdacs:alertlevel>Orange</gdacs:alertlevel><gdacs:eventtype>FL</gdacs:eventtype>
<gdacs:eventid>2002</gdacs:eventid><gdacs:iscurrent>true</gdacs:iscurrent><geo:Point><geo:lat>-35.0</geo:lat><geo:long>-71.5</geo:long></geo:Point></item>
<item><title>Green quake</title><gdacs:alertlevel>Green</gdacs:alertlevel><gdacs:eventtype>EQ</gdacs:eventtype><gdacs:eventid>3003</gdacs:eventid>
<geo:Point><geo:lat>0</geo:lat><geo:long>0</geo:long></geo:Point></item>
</channel></rss>"""

MA_XML = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
<entry><title>x</title><link href="https://meteoalarm.org/a"/><cap:areaDesc>Lombardia</cap:areaDesc><cap:event>Orange Thunderstorm Warning</cap:event>
<cap:severity>Severe</cap:severity><cap:onset>{onset}</cap:onset><cap:expires>{exp}</cap:expires><cap:status>Actual</cap:status><cap:message_type>Alert</cap:message_type></entry>
<entry><cap:areaDesc>Sardegna</cap:areaDesc><cap:event>Red Wind Warning</cap:event><cap:severity>Extreme</cap:severity>
<cap:onset>{onset}</cap:onset><cap:expires>{exp}</cap:expires><cap:status>Actual</cap:status><cap:message_type>Alert</cap:message_type></entry>
</feed>"""

PTWC_XML = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#">
<title>TSUNAMI THREAT MESSAGE   NUMBER 1</title>
<entry><title>NEAR COAST OF CENTRAL CHILE</title><id>urn:ptwc:1</id><updated>{t}</updated><geo:lat>-35.5</geo:lat><geo:long>-72.9</geo:long>
<summary type="xhtml"><div>Preliminary magnitude 8.1 (Mwp). HAZARDOUS TSUNAMI WAVES ARE FORECAST for coasts of CHILE and PERU.</div></summary>
<link rel="alternate" href="https://tsunami.gov/x"/></entry></feed>"""


# ---------------- parsers ----------------
def test_parsers():
    assert quakes.parse_usgs(usgs_doc(5.0, -35, -71))[0].mag == 5.0
    e = quakes.parse_emsc(emsc_doc(5.2, -35, -71))[0]
    assert e.src == "EMSC" and e.lat == -35
    i = quakes.parse_ingv(INGV_TEXT.format(t=NOW.strftime("%Y-%m-%dT%H:%M:%S.000000")))
    assert i[0].mag == 3.9 and "Milano" in i[0].place
    g = feeds.parse_gdacs(GDACS_XML)
    assert {x.details["alertlevel"] for x in g} == {"red", "orange", "green"}
    ma = feeds.parse_meteoalarm(MA_XML.format(onset=iso(NOW), exp=iso(NOW + timedelta(hours=10))), ["Lombardia"], "milan")
    assert len(ma) == 1 and ma[0].fixed_levels == {"milan": "high"} and ma[0].title.startswith("Orange Thunderstorm")
    p = feeds.parse_ptwc(PTWC_XML.format(t=iso(NOW)))
    assert p[0].details["threat"] and p[0].details["mentions_chile"] and p[0].magnitude == 8.1


# ---------------- policy ----------------
def q(mag, lat, lon, hours_ago=0):
    return Event(key="k", kind="quake", title="q", time=NOW - timedelta(hours=hours_ago), lat=lat, lon=lon, magnitude=mag)


def test_policy_quakes(cfg):
    assert policy.evaluate(q(3.9, 45.40, 9.30), cfg)[0] == "high"              # near Milan
    assert policy.evaluate(q(2.6, 45.40, 9.30), cfg)[0] == "info"
    assert policy.evaluate(q(4.2, -35.3, -71.6), cfg)[0] == "none"             # small, Talca (watch)
    assert policy.evaluate(q(5.8, -35.3, -71.6), cfg)[0] == "high"
    lvl, why = policy.evaluate(q(6.8, -35.3, -71.6), cfg)
    assert lvl == "critical" and "Talca" in why
    assert policy.evaluate(q(7.2, 38.0, 142.0), cfg)[0] == "high"              # Japan, world
    assert policy.evaluate(q(6.0, 38.0, 142.0), cfg)[0] == "none"
    assert policy.evaluate(q(6.8, -35.3, -71.6, hours_ago=12), cfg)[0] == "none"   # old


def test_policy_gdacs_tsunami(cfg):
    g = {e.key: e for e in feeds.parse_gdacs(GDACS_XML)}
    levels = {k: policy.evaluate(e, cfg)[0] for k, e in g.items()}
    assert levels["gdacs:TC:1001"] == "high"         # red far away: high, not critical
    assert levels["gdacs:FL:2002"] == "high"         # orange near Talca
    assert levels["gdacs:EQ:3003"] == "none"
    p = feeds.parse_ptwc(PTWC_XML.format(t=iso(NOW)))[0]
    lvl, why = policy.evaluate(p, cfg)
    assert lvl == "high" and "inland" in why             # Santiago/Talca are inland: no 3 a.m. siren
    from iris.config import Location
    cfg.locations.append(Location("valpo", "Valparaíso", -33.05, -71.62, "America/Santiago", "CL", "watch", coastal=True))
    assert policy.evaluate(p, cfg)[0] == "critical"     # a coastal place is in danger


def test_quiet_hours(cfg):
    cfg.raw["quiet_hours"] = {"start": "23:00", "end": "07:30"}
    tz = cfg.tz
    assert policy.in_quiet_hours(cfg, datetime(2026, 10, 17, 23, 30, tzinfo=tz))
    assert policy.in_quiet_hours(cfg, datetime(2026, 10, 18, 7, 0, tzinfo=tz))
    assert not policy.in_quiet_hours(cfg, datetime(2026, 10, 18, 7, 31, tzinfo=tz))
    assert not policy.in_quiet_hours(cfg, datetime(2026, 10, 18, 14, 0, tzinfo=tz))


def test_forecast(cfg):
    loc = cfg.location("talca")
    data = {"daily": {"time": ["2026-10-18", "2026-10-19"], "precipitation_sum": [5, 75],
                      "wind_gusts_10m_max": [40, 95], "temperature_2m_max": [20, 22], "temperature_2m_min": [5, 6]}}
    evs = feeds.evaluate_forecast(data, loc, cfg.profile("watch"))
    assert {e.details["metric"] for e in evs} == {"rain", "gust"}
    assert all(e.fixed_levels == {"talca": "info"} for e in evs)   # SENAPRED covers official alerts


# ---------------- end to end ----------------
def run(coro):
    return asyncio.run(coro)


def test_end_to_end(cfg):
    sent = []

    async def scenario():
        with respx.mock(assert_all_called=False) as m:
            m.post(url__regex=r"https://api.telegram.org/botT/sendMessage").mock(
                side_effect=lambda req: (sent.append(json.loads(req.content)), httpx.Response(200, json={"ok": True, "result": {}}))[1])
            usgs = m.get(quakes.USGS_URL)
            emsc = m.get(url__startswith=quakes.EMSC_URL)
            async with httpx.AsyncClient() as client:
                app = Iris(cfg, store=Store(":memory:"), client=client)
                # Baseline run: an old-ish but recent quake exists at install -> no alert, goes to digest
                usgs.mock(return_value=httpx.Response(200, json=usgs_doc(6.0, -35.3, -71.7, id_="old")))
                await app.process("usgs", await app.source_jobs()["usgs"]())
                assert sent == []
                app.store.kv_set("baseline:emsc", True)
                # New M5.9 near Talca reported by USGS, then EMSC (same quake) -> ONE alert
                usgs.mock(return_value=httpx.Response(200, json=usgs_doc(5.9, -35.40, -71.90, t=NOW, id_="new")))
                await app.process("usgs", await app.source_jobs()["usgs"]())
                emsc.mock(return_value=httpx.Response(200, json=emsc_doc(5.8, -35.45, -71.85, t=NOW + timedelta(seconds=20))))
                await app.process("emsc", await app.source_jobs()["emsc"]())
                assert len(sent) == 1 and "HIGH" in sent[0]["text"] and "Talca" in sent[0]["text"]
                # Magnitude revised to 6.7 -> escalates to CRITICAL as an UPDATE
                usgs.mock(return_value=httpx.Response(200, json=usgs_doc(6.7, -35.40, -71.90, t=NOW, id_="new")))
                await app.process("usgs", await app.source_jobs()["usgs"]())
                assert len(sent) == 2 and "CRITICAL" in sent[1]["text"] and "UPDATE" in sent[1]["text"]
                # Same again -> nothing new
                await app.process("usgs", await app.source_jobs()["usgs"]())
                assert len(sent) == 2
                # Quiet hours: HIGH is held for the digest, CRITICAL still goes through
                cfg.raw["quiet_hours"] = {"start": "00:00", "end": "23:59"}
                app.store.kv_set("baseline:meteoalarm", True)
                await app.process("meteoalarm", feeds.parse_meteoalarm(
                    MA_XML.format(onset=iso(NOW), exp=iso(NOW + timedelta(hours=8))), ["Lombardia"], "milan"))
                assert len(sent) == 2
                digest = app.build_digest()
                assert "Orange Thunderstorm" in digest and "Held overnight" in digest and "active when Iris started" in digest
                # Commands
                text, kb = await app.handle("/recent")
                assert "/e" in text and kb["inline_keyboard"]
                assert "Thunderstorm" in await app.handle("/event 3") or "Usage" not in await app.handle("/event 1")
                assert "Muted" in await app.handle("/mute 30m")

    run(scenario())
