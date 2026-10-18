"""DPC zone warnings (Italy), SENAPRED official channel (Chile), demoted Meteoalarm."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import respx

from iris import policy
from iris.app import Iris
from iris.collectors import dpc, feeds
from iris.news import parse_telegram_channel
from iris.store import Store
from test_iris import MA_XML, iso
from test_news import FakeClaude, cfg, rss  # noqa: F401

FX = Path(__file__).parent / "fixtures"
TODAY = datetime.now(timezone.utc).strftime("%Y%m%d")


def test_dpc_zone_for_milan(cfg):
    topo = json.loads((FX / "dpc_today.json").read_text())
    zone, levels = dpc.zone_levels(topo, "Milano")
    assert zone == "Nodo Idraulico di Milano"
    day = datetime.now().strftime("%Y-%m-%d")
    evs = dpc.events_for(day, topo, cfg.locations, "Bollettino test")
    by_risk = {e.details["risk"]: e for e in evs}
    assert set(by_risk) == {"thunderstorm", "landslide"}               # flood is green → nothing
    assert by_risk["thunderstorm"].fixed_levels == {"milan": "high"}   # orange → HIGH
    assert by_risk["landslide"].fixed_levels == {"milan": "info"}      # yellow → digest
    assert "Nodo Idraulico di Milano" in by_risk["thunderstorm"].title
    lvl, why = policy.evaluate(by_risk["thunderstorm"], cfg)
    assert lvl == "high" and "Milan" in why
    # the red zone elsewhere in Italy is not ours
    assert all("Tordino" not in e.title for e in evs)


def test_dpc_fetch_uses_latest_bulletin_and_caches(cfg):
    bulletin = json.loads((FX / "dpc_bulletin.json").read_text())
    name = f"files/{TODAY}_1458.json"
    topo = json.loads((FX / "dpc_today.json").read_text())
    calls = {"topo": 0}

    def topo_resp(req):
        calls["topo"] += 1
        return httpx.Response(200, json=topo)

    async def go():
        with respx.mock(assert_all_called=False) as m:
            m.get(f"{dpc.API}/commits").mock(return_value=httpx.Response(200, json=[{"sha": "abc"}]))
            m.get(f"{dpc.API}/commits/abc").mock(return_value=httpx.Response(200, json={"files": [
                {"filename": f"files/topojson/{TODAY}_1458_today.json"}, {"filename": name}]}))
            m.get(dpc.RAW + name).mock(return_value=httpx.Response(200, json=bulletin))
            m.get(url__regex=r".*topojson/.*_(today|tomorrow)\.json").mock(side_effect=topo_resp)
            async with httpx.AsyncClient() as c:
                s = Store(":memory:")
                a = await dpc.fetch_dpc(c, cfg, s)
                b = await dpc.fetch_dpc(c, cfg, s)       # same bulletin: no new downloads
                return a, b
    a, b = asyncio.run(go())
    assert calls["topo"] == 2                             # today + tomorrow, once
    assert {e.key for e in a} == {e.key for e in b} and len(a) == 4   # 2 risks × 2 days


def test_meteoalarm_italy_is_digest_only_and_skips_thunderstorms(cfg):
    xml = MA_XML.format(onset=iso(datetime.now(timezone.utc)), exp=iso(datetime.now(timezone.utc) + timedelta(hours=8)))
    evs = feeds.parse_meteoalarm(xml, ["Lombardia"], "milan", "info", ["thunderstorm", "rain", "flood"])
    assert evs == []                                      # the orange thunderstorm is DPC's job now
    wind = xml.replace("Orange Thunderstorm Warning", "Orange Wind Warning")
    evs = feeds.parse_meteoalarm(wind, ["Lombardia"], "milan", "info", ["thunderstorm", "rain", "flood"])
    assert len(evs) == 1 and evs[0].fixed_levels == {"milan": "info"}


def test_parse_senapred_channel():
    html = (FX / "senapred.html").read_text().replace("TIME1", "2026-10-13T21:49:48+00:00").replace("TIME2", "2026-10-14T00:38:10+00:00")
    posts = parse_telegram_channel(html, "SenapredChile")
    assert len(posts) == 2                                # the video post without time is skipped
    p = posts[0]
    assert p["url"] == "https://t.me/SenapredChile/1629" and p["domain"] == "t.me/SenapredChile"
    assert "evacuar campamento" in p["summary"] and "#Talagante" in p["summary"] and "\n" in p["summary"]
    assert p["title"].startswith("¡ATENCIÓN! Por desborde")


def test_official_post_alerts_without_second_source(cfg):
    """A SENAPRED evacuation order near Santiago: no waiting, no web search, marked official."""
    cfg.raw["news"]["feeds"] = [{"id": "senapred", "type": "telegram", "channel": "SenapredChile", "lang": "es",
                                 "scope": "CL", "official": True, "authority": "SENAPRED"}]
    now = datetime.now(timezone.utc)
    html = (FX / "senapred.html").read_text().replace("TIME1", (now - timedelta(minutes=3)).isoformat()) \
                                             .replace("TIME2", (now - timedelta(minutes=2)).isoformat())

    class C(FakeClaude):
        def __call__(self, request):
            body = json.loads(request.content)
            names = [t.get("name") for t in body.get("tools", [])]
            self.calls.append(body)
            if "record_triage" in names:
                out = []
                for l in body["messages"][0]["content"].split("\n")[1:]:
                    it = json.loads(l)
                    if "evacuar" in it["text"]:
                        out.append({"id": it["id"], "relevant": True, "category": "weather", "title_en": "Evacuation ordered in Talagante as Mapocho overflows",
                                    "summary_en": "SENAPRED orders evacuation of riverside camp", "place": "Talagante", "country": "CL",
                                    "lat": -33.66, "lon": -70.93, "severity": 4, "world_significant": False})
                    else:
                        out.append({"id": it["id"], "relevant": False})
                return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "x", "name": "record_triage", "input": {"items": out}}],
                                                 "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 10}})
            v = {"status": "confirmed", "severity": 4, "category": "weather", "title": "Evacuation in Talagante: Mapocho overflowing",
                 "summary": "SENAPRED ordered the evacuation of a riverside camp and sent cell-broadcast alerts.", "what_to_do": "",
                 "place": "Talagante", "country": "CL", "lat": -33.66, "lon": -70.93, "still_ongoing": True, "reasoning": "official"}
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "y", "name": "record_verdict", "input": v}],
                                             "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 10}})
    claude, sent = C(), []

    async def go():
        with respx.mock(assert_all_called=False) as m:
            m.get("https://t.me/s/SenapredChile").mock(return_value=httpx.Response(200, text=html))
            m.post("https://api.anthropic.com/v1/messages").mock(side_effect=claude)
            m.post(url__regex=r".*/sendMessage").mock(side_effect=lambda r: (sent.append(json.loads(r.content)["text"]),
                                                      httpx.Response(200, json={"ok": True, "result": {}}))[1])
            async with httpx.AsyncClient() as c:
                app = Iris(cfg, store=Store(":memory:"), client=c)
                app.store.kv_set("baseline:news", True)
                await app.process("news", await app.news.run_cycle())
    asyncio.run(go())
    triage = [c for c in claude.calls if "record_triage" in [t.get("name") for t in c["tools"]]][0]
    assert "Sismo de mediana" in triage["messages"][0]["content"]          # official: no keyword prefilter
    verify = [c for c in claude.calls if "record_verdict" in [t.get("name") for t in c["tools"]]][0]
    assert "OFFICIAL statement by SENAPRED" in verify["messages"][0]["content"]
    assert not any(t.get("name") == "web_search" for t in verify["tools"])
    assert len(sent) == 1 and "🏛 Official: SENAPRED" in sent[0] and "HIGH" in sent[0] and "Santiago" in sent[0]
