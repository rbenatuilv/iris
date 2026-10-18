import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
import respx

from iris.app import Iris
from iris.config import load_config
from iris.llm import LLM, BudgetExceeded
from iris.news import parse_feed, prefilter
from iris.store import Store
from test_iris import ROOT

NOW = datetime.now(timezone.utc)


def rss(items, title="Feed"):
    body = "".join(f"<item><title>{t}</title><link>{u}</link><description>{d}</description>"
                   f"<pubDate>{format_datetime(NOW - timedelta(minutes=m))}</pubDate></item>" for t, u, d, m in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>{body}</channel></rss>'


ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Blast at plant</title>
<link href="https://x.com/a"/><updated>2026-10-17T20:00:00Z</updated><summary>An explosion &lt;b&gt;hit&lt;/b&gt;</summary></entry></feed>"""


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "defaults.yaml").write_text((ROOT / "config" / "defaults.yaml").read_text())
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=T\nTELEGRAM_CHAT_ID=42\nANTHROPIC_API_KEY=k\n")
    c = load_config(tmp_path)
    c.raw["quiet_hours"] = {}
    c.raw["news"]["gdelt"]["enabled"] = False
    c.raw["news"]["feeds"] = [
        {"id": "ansa", "url": "https://ansa.test/rss", "lang": "it", "scope": "milan"},
        {"id": "giorno", "url": "https://giorno.test/rss", "lang": "it", "scope": "milan"},
    ]
    c.raw["llm"]["context_followups"] = False
    return c


def test_parse_feed_and_prefilter():
    items = parse_feed(rss([("Scontri in centro", "https://a/1", "<p>saccheggi</p>", 5)]))
    assert items[0]["title"] == "Scontri in centro" and items[0]["summary"] == "saccheggi" and items[0]["published"]
    a = parse_feed(ATOM)
    assert a[0]["url"] == "https://x.com/a" and a[0]["summary"] == "An explosion hit"
    assert prefilter("Rapina a mano armata in via Padova", "", "it")
    assert not prefilter("Inter batte il Milan 2-1", "", "it")


class FakeClaude:
    """Answers triage and verification calls like the Messages API would."""
    def __init__(self, verdict="confirmed", severity=3):
        self.calls = []
        self.verdict, self.severity = verdict, severity

    def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(body)
        tool_names = [t.get("name") for t in body.get("tools", [])]
        usage = {"input_tokens": 1000, "output_tokens": 200}
        if "record_triage" in tool_names:
            ids = [json.loads(l)["id"] for l in body["messages"][0]["content"].split("\n")[1:]]
            out = []
            for l in body["messages"][0]["content"].split("\n")[1:]:
                it = json.loads(l)
                if "sacchegg" in it["title"].lower() or "scontri" in it["title"].lower():
                    out.append({"id": it["id"], "relevant": True, "category": "unrest",
                                "title_en": "Clashes and looting in central Milan",
                                "summary_en": "Protest turned violent, shops looted on Corso Vittorio Emanuele.",
                                "place": "Corso Vittorio Emanuele, Milan", "country": "IT", "lat": 45.4651, "lon": 9.1942,
                                "severity": 3, "world_significant": False})
                else:
                    out.append({"id": it["id"], "relevant": False})
            content = [{"type": "tool_use", "id": "t1", "name": "record_triage", "input": {"items": out}}]
        elif "record_verdict" in tool_names:
            usage["server_tool_use"] = {"web_search_requests": 1}
            content = [{"type": "tool_use", "id": "t2", "name": "record_verdict", "input": {
                "status": self.verdict, "severity": self.severity, "category": "unrest",
                "title": "Clashes and looting in central Milan",
                "summary": "A protest turned violent; several shops were looted. Police are clearing the area.",
                "what_to_do": "Avoid Duomo and Corso Vittorio Emanuele tonight.", "place": "Corso Vittorio Emanuele, Milan",
                "country": "IT", "lat": 45.4651, "lon": 9.1942, "independent_sources": 2, "reasoning": "ok"}}]
        else:
            content = [{"type": "text", "text": "Quiet night overall."}]
        return httpx.Response(200, json={"content": content, "stop_reason": "tool_use", "usage": usage})


def run_cycle(cfg, feeds, claude, baseline_done=True):
    sent = []

    async def go():
        with respx.mock(assert_all_called=False) as m:
            m.post(url__regex=r".*/sendMessage").mock(side_effect=lambda r: (sent.append(json.loads(r.content)["text"]),
                                                                             httpx.Response(200, json={"ok": True, "result": {}}))[1])
            for url, xml in feeds.items():
                m.get(url).mock(return_value=httpx.Response(200, text=xml))
            m.post("https://api.anthropic.com/v1/messages").mock(side_effect=claude)
            async with httpx.AsyncClient() as client:
                app = Iris(cfg, store=Store(":memory:"), client=client)
                if baseline_done:
                    app.store.kv_set("baseline:news", True)
                events = await app.news.run_cycle()
                await app.process("news", events)
                return app
    return asyncio.run(go()), sent


def test_two_sources_confirmed_alert(cfg):
    claude = FakeClaude()
    feeds = {"https://ansa.test/rss": rss([("A Milano saccheggiati negozi in corso Vittorio Emanuele", "https://ansa.test/1", "", 20),
                                          ("Inter, che partita", "https://ansa.test/2", "", 10)]),
             "https://giorno.test/rss": rss([("Scontri in centro a Milano, negozi saccheggiati", "https://giorno.test/9", "", 15)])}
    app, sent = run_cycle(cfg, feeds, claude)
    assert len(sent) == 1, sent
    msg = sent[0]
    assert "HIGH · Unrest" in msg and "Confirmed · 2 sources" in msg and "Avoid Duomo" in msg and "in Milan" in msg
    # one triage call (the football item never reaches the model), one verification
    kinds = [[t.get("name") for t in c.get("tools", [])] for c in claude.calls]
    assert sum("record_triage" in k for k in kinds) == 1 and sum("record_verdict" in k for k in kinds) == 1
    triage_user = next(c for c in claude.calls if "record_triage" in [t.get("name") for t in c["tools"]])["messages"][0]["content"]
    assert "Inter" not in triage_user
    assert app.llm.spent_today() > 0
    stats = app.llm.spend_stats()
    assert stats["verify"]["searches"] == 1


def test_single_source_waits_then_unconfirmed(cfg):
    claude = FakeClaude(verdict="unconfirmed")
    feeds = {"https://ansa.test/rss": rss([("Scontri in piazza a Milano", "https://ansa.test/1", "", 5)]),
             "https://giorno.test/rss": rss([])}
    cfg.raw["news"]["single_source_wait_minutes"] = 0
    app, sent = run_cycle(cfg, feeds, claude)
    assert sent == []                                    # unconfirmed news never pings you
    assert "unconfirmed" in app.build_digest()           # ...it goes to the digest instead


def test_budget_cap(cfg):
    async def go():
        async with httpx.AsyncClient() as client:
            store = Store(":memory:")
            llm = LLM(client, "k", store, cfg)
            llm.daily_budget = 0.01
            llm._record("claude-sonnet-5-5", {"input_tokens": 10000, "output_tokens": 0}, "x")   # $0.02
            with pytest.raises(BudgetExceeded):
                await llm.text(model="claude-haiku-5-5", system="s", user="u", purpose="p")
    asyncio.run(go())


def test_one_story_one_cluster_and_cheap_verify(cfg):
    """Tonight's case: clashes, looting and reactions in Milan must be ONE event, verified once, cheaply."""
    class Varied(FakeClaude):
        def __call__(self, request):
            body = json.loads(request.content)
            if "record_triage" not in [t.get("name") for t in body.get("tools", [])]:
                return super().__call__(request)
            self.calls.append(body)
            titles = {"scontri": ("Clashes between protesters and police in Milan", 45.4641, 9.1919),
                      "saccheggi": ("Shops looted on Corso Vittorio Emanuele", 45.4651, 9.1942),
                      "fontana": ("Lombardy president condemns violence in Milan", 45.4842, 9.2040)}
            out = []
            for l in body["messages"][0]["content"].split("\n")[1:]:
                it = json.loads(l)
                key = next(k for k in titles if k in it["title"].lower())
                t, lat, lon = titles[key]
                out.append({"id": it["id"], "relevant": True, "category": "unrest", "title_en": t, "summary_en": t,
                            "place": "Milan", "country": "IT", "lat": lat, "lon": lon, "severity": 3,
                            "world_significant": False})
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "x", "name": "record_triage",
                                                          "input": {"items": out}}], "stop_reason": "tool_use",
                                             "usage": {"input_tokens": 500, "output_tokens": 100}})
    claude = Varied()
    feeds = {"https://ansa.test/rss": rss([("Scontri in centro a Milano", "https://ansa.test/1", "", 30),
                                          ("Fontana: immagini gravissime, scontri inaccettabili", "https://ansa.test/2", "", 10)]),
             "https://giorno.test/rss": rss([("Negozi saccheggiati in corso Vittorio Emanuele", "https://giorno.test/3", "", 20)])}
    app, sent = run_cycle(cfg, feeds, claude)
    assert app.store.db.execute("SELECT COUNT(*) FROM news_clusters").fetchone()[0] == 1
    verifies = [c for c in claude.calls if "record_verdict" in [t.get("name") for t in c.get("tools", [])]]
    assert len(verifies) == 1
    assert verifies[0]["model"] == "claude-haiku-5-5"
    assert not any(t.get("name") == "web_search" for t in verifies[0]["tools"])   # 2 outlets: no search needed
    assert len(sent) == 1 and "Confirmed · 2 sources" in sent[0]
