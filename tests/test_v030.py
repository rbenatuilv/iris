"""v0.3.0: strikes, air quality, escalation of confirmed stories, per-feed failure warnings."""
import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import respx

from iris import policy
from iris.app import Iris
from iris.collectors import civic
from iris.store import Store
from test_dedup import RIOT, Scripted, cycles
from test_news import cfg, rss  # noqa: F401

TODAY = date(2026, 10, 28)
STRIKES_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>MIT</title>
<item><title>Data inizio: 16/10/2026 - Settore: Trasporto merci - Rilevanza: Regionale - Regione: Sicilia - Provincia:  Tutte</title>
<description><![CDATA[Fermo autotrasporto]]></description><guid>g1</guid></item>
<item><title>Data inizio: 30/10/2026 - Settore: Generale - Rilevanza: Nazionale - Regione: Italia - Provincia:  Tutte</title>
<description><![CDATA[<p>Sciopero generale intera giornata</p>]]></description><guid>g2</guid><link>https://scioperi.mit.gov.it/x/2</link></item>
<item><title>Data inizio: 29/10/2026 - Settore: Trasporto pubblico locale - Rilevanza: Provinciale - Regione: Lombardia - Provincia: Milano</title>
<description><![CDATA[ATM Milano 24 ore]]></description><guid>g3</guid></item>
<item><title>Data inizio: 29/10/2026 - Settore: Trasporto pubblico locale - Rilevanza: Locale - Regione: Liguria - Provincia: Savona</title>
<description><![CDATA[TPL Savona 4 ore]]></description><guid>g4</guid></item>
<item><title>Data inizio: 20/11/2026 - Settore: Ferroviario - Rilevanza: Nazionale - Regione: Italia - Provincia:  Tutte</title>
<description><![CDATA[too far ahead]]></description><guid>g5</guid></item>
</channel></rss>"""


def test_strikes_that_matter_to_milan(cfg):
    evs = civic.strike_events(civic.parse_strikes(STRIKES_RSS), cfg.locations, cfg.get("strikes"), today=TODAY)
    titles = {e.title: e.fixed_levels for e in evs}
    assert len(evs) == 2, titles                     # Sicily freight, Savona buses and the far-future one are skipped
    atm = next(e for e in evs if "local public transport" in e.title)
    gen = next(e for e in evs if "general strike" in e.title)
    assert atm.fixed_levels == {"milan": "high"} and "Milano" in atm.title and "Thu 29 Oct" in atm.title
    assert gen.fixed_levels == {"milan": "high"} and "nationwide" in gen.title


def test_air_quality(cfg):
    santiago = cfg.location("santiago")
    hours = [f"2026-06-1{d}T{h:02d}:00" for d in (5, 6) for h in range(24)]
    data = {"hourly": {"time": hours, "pm2_5": [60] * 24 + [125] * 24}}
    evs = civic.air_events(data, santiago, cfg.get("air"))
    assert len(evs) == 1 and evs[0].fixed_levels == {"santiago": "high"} and "125" in evs[0].title
    data["hourly"]["pm2_5"] = [80] * 24 + [20] * 24
    evs = civic.air_events(data, santiago, cfg.get("air"))
    assert len(evs) == 1 and evs[0].fixed_levels == {"santiago": "info"}


def test_confirmed_story_escalates_through_merged_reports(cfg):
    """The bug: deaths reported later in the SAME story were swallowed. Now: re-verified → UPDATE."""
    claude = Scripted()
    claude.triage = {"scontri": RIOT, "saccheggi": RIOT,
                     "morto": {**RIOT, "title_en": "Riot in central Milan: one dead", "severity": 4,
                               "summary_en": "Clashes near the Duomo, one person killed"}}
    night = {"https://ansa.test/rss": rss([("Scontri in centro a Milano", "https://ansa.test/1", "", 30)]),
             "https://giorno.test/rss": rss([("Negozi saccheggiati in centro", "https://giorno.test/2", "", 20)])}
    later = {"https://ansa.test/rss": rss([("Un morto in centro a Milano", "https://ansa.test/7", "", 5)]),
             "https://giorno.test/rss": rss([])}
    n = {"v": 0}
    orig = claude.__call__

    def call(request):
        body = json.loads(request.content)
        if "record_verdict" in [t.get("name") for t in body.get("tools", [])]:
            n["v"] += 1
            if n["v"] == 2:
                assert "already CONFIRMED at severity 3" in body["messages"][0]["content"]
                claude.verdict_extra = {"severity": 4, "title": "Riot in Milan: one dead", "summary": "One person died."}
        return orig(request)
    app, sent = cycles(cfg, call, [night, later])
    assert app.store.db.execute("SELECT COUNT(*) FROM news_clusters").fetchone()[0] == 1
    assert len(sent) == 2 and "CRITICAL" in sent[1] and "UPDATE" in sent[1] and "#E1" in sent[1], sent


def test_failing_feed_is_reported_once(cfg):
    sent = []
    cfg.raw["source_failure_alert_minutes"] = 0
    cfg.raw["news"]["feeds"] = [{"id": "dead", "url": "https://dead.test/rss", "lang": "it", "scope": "milan"},
                                {"id": "ok", "url": "https://ok.test/rss", "lang": "it", "scope": "milan"}]

    async def go():
        with respx.mock(assert_all_called=False) as m:
            m.get("https://dead.test/rss").mock(return_value=httpx.Response(404))
            m.get("https://ok.test/rss").mock(return_value=httpx.Response(200, text=rss([])))
            m.post(url__regex=r".*/sendMessage").mock(side_effect=lambda r: (sent.append(json.loads(r.content)["text"]),
                                                      httpx.Response(200, json={"ok": True, "result": {}}))[1])
            async with httpx.AsyncClient() as c:
                app = Iris(cfg, store=Store(":memory:"), client=c)
                for _ in range(3):
                    await app._news_cycle()
                return app.health_summary(short=True)
    short = asyncio.run(go())
    assert len([s for s in sent if "News feed <b>dead</b>" in s]) == 1
    assert "news:dead" in short
