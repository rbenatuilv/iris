import time
from datetime import datetime

import httpx

from iris import digest
from iris.app import Iris
from iris.models import Event
from iris.policy import evaluate
from iris.store import Store
from test_news import cfg  # noqa: F401


def _app(cfg):
    return Iris(cfg, store=Store(":memory:"), client=httpx.AsyncClient())


def _add(app, key, title, reason, level="info", area="home", country="IT", sev=2, ago_h=1.0, held=False):
    t = time.time() - ago_h * 3600
    app.store.save_event(key, "news", title, 45.46, 9.19, t, level, reason,
                         {"area": area, "cat": "unrest", "details": {"country": country, "severity": sev}})
    app.store.add_digest(key, level, f"🔵 <b>{title}</b>", held=held)


def _fill(app):
    _add(app, "n:legnano", "Student clashes in Legnano", "26 km from Milan", level="high", sev=3, ago_h=20, held=True)
    _add(app, "n:paris", "French protests turn violent", "Paris, France (world)", area="world", country="FR", ago_h=50)
    _add(app, "n:riot1", "Prosecutors seek jail for five", "in Milan", ago_h=21)
    _add(app, "n:riot2", "Eleven arrested after Milan riots", "in Milan · over", ago_h=20)
    _add(app, "n:rome", "Clashes at protests in Rome", "Roma, Italia", area="world", country="IT", ago_h=35)
    _add(app, "n:ubisoft", "Ubisoft Milan staff strike", "in Milan", sev=1, ago_h=22)
    _add(app, "n:duomo", "Security alert at the Duomo", "in Milan", sev=3, ago_h=15)
    _add(app, "n:monza", "Fire at business in Monza", "15 km from Milan", ago_h=14)
    _add(app, "n:knife", "Police officer knifed in woods", "21 km from Milan · unconfirmed", ago_h=2)
    _add(app, "n:civate", "Masked youths detained in Civate", "29 km from Milan", ago_h=1.5)
    _add(app, "n:farellones", "Routes to Farellones closed", "36 km from Santiago", area="CL", country="CL", ago_h=5)
    _add(app, "n:france2", "Curfews in French cities", "France (world)", area="world", country="FR", ago_h=15)


def test_sections_order_and_trimming(cfg):
    app = _app(cfg)
    _fill(app)
    text = app.build_digest()
    pos = [text.index(h) for h in ("⚠️ Held overnight", "📍 Milan &amp; around", "🇮🇹 Italy", "🇨🇱 Chile", "🌍 World")]
    assert pos == sorted(pos)
    assert "(world)" not in text and "from Milan" not in text.split("📍")[1].split("🇮🇹")[0]
    assert "26 km from Milan" in text                    # held section keeps the full place
    milan = text.split("📍 Milan &amp; around")[1].split("🇮🇹")[0]
    assert milan.index("Security alert at the Duomo") < milan.index("Police officer knifed")   # more serious first
    assert "Ubisoft" not in milan.split("<i>")[0] and "Ubisoft Milan staff strike</i>" in milan              # folded, named
    assert "Clashes at protests in Rome" in text.split("🇮🇹 Italy")[1].split("🇨🇱")[0]


def test_ai_groups_merge_only_valid_ones(cfg):
    app = _app(cfg)
    _fill(app)
    items = digest.collect(app)
    ids = {i.key: i.eid for i in items}
    groups = [{"ids": [ids["n:riot1"], ids["n:riot2"]], "label": "Riot aftermath", "summary": "Jail sought for 11"},
              {"ids": [ids["n:paris"], ids["n:farellones"]], "label": "Bogus", "summary": "x"},       # mixed sections
              {"ids": [ids["n:legnano"], ids["n:duomo"]], "label": "Held", "summary": "x"},           # held item
              {"ids": [ids["n:france2"]], "label": "Single", "summary": "x"}]                         # one id only
    text = digest.render(app, items, "Busy night in Milan & France.", groups)
    assert "<i>Busy night in Milan &amp; France.</i>" in text
    assert f"<b>Riot aftermath</b> (2 reports): Jail sought for 11 · /e{ids['n:riot2']}" in text
    assert "Bogus" not in text and "Held</b>" not in text and "Single" not in text
    assert "Prosecutors seek jail" not in text           # merged away


def test_empty_digest(cfg):
    assert "All quiet" in _app(cfg).build_digest()


def test_when_format():
    now = datetime(2026, 10, 20, 9, 0).astimezone()
    assert digest.fmt_when(now.replace(hour=5, minute=24).timestamp(), now) == "05:24"
    assert digest.fmt_when(datetime(2026, 10, 19, 10, 45).astimezone().timestamp(), now).endswith("10:45")
    assert digest.fmt_when(datetime(2026, 10, 1, 10, 45).astimezone().timestamp(), now).endswith("Oct")


def test_chile_single_crime_left_out(cfg):
    def ev(sev, cat):
        return Event(key=f"news:{sev}{cat}", kind="news", title="x", time=datetime.now().astimezone(),
                     lat=-33.42, lon=-70.65, details={"category": cat, "severity": sev, "country": "CL",
                                                      "precision": "neighbourhood", "verified": "confirmed"})
    assert evaluate(ev(3, "crime"), cfg)[0] == "none"     # one homicide in Santiago: not for the digest
    assert evaluate(ev(4, "crime"), cfg)[0] != "none"     # major crime still counts
    assert evaluate(ev(3, "unrest"), cfg)[0] == "info"    # mass protests unchanged
