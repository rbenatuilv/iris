"""Replays of real situations: the same story must not ping twice."""
import asyncio
import json
from datetime import timedelta

import httpx
import respx

from iris.app import Iris
from iris.store import Store
from test_news import FakeClaude, cfg, rss  # noqa: F401


class Scripted(FakeClaude):
    """Triage returns whatever `self.triage` maps title keywords to; verify returns `self.verdict_extra`."""
    def __init__(self):
        super().__init__()
        self.triage = {}
        self.verdict_extra = {}

    def __call__(self, request):
        body = json.loads(request.content)
        names = [t.get("name") for t in body.get("tools", [])]
        if "record_triage" in names:
            self.calls.append(body)
            out = []
            for l in body["messages"][0]["content"].split("\n")[1:]:
                it = json.loads(l)
                k = next((k for k in self.triage if k in it["title"].lower()), None)
                out.append({"id": it["id"], **self.triage[k]} if k else {"id": it["id"], "relevant": False})
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "x", "name": "record_triage",
                                  "input": {"items": out}}], "stop_reason": "tool_use",
                                  "usage": {"input_tokens": 100, "output_tokens": 50}})
        if "record_verdict" in names:
            self.calls.append(body)
            v = {"status": "confirmed", "severity": 3, "category": "unrest", "title": "Riot in central Milan",
                 "summary": "Clashes and looting near the Duomo.", "what_to_do": "", "place": "Milan", "country": "IT",
                 "lat": 45.4642, "lon": 9.19, "independent_sources": 3, "still_ongoing": True, "reasoning": "x",
                 **self.verdict_extra}
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "y", "name": "record_verdict",
                                  "input": v}], "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 50}})
        return super().__call__(request)


RIOT = {"relevant": True, "category": "unrest", "title_en": "Riot and looting in central Milan",
        "summary_en": "Protest turned violent near the Duomo", "place": "Milan", "country": "IT",
        "lat": 45.4642, "lon": 9.19, "severity": 3, "world_significant": False}


def cycles(cfg, claude, rounds):
    """rounds: list of {url: rss_xml}. Runs one news cycle per round on the same Iris instance."""
    sent = []

    async def go():
        with respx.mock(assert_all_called=False) as m:
            m.post(url__regex=r".*/sendMessage").mock(side_effect=lambda r: (sent.append(json.loads(r.content)["text"]),
                                                      httpx.Response(200, json={"ok": True, "result": {}}))[1])
            m.post("https://api.anthropic.com/v1/messages").mock(side_effect=claude)
            routes = {u: m.get(u) for u in rounds[0]}
            async with httpx.AsyncClient() as client:
                app = Iris(cfg, store=Store(":memory:"), client=client)
                app.store.kv_set("baseline:news", True)
                for feeds in rounds:
                    for u, xml in feeds.items():
                        routes[u].mock(return_value=httpx.Response(200, text=xml))
                    await app.process("news", await app.news.run_cycle())
                return app
    return asyncio.run(go()), sent


def test_morning_recap_does_not_ping_again(cfg):
    claude = Scripted()
    claude.triage = {"scontri": RIOT, "saccheggi": RIOT,
                     "bilancio": {**RIOT, "title_en": "Milan march: about 50 detained after clashes",
                                  "summary_en": "Police tally after last night's march, square cleared"}}
    night = {"https://ansa.test/rss": rss([("Scontri in centro a Milano", "https://ansa.test/1", "", 30)]),
             "https://giorno.test/rss": rss([("Negozi saccheggiati in centro", "https://giorno.test/2", "", 20)])}
    morning = {"https://ansa.test/rss": rss([("Il bilancio della notte: 50 fermati", "https://ansa.test/9", "", 5)]),
               "https://giorno.test/rss": rss([])}
    app, sent = cycles(cfg, claude, [night, morning])
    assert len(sent) == 1, sent                 # one alert at night, nothing in the morning
    assert app.store.db.execute("SELECT COUNT(*) FROM news_clusters").fetchone()[0] == 1


def test_verifier_flags_same_story(cfg):
    """Different wording AND different category group would escape the clusterer; the verifier catches it."""
    claude = Scripted()
    claude.triage = {"scontri": RIOT, "saccheggi": RIOT,
                     "agenti": {**RIOT, "category": "crime", "title_en": "Police officers injured, arrests made",
                                "summary_en": "Officers hurt during disorder downtown"}}
    night = {"https://ansa.test/rss": rss([("Scontri in centro a Milano", "https://ansa.test/1", "", 30)]),
             "https://giorno.test/rss": rss([("Negozi saccheggiati in centro", "https://giorno.test/2", "", 20)])}
    later = {"https://ansa.test/rss": rss([("Agenti feriti e arresti", "https://ansa.test/5", "", 5)]),
             "https://giorno.test/rss": rss([("Agenti feriti, arresti in centro", "https://giorno.test/6", "", 4)])}

    first_verdict = {}
    calls = {"n": 0}
    orig = claude.__call__

    def call(request):
        body = json.loads(request.content)
        if "record_verdict" in [t.get("name") for t in body.get("tools", [])]:
            calls["n"] += 1
            if calls["n"] == 2:
                assert "Already reported" in body["messages"][0]["content"]
                claude.verdict_extra = {"same_story_as": 1, "new_development": False}
        return orig(request)
    app, sent = cycles(cfg, call, [night, later])
    assert len(sent) == 1, sent


def test_story_that_is_over_goes_to_digest_only(cfg):
    claude = Scripted()
    claude.triage = {"scontri": RIOT, "saccheggi": RIOT}
    claude.verdict_extra = {"still_ongoing": False}
    night = {"https://ansa.test/rss": rss([("Scontri in centro a Milano", "https://ansa.test/1", "", 30)]),
             "https://giorno.test/rss": rss([("Negozi saccheggiati in centro", "https://giorno.test/2", "", 20)])}
    app, sent = cycles(cfg, claude, [night])
    assert sent == []
    assert "over" in app.build_digest()


def test_real_escalation_still_sends_update(cfg):
    claude = Scripted()
    claude.triage = {"scontri": RIOT, "saccheggi": RIOT,
                     "morti": {**RIOT, "category": "crime", "title_en": "Two dead as riot spreads to Porta Venezia",
                               "summary_en": "Deaths reported, unrest spreading", "severity": 4}}
    night = {"https://ansa.test/rss": rss([("Scontri in centro a Milano", "https://ansa.test/1", "", 30)]),
             "https://giorno.test/rss": rss([("Negozi saccheggiati in centro", "https://giorno.test/2", "", 20)])}
    later = {"https://ansa.test/rss": rss([("Due morti a Porta Venezia", "https://ansa.test/7", "", 5)]),
             "https://giorno.test/rss": rss([("Due morti in centro", "https://giorno.test/8", "", 4)])}
    n = {"v": 0}
    orig = claude.__call__

    def call(request):
        body = json.loads(request.content)
        if "record_verdict" in [t.get("name") for t in body.get("tools", [])]:
            n["v"] += 1
            if n["v"] == 2:
                claude.verdict_extra = {"same_story_as": 1, "new_development": True, "severity": 4,
                                        "title": "Riot spreads, two dead", "summary": "Two people died."}
        return orig(request)
    app, sent = cycles(cfg, call, [night, later])
    assert len(sent) == 2 and "UPDATE" in sent[1] and "CRITICAL" in sent[1] and "#E1" in sent[1], sent
