"""Near-you boost inside Milan (live location), with location precision."""
from datetime import datetime, timezone

from iris import policy
from iris.models import Event
from test_news import cfg  # noqa: F401

NAVIGLI = (45.4520, 9.1750)          # where you are


def news(sev, lat, lon, precision="street", verified="confirmed", **kw):
    return Event(key="news:1", kind="news", title="t", time=datetime.now(timezone.utc), lat=lat, lon=lon,
                 details={"severity": sev, "country": "IT", "verified": verified, "precision": precision, **kw})


def test_near_you_levels(cfg):
    cfg.me = NAVIGLI
    # armed robbery (sev 2) 200 m away, via Vigevano: digest → ALERT
    lvl, why = policy.evaluate(news(2, 45.4535, 9.1735), cfg)
    assert lvl == "high" and why == "200 m from you"
    # the same robbery in Città Studi, ~4.5 km away: stays in the digest
    assert policy.evaluate(news(2, 45.478, 9.227), cfg)[0] == "info"
    # clashes (sev 3) ~1 km away: HIGH → CRITICAL
    lvl, why = policy.evaluate(news(3, 45.4610, 9.1760), cfg)
    assert lvl == "critical" and why.endswith("from you")
    # sev 2 right next to you never reaches CRITICAL (needs 3+)
    assert policy.evaluate(news(2, 45.4521, 9.1751), cfg)[0] == "high"
    # clashes 2.5 km away: still HIGH, but the alert says how far
    lvl, why = policy.evaluate(news(3, 45.4745, 9.1750), cfg)
    assert lvl == "high" and "2.5 km from you" in why


def test_vague_location_gets_no_boost(cfg):
    cfg.me = (45.4642, 9.1900)        # standing at the Duomo
    # "a robbery in Milan" placed at the city centre with precision=city: no "near you"
    assert policy.evaluate(news(2, 45.4642, 9.1900, precision="city"), cfg)[0] == "info"


def test_no_live_location_no_change(cfg):
    cfg.me = None
    assert policy.evaluate(news(2, 45.4535, 9.1735), cfg)[0] == "info"


def test_boost_respects_verification_and_ended(cfg):
    cfg.me = NAVIGLI
    assert policy.evaluate(news(2, 45.4535, 9.1735, verified=None), cfg)[0] == "info"     # unconfirmed: digest
    lvl, why = policy.evaluate(news(2, 45.4535, 9.1735, ended=True), cfg)
    assert lvl == "info" and "over" in why                                                # already over: digest


def test_single_source_near_you_is_checked_immediately(cfg, monkeypatch):
    """No 15-minute wait for a second outlet when it's happening around the corner."""
    import time
    from iris.location import Tracker
    from test_dedup import RIOT, Scripted, cycles
    from test_news import rss
    monkeypatch.setattr(Tracker, "active", lambda self: {"lat": NAVIGLI[0], "lon": NAVIGLI[1], "source": "live",
                                                          "tz": "Europe/Rome", "expires": time.time() + 3600})
    claude = Scripted()
    claude.triage = {"rapina": {**RIOT, "category": "crime", "title_en": "Armed robbery at a shop on via Vigevano",
                                "summary_en": "Two armed men robbed a shop", "place": "via Vigevano, Milan",
                                "lat": 45.4535, "lon": 9.1735, "severity": 2, "precision": "street"}}
    claude.verdict_extra = {"severity": 2, "category": "crime", "title": "Armed robbery on via Vigevano",
                            "summary": "Two armed men robbed a shop; police searching the area.", "lat": 45.4535,
                            "lon": 9.1735, "precision": "street", "place": "via Vigevano, Milan"}
    feeds = {"https://ansa.test/rss": rss([("Rapina a mano armata in via Vigevano", "https://ansa.test/1", "", 3)]),
             "https://giorno.test/rss": rss([])}
    app, sent = cycles(cfg, claude, [feeds])
    verifies = [c for c in claude.calls if "record_verdict" in [t.get("name") for t in c.get("tools", [])]]
    assert len(verifies) == 1 and any(t.get("name") == "web_search" for t in verifies[0]["tools"])
    assert len(sent) == 1 and "HIGH · Crime" in sent[0] and "200 m from you" in sent[0]
