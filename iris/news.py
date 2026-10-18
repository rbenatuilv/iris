"""News layer: RSS + GDELT → keyword prefilter → Haiku triage (translate, classify,
locate, rate) → clustering of reports about the same incident → Sonnet verification
(with web search) for anything that would reach you as HIGH/CRITICAL → Events."""
from __future__ import annotations

import asyncio
import hashlib
import html as htmlmod
import json
import logging
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import defusedxml.ElementTree as ET
from html.parser import HTMLParser

from .llm import LLM, BudgetExceeded, LLMError
from .models import Event, haversine_km, level_rank

log = logging.getLogger("iris.news")

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY, feed TEXT, scope TEXT, url TEXT, domain TEXT, title TEXT, summary TEXT,
    lang TEXT, published REAL, fetched REAL, status TEXT, triage TEXT, cluster INTEGER);
CREATE INDEX IF NOT EXISTS ni_status ON news_items(status);
CREATE INDEX IF NOT EXISTS ni_fetched ON news_items(fetched);
CREATE TABLE IF NOT EXISTS news_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, title TEXT, summary TEXT, place TEXT, country TEXT,
    lat REAL, lon REAL, severity INTEGER, world_significant INTEGER, first_seen REAL, first_published REAL,
    last_update REAL, tokens TEXT, verified TEXT, verified_at REAL, verify_count INTEGER DEFAULT 0,
    what_to_do TEXT, reasoning TEXT);
CREATE INDEX IF NOT EXISTS nc_update ON news_clusters(last_update);
"""

CATEGORIES = ["crime", "terrorism", "unrest", "strike_transport", "infrastructure", "fire", "weather",
              "geohazard", "accident", "health", "conflict", "other"]
CATEGORY_LABEL = {"crime": "Crime", "terrorism": "Terrorism", "unrest": "Unrest", "strike_transport": "Strike / transport",
                  "infrastructure": "Infrastructure", "fire": "Fire", "weather": "Weather", "geohazard": "Natural hazard",
                  "accident": "Accident", "health": "Health", "conflict": "Conflict", "other": "Incident"}
GROUP = {"crime": "violence", "terrorism": "violence", "unrest": "unrest", "strike_transport": "transport",
         "infrastructure": "infra", "fire": "hazard", "weather": "hazard", "geohazard": "hazard",
         "accident": "hazard", "health": "health", "conflict": "conflict", "other": "other"}

# Cheap, generous keyword prefilter (stems). Anything matching goes to Haiku; everything else is dropped.
KEYWORDS = {
    "it": "sparator spara accoltell rapina omicid uccis morto morti mort feriti ferit attentat esplosion bomba ordigno "
          "incendi fiamme rogo evacuat allerta alluvion esondaz frana terremot scossa sciopero blackout black-out "
          "manifestaz corteo scontri guerriglia saccheggi tafferugli disordini ostaggi crollo crollat incidente "
          "emergenza nube_tossica fuga_di_gas metro_ferm chius sequestr arrest polizia carabinieri allarme "
          "maltempo grandine tromba_d'aria nubifragio caldo_record smog epidemi focolaio virus terror armat coltello",
    "es": "balacera tiroteo baleado disparo asesinat homicidio muerto muertos fallecid herido heridos portonazo "
          "turbazo encerrona asalto robo_violento secuestr atentado explosi bomba artefacto incendi fuego llamas "
          "evacuaci alerta_roja alerta_amarilla temporal inundaci aluvi desborde sismo terremoto tsunami paro "
          "huelga corte_de_luz apag manifestaci protesta disturbios barricada saqueo estado_de_excepci toque_de_queda "
          "enfrentamiento carabineros pdi emergencia derrumbe accidente choque volcamiento avalancha alud brote "
          "contaminaci preemergencia nevazón marejada narco crimen_organizado",
    "en": "shooting shot gunman stabbing stabbed killed dead death injured wounded explosion blast bomb attack "
          "terror hostage fire wildfire evacuat flood landslide earthquake quake tsunami eruption storm hurricane "
          "typhoon cyclone strike blackout outage protest riot unrest looting curfew state_of_emergency emergency "
          "war missile drone invasion airstrike troops outbreak epidemic pandemic collapse crash derail nuclear",
}
_KW = {lang: re.compile(r"\b(?:" + "|".join(re.escape(w.replace("_", " ")) for w in words.split()) + ")", re.I)
       for lang, words in KEYWORDS.items()}

STOP = set("the a an of in on at to for and or with by from is are was were be been has have had as it its this that "
           "after over into near amid says said new two three one".split())


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in STOP}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", htmlmod.unescape(s)).strip()


def domain_of(url: str) -> str:
    d = urlparse(url).netloc.lower()
    return d[4:] if d.startswith("www.") else d


def prefilter(title: str, summary: str, lang: str) -> bool:
    text = f"{title} {summary}"
    langs = [lang] if lang in _KW else list(_KW)
    if lang != "en":
        langs.append("en")
    return any(_KW[l].search(text) for l in langs)


# ------------------------------------------------------------------ parsing
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_date(s: str) -> float | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def parse_feed(xml: str) -> list[dict]:
    """Generic RSS 2.0 / Atom parser → [{title, url, summary, published}]"""
    root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    out = []
    for el in root.iter():
        name = _local(el.tag)
        if name not in ("item", "entry"):
            continue
        f: dict = {}
        for c in el:
            n = _local(c.tag)
            if n == "link":
                f.setdefault("url", (c.get("href") or c.text or "").strip())
            elif n in ("title",):
                f["title"] = strip_html(c.text or "")
            elif n in ("description", "summary", "encoded", "content") and not f.get("summary"):
                f["summary"] = strip_html("".join(c.itertext()))[:600]
            elif n in ("pubDate", "published", "updated", "date") and not f.get("published"):
                f["published"] = _parse_date(c.text or "")
            elif n in ("guid", "id"):
                f.setdefault("guid", (c.text or "").strip())
        if not f.get("url") and f.get("guid", "").startswith("http"):
            f["url"] = f["guid"]
        if f.get("title") and f.get("url"):
            out.append(f)
    return out


class _TgParser(HTMLParser):
    """Reads the public web view of a Telegram channel (https://t.me/s/<channel>)."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts: list[dict] = []
        self._cur = None
        self._depth = 0          # div depth inside the current text block
        self._in_text = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div" and "tgme_widget_message " in cls + " " and a.get("data-post"):
            self._cur = {"post": a["data-post"], "text": "", "time": None}
            self.posts.append(self._cur)
        elif self._cur is not None and tag == "div" and "tgme_widget_message_text" in cls and not self._in_text:
            self._in_text, self._depth = True, 1
        elif self._in_text and tag == "div":
            self._depth += 1
        elif self._in_text and tag == "br":
            self._cur["text"] += "\n"
        elif self._cur is not None and tag == "time" and a.get("datetime") and self._cur["time"] is None:
            self._cur["time"] = a["datetime"]

    def handle_endtag(self, tag):
        if self._in_text and tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self._in_text = False

    def handle_data(self, data):
        if self._in_text and self._cur is not None:
            self._cur["text"] += data


def parse_telegram_channel(html_text: str, channel: str) -> list[dict]:
    p = _TgParser()
    p.feed(html_text)
    out = []
    for post in p.posts:
        text = re.sub(r"[ \t]+", " ", post["text"]).strip()
        ts = _parse_date(post["time"]) if post["time"] else None
        if not text or ts is None:
            continue                # video-only posts etc.
        first = text.split("\n", 1)[0]
        out.append({"title": first[:200], "summary": text[:600], "url": f"https://t.me/{post['post']}",
                    "published": ts, "domain": f"t.me/{channel}"})
    return out


def parse_gdelt(data: dict) -> list[dict]:
    out = []
    for a in data.get("articles", []) or []:
        ts = None
        try:
            ts = datetime.strptime(a.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
        if a.get("url") and a.get("title"):
            lang = {"Italian": "it", "Spanish": "es", "English": "en"}.get(a.get("language", ""), "")
            out.append({"title": strip_html(a["title"]), "url": a["url"], "summary": "", "published": ts,
                        "lang": lang, "domain": a.get("domain")})
    return out


# ------------------------------------------------------------------ prompts
TRIAGE_SYSTEM = """You triage news items for a personal security alert system. The owner lives in {home} and
also follows: {others}. Items come from local, national and international outlets in Italian, Spanish and English.

For EVERY item return an entry with its id. Mark relevant=true only for a real, specific, CURRENT incident or
situation that could affect people's safety, movement or daily life (crime, attacks, unrest, strikes/transport
disruption, outages, fires, severe weather, natural hazards, serious accidents, health threats, armed conflict).
relevant=false for: opinion, politics/debate without an immediate safety impact, court cases/trials about past
events, anniversaries, sports, culture, economy, celebrity, follow-ups with no new incident, advertising.
For irrelevant items return only id and relevant=false.

For relevant items fill every field, in ENGLISH:
- title_en: max 12 words, factual. summary_en: max 30 words: what, where, consequences so far.
- place: most specific place (neighbourhood/street + city), country: ISO-3166 alpha-2.
- lat/lon: your best approximate coordinates of that place (city centre if only the city is known).
- severity (local impact):
  1 minor (petty crime, small fire, minor accident, small demonstration)
  2 notable (armed robbery, serious injury, localized protest or traffic block, partial transit disruption)
  3 significant (killing, large fire, protest with clashes or looting, city-wide strike or metro shutdown, large blackout)
  4 major (3+ dead in one incident, terror attack, riots across a city, state of emergency, mass evacuation)
  5 catastrophic (dozens dead, national emergency, war reaching the country)
- world_significant: true only for a top global headline with broad international impact (major war escalation,
  terror attack in a major city, disaster with 100+ dead, pandemic-level outbreak, nuclear/industrial catastrophe).
- event_time: ISO time if stated, else null."""

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"}, "relevant": {"type": "boolean"},
            "category": {"type": "string", "enum": CATEGORIES},
            "title_en": {"type": "string"}, "summary_en": {"type": "string"},
            "place": {"type": "string"}, "country": {"type": "string"},
            "lat": {"type": ["number", "null"]}, "lon": {"type": ["number", "null"]},
            "severity": {"type": "integer", "minimum": 0, "maximum": 5},
            "world_significant": {"type": "boolean"}, "event_time": {"type": ["string", "null"]}},
        "required": ["id", "relevant"]}}},
    "required": ["items"]}

VERIFY_SYSTEM = """You verify a possible incident before a personal security alert is sent to someone who lives in
{home} (also follows {others}). Today is {today}. You get the reports grouped so far. Use web search when the
reports come from a single outlet or anything is unclear. Decide:
- status: "confirmed" (real, current, at least two independent credible outlets or an official source),
  "unconfirmed" (plausible but only one source and nothing else found), "rejected" (false, old news, not an
  incident, or wrongly located).
- Correct severity (same 1-5 scale: 1 minor, 2 notable, 3 significant, 4 major, 5 catastrophic), category,
  place, coordinates.
- still_ongoing: true if the situation is still developing or its effects still matter now (clashes continuing,
  road still closed, fire still burning, suspect at large). false if it is over and the reports are recaps,
  aftermath, arrests counts or reactions.
- same_story_as: the owner may ALREADY have been told about this. If it is the same incident as one in the
  "Already reported" list, give that number; otherwise null.
- new_development: only if same_story_as is set: true when there is materially new, important information
  (escalation, new victims, spreading to new areas, new danger); false for recaps, reactions, updated tallies.
- Write the alert text in ENGLISH, concise: title (max 10 words), summary (max 2 short sentences: what happened,
  current state), what_to_do (one short practical sentence ONLY if relevant to someone nearby right now, e.g.
  areas to avoid; else empty). No speculation; state what is known.
Then call record_verdict."""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["confirmed", "unconfirmed", "rejected"]},
        "severity": {"type": "integer", "minimum": 0, "maximum": 5},
        "category": {"type": "string", "enum": CATEGORIES},
        "title": {"type": "string"}, "summary": {"type": "string"}, "what_to_do": {"type": "string"},
        "place": {"type": "string"}, "country": {"type": "string"},
        "lat": {"type": ["number", "null"]}, "lon": {"type": ["number", "null"]},
        "independent_sources": {"type": "integer"}, "still_ongoing": {"type": "boolean"},
        "same_story_as": {"type": ["integer", "null"]}, "new_development": {"type": "boolean"},
        "reasoning": {"type": "string"}},
    "required": ["status", "severity", "title", "summary", "still_ongoing", "reasoning"]}


class NewsPipeline:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.store = app.store
        self.db = app.store.db
        self.db.executescript(SCHEMA)
        for col in ("ended INTEGER DEFAULT 0", "indep INTEGER DEFAULT 0"):
            try:
                self.db.execute(f"ALTER TABLE news_clusters ADD COLUMN {col}")
            except Exception:  # noqa: BLE001 - column already exists
                pass
        self.llm: LLM = app.llm
        self.ncfg = self.cfg.get("news", {}) or {}
        self.official = {f["id"]: f.get("authority", f["id"]) for f in self.ncfg.get("feeds", []) if f.get("official")}

    # ------------------------------------------------------------ ingest
    async def fetch_all(self) -> tuple[list[dict], dict]:
        feeds = self.ncfg.get("feeds", []) or []
        health: dict = {}

        async def one(f):
            try:
                if f.get("type") == "telegram":
                    r = await self.app.client.get(f"https://t.me/s/{f['channel']}")
                    r.raise_for_status()
                    items = parse_telegram_channel(r.text, f["channel"])
                else:
                    r = await self.app.client.get(f["url"])
                    r.raise_for_status()
                    items = parse_feed(r.content)
                for it in items:
                    it.update(feed=f["id"], scope=f.get("scope", "world"), lang=f.get("lang", ""))
                health[f["id"]] = {"ok": True, "n": len(items), "t": time.time()}
                return items
            except Exception as e:  # noqa: BLE001
                health[f["id"]] = {"ok": False, "err": f"{type(e).__name__}: {e}"[:150], "t": time.time()}
                return []

        results = await asyncio.gather(*(one(f) for f in feeds))
        items = [i for r in results for i in r]
        gd = self.ncfg.get("gdelt", {}) or {}
        if gd.get("enabled"):
            last = self.store.kv_get("gdelt_last", 0) or 0
            if time.time() - last >= gd.get("interval", 900):
                self.store.kv_set("gdelt_last", time.time())
                for i, q in enumerate(gd.get("queries", [])):
                    if i:
                        await asyncio.sleep(8)            # GDELT asks for max 1 request / 5 s
                    gid = f"gdelt:{q['scope']}"
                    try:
                        params = {"query": q["q"], "mode": "ArtList", "format": "json",
                                  "timespan": gd.get("timespan", "2h"),
                                  "maxrecords": str(gd.get("maxrecords", 75)), "sort": "DateDesc"}
                        r = await self.app.client.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params)
                        if r.status_code == 429:          # busy: wait and try once more, else skip this round
                            await asyncio.sleep(15)
                            r = await self.app.client.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params)
                            if r.status_code == 429:
                                health[gid] = {"ok": True, "n": 0, "note": "rate-limited, retrying next round",
                                               "t": time.time()}
                                continue
                        r.raise_for_status()
                        try:
                            data = r.json()
                        except ValueError:
                            raise RuntimeError(r.text[:120])
                        got = parse_gdelt(data)
                        for it in got:
                            it.update(feed=gid, scope=q["scope"], lang=it.get("lang") or q.get("lang", ""))
                        items += got
                        health[gid] = {"ok": True, "n": len(got), "t": time.time()}
                    except Exception as e:  # noqa: BLE001
                        health[gid] = {"ok": False, "err": f"{type(e).__name__}: {e}"[:150], "t": time.time()}
        prev = self.store.kv_get("news_health", {}) or {}
        prev.update(health)
        self.store.kv_set("news_health", prev)
        return items, health

    def store_new(self, items: list[dict], baseline: bool) -> int:
        now = time.time()
        max_age = self.ncfg.get("max_age_hours", 8) * 3600
        n = 0
        for it in items:
            url = it["url"].split("#")[0]
            iid = hashlib.sha1(url.encode()).hexdigest()[:16]
            if self.db.execute("SELECT 1 FROM news_items WHERE id=?", (iid,)).fetchone():
                continue
            pub = it.get("published") or now
            title, summary = it.get("title", ""), it.get("summary", "")
            if pub < now - max_age or (baseline and pub < now - 2 * 3600):
                status = "old"
            elif it["feed"] not in self.official and not prefilter(title, summary, it.get("lang", "")):
                status = "filtered"
            else:
                status = "new"
                n += 1
            self.db.execute("INSERT INTO news_items(id,feed,scope,url,domain,title,summary,lang,published,fetched,status) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (iid, it["feed"], it.get("scope"), url, it.get("domain") or domain_of(url), title,
                             summary, it.get("lang", ""), pub, now, status))
        return n

    # ------------------------------------------------------------ triage
    def _context(self) -> tuple[str, str]:
        locs = self.cfg.locations
        home = next((l.name for l in locs if l.profile == "home"), locs[0].name if locs else "?")
        others = ", ".join(f"{l.name} ({l.country})" for l in locs if l.name != home) or "nothing else"
        return home, others

    async def triage(self) -> int:
        limit = int(self.ncfg.get("max_triage_per_cycle", 150))
        rows = self.db.execute("SELECT * FROM news_items WHERE status='new' ORDER BY published DESC LIMIT ?",
                               (limit,)).fetchall()
        if not rows:
            return 0
        home, others = self._context()
        system = TRIAGE_SYSTEM.format(home=home, others=others)
        done = 0
        for i in range(0, len(rows), 20):
            batch = rows[i:i + 20]
            lines = [json.dumps({"id": r["id"], "source": r["domain"], "lang": r["lang"],
                                 "published": datetime.fromtimestamp(r["published"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                 "title": r["title"], "text": (r["summary"] or "")[:400]}, ensure_ascii=False)
                     for r in batch]
            out = await self.llm.structured(model=self.llm.triage_model, system=system,
                                            user="Items (one JSON per line):\n" + "\n".join(lines),
                                            tool_name="record_triage", schema=TRIAGE_SCHEMA, purpose="triage",
                                            max_tokens=4000)
            by_id = {o.get("id"): o for o in out.get("items", []) if isinstance(o, dict)}
            for r in batch:
                t = by_id.get(r["id"])
                if t is None:
                    continue           # not answered: retry next cycle
                status = "relevant" if t.get("relevant") and (t.get("severity") or 0) >= 1 else "irrelevant"
                self.db.execute("UPDATE news_items SET status=?, triage=? WHERE id=?",
                                (status, json.dumps(t, ensure_ascii=False), r["id"]))
                done += 1
        return done

    # ------------------------------------------------------------ clustering
    def cluster(self) -> set[int]:
        changed: set[int] = set()
        now = time.time()
        rows = self.db.execute("SELECT * FROM news_items WHERE status='relevant' AND cluster IS NULL ORDER BY published").fetchall()
        for r in rows:
            t = json.loads(r["triage"])
            cat = t.get("category") if t.get("category") in CATEGORIES else "other"
            toks = tokens(f"{t.get('title_en', '')} {t.get('summary_en', '')}")
            lat, lon = t.get("lat"), t.get("lon")
            best, best_score = None, 0.0
            for c in self.db.execute("SELECT * FROM news_clusters WHERE last_update > ?", (now - 36 * 3600,)):
                if GROUP.get(c["category"]) != GROUP.get(cat):
                    continue
                if lat is not None and c["lat"] is not None:
                    dist = haversine_km(lat, lon, c["lat"], c["lon"])
                    if dist > 40:
                        continue
                elif (t.get("country") or "").upper() != (c["country"] or "").upper():
                    continue
                score = jaccard(toks, set(json.loads(c["tokens"] or "[]")))
                same_place = (t.get("place") or "").lower() == (c["place"] or "").lower() and t.get("place")
                dt = abs((r["published"] or now) - (c["first_published"] or now))
                if same_place and dt < 12 * 3600:
                    score += 0.15
                # Unrest, strikes, outages, hazards: one place + one evening = almost always one story
                # (e.g. clashes, looting, arrests, politicians' reactions). Violent crime stays text-based.
                recent = (r["published"] or now) - (c["last_update"] or now) < 18 * 3600
                if GROUP.get(cat) != "violence" and (dt < 8 * 3600 or recent):
                    near = (lat is not None and c["lat"] is not None and haversine_km(lat, lon, c["lat"], c["lon"]) <= 15)
                    if near or same_place:
                        score += 0.3
                if score > best_score:
                    best, best_score = c, score
            if best is not None and best_score >= 0.2:
                cid = best["id"]
                merged = set(json.loads(best["tokens"] or "[]")) | toks
                sev = max(best["severity"] or 0, t.get("severity") or 0) if best["verified"] != "confirmed" else best["severity"]
                self.db.execute("UPDATE news_clusters SET tokens=?, severity=?, world_significant=?, last_update=? WHERE id=?",
                                (json.dumps(sorted(merged)[:300]), sev,
                                 int(bool(best["world_significant"]) or bool(t.get("world_significant"))), now, cid))
            else:
                cur = self.db.execute(
                    "INSERT INTO news_clusters(category,title,summary,place,country,lat,lon,severity,world_significant,"
                    "first_seen,first_published,last_update,tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cat, t.get("title_en", r["title"]), t.get("summary_en", ""), t.get("place", ""),
                     (t.get("country") or "").upper(), lat, lon, t.get("severity") or 1,
                     int(bool(t.get("world_significant"))), now, r["published"] or now, now, json.dumps(sorted(toks))))
                cid = cur.lastrowid
            self.db.execute("UPDATE news_items SET cluster=? WHERE id=?", (cid, r["id"]))
            changed.add(cid)
        return changed

    # ------------------------------------------------------------ events
    def cluster_items(self, cid: int):
        return self.db.execute("SELECT * FROM news_items WHERE cluster=? ORDER BY published", (cid,)).fetchall()

    def to_event(self, cid: int) -> Event:
        c = self.db.execute("SELECT * FROM news_clusters WHERE id=?", (cid,)).fetchone()
        items = self.cluster_items(cid)
        domains: dict[str, str] = {}
        for it in items:
            domains.setdefault(it["domain"], it["url"])
        t = datetime.fromtimestamp(c["first_published"] or c["first_seen"], timezone.utc)
        return Event(key=f"news:{cid}", kind="news", title=c["title"], time=t, lat=c["lat"], lon=c["lon"],
                     url=items[0]["url"] if items else "", sources=list(domains), ongoing=True,
                     details={"severity": c["severity"] or 0, "category": c["category"], "summary": c["summary"],
                              "place": c["place"], "country": c["country"], "world_significant": bool(c["world_significant"]),
                              "verified": c["verified"], "n_sources": len(domains), "what_to_do": c["what_to_do"] or "",
                              "links": dict(list(domains.items())[:5]), "cluster": cid,
                              "ended": bool(c["ended"]), "independent_sources": c["indep"] or 0,
                              "official": sorted({self.official[it["feed"]] for it in items if it["feed"] in self.official})})

    def _already_reported(self, c) -> list:
        """Recent clusters near this one that the owner has already seen (alert or digest)."""
        out = []
        for k in self.db.execute("SELECT * FROM news_clusters WHERE id != ? AND last_update > ? ORDER BY last_update DESC",
                                 (c["id"], time.time() - 48 * 3600)):
            # any category: the same riot may be filed as "unrest" by one outlet and "crime" by another
            if c["lat"] is not None and k["lat"] is not None:
                if haversine_km(c["lat"], c["lon"], k["lat"], k["lon"]) > 30:
                    continue
            elif (c["country"] or "") != (k["country"] or ""):
                continue
            ev = self.store.get_event(f"news:{k['id']}")
            if ev and ev["level"] in ("info", "high", "critical"):
                out.append(k)
            if len(out) >= 8:
                break
        return out

    def merge_into(self, src: int, dst: int) -> None:
        self.db.execute("UPDATE news_items SET cluster=? WHERE cluster=?", (dst, src))
        a = self.db.execute("SELECT tokens FROM news_clusters WHERE id=?", (src,)).fetchone()
        b = self.db.execute("SELECT tokens FROM news_clusters WHERE id=?", (dst,)).fetchone()
        toks = sorted(set(json.loads(a["tokens"] or "[]")) | set(json.loads(b["tokens"] or "[]")))[:300]
        self.db.execute("UPDATE news_clusters SET tokens=?, last_update=? WHERE id=?", (json.dumps(toks), time.time(), dst))
        self.db.execute("DELETE FROM news_clusters WHERE id=?", (src,))
        self.db.execute("DELETE FROM events WHERE key=?", (f"news:{src}",))
        self.db.execute("DELETE FROM digest WHERE event_key=? AND sent=0", (f"news:{src}",))

    async def verify(self, cid: int, potential: str = "high") -> int:
        c = self.db.execute("SELECT * FROM news_clusters WHERE id=?", (cid,)).fetchone()
        items = self.cluster_items(cid)
        home, others = self._context()
        today = datetime.now(self.cfg.tz).strftime("%A %d %B %Y, %H:%M %Z")
        reports = "\n".join(
            f"- [{it['domain']}] {datetime.fromtimestamp(it['published'], timezone.utc):%Y-%m-%d %H:%M} UTC | {it['title']}"
            f" | {(it['summary'] or '')[:300]} | {it['url']}" for it in items[:12])
        user = (f"Candidate incident: {c['title']} — {c['summary']}\nPlace: {c['place']} ({c['country']}), "
                f"category {c['category']}, provisional severity {c['severity']}.\n"
                f"{len({it['domain'] for it in items})} distinct outlet(s) so far:\n{reports}")
        n_domains = len({it["domain"] for it in items})
        known = self._already_reported(c)
        if known:
            user += "\n\nAlready reported to the owner (last 48 h, same area):\n" + "\n".join(
                f"#{k['id']}: {k['title']} — {k['summary'][:200]} (first seen "
                f"{datetime.fromtimestamp(k['first_seen'], self.cfg.tz):%a %H:%M})" for k in known)
        official = sorted({self.official[it["feed"]] for it in items if it["feed"] in self.official})
        search = 1 if n_domains < 2 and not official else 0   # corroborated or official: no search needed
        if official:
            user += (f"\n\nIncludes an OFFICIAL statement by {', '.join(official)} (the competent authority): treat the "
                     "facts it states as confirmed; judge relevance, place, severity and whether it is still ongoing.")
        model = self.llm.critical_verify_model if potential == "critical" else self.llm.verify_model
        self.store.kv_set(f"verified_domains:{cid}", n_domains)
        v = await self.llm.structured(model=model,
                                      system=VERIFY_SYSTEM.format(home=home, others=others, today=today),
                                      user=user, tool_name="record_verdict", schema=VERIFY_SCHEMA,
                                      purpose="verify", max_tokens=1500, search=search)
        known_ids = {k["id"] for k in known}
        same = v.get("same_story_as")
        if isinstance(same, int) and same in known_ids:
            target = self.db.execute("SELECT * FROM news_clusters WHERE id=?", (same,)).fetchone()
            self.merge_into(cid, same)
            log.info("cluster %s is the same story as %s (new development: %s)", cid, same, v.get("new_development"))
            if v.get("new_development") and v.get("status") == "confirmed":
                # escalation or important news: refresh the story so the level check can send an UPDATE
                self.db.execute(
                    "UPDATE news_clusters SET severity=MAX(severity, ?), title=?, summary=?, what_to_do=?, ended=?, "
                    "verified='confirmed', indep=MAX(indep, ?) WHERE id=?",
                    (int(v.get("severity") or target["severity"]), v.get("title") or target["title"],
                     v.get("summary") or target["summary"], v.get("what_to_do") or "",
                     0 if v.get("still_ongoing", True) else 1, int(v.get("independent_sources") or 0), same))
            return same
        self.db.execute(
            "UPDATE news_clusters SET ended=?, indep=? WHERE id=?",
            (0 if v.get("still_ongoing", True) else 1, int(v.get("independent_sources") or 0), cid))
        self.db.execute(
            "UPDATE news_clusters SET verified=?, verified_at=?, verify_count=verify_count+1, severity=?, category=?, "
            "title=?, summary=?, what_to_do=?, place=COALESCE(NULLIF(?,''),place), country=COALESCE(NULLIF(?,''),country), "
            "lat=COALESCE(?,lat), lon=COALESCE(?,lon), reasoning=? WHERE id=?",
            (v.get("status"), time.time(), int(v.get("severity") or c["severity"]),
             v.get("category") if v.get("category") in CATEGORIES else c["category"],
             v.get("title") or c["title"], v.get("summary") or c["summary"], v.get("what_to_do") or "",
             v.get("place") or "", (v.get("country") or "").upper(), v.get("lat"), v.get("lon"),
             (v.get("reasoning") or "")[:500], cid))
        log.info("verified cluster %s: %s (sev %s, ongoing %s)", cid, v.get("status"), v.get("severity"),
                 v.get("still_ongoing"))
        return cid

    def _needs_verify(self, ev: Event, c) -> tuple[bool, str]:
        from .policy import evaluate
        potential, _ = evaluate(replace(ev, details={**ev.details, "verified": "confirmed"}), self.cfg)
        if c["verified"] in ("rejected", "confirmed") or (c["verify_count"] or 0) >= 2:
            return False, potential
        if level_rank(potential) < level_rank("high"):
            return False, potential
        if c["verified_at"]:
            # an unconfirmed story is re-checked once, and only when a NEW outlet reports it
            before = self.store.kv_get(f"verified_domains:{c['id']}", 0) or 0
            if ev.details["n_sources"] <= before:
                return False, potential
        wait = self.ncfg.get("single_source_wait_minutes", 15) * 60
        if (ev.details["n_sources"] < 2 and not ev.details.get("official") and (ev.details["severity"] or 0) < 4
                and time.time() - c["first_seen"] < wait):
            return False, potential           # give other outlets a few minutes to report it
        return True, potential

    async def run_cycle(self) -> list[Event]:
        baseline = not self.store.kv_get("baseline:news", False)
        items, health = await self.fetch_all()
        if health and not any(h["ok"] for h in health.values()):
            raise RuntimeError("all news feeds failed: " + "; ".join(f"{k}: {v.get('err')}" for k, v in list(health.items())[:3]))
        self.store_new(items, baseline)
        if not self.llm.enabled:
            return []
        try:
            await self.triage()
        except BudgetExceeded:
            await self.app.budget_exhausted()
            return []
        except LLMError as e:
            log.error("triage failed: %s", e)
        changed = self.cluster()
        # also revisit recent clusters waiting for verification
        pending = {r["id"] for r in self.db.execute(
            "SELECT id FROM news_clusters WHERE verified IS NULL AND last_update > ? AND severity >= 2",
            (time.time() - 6 * 3600,))}
        events, done = [], set()
        for cid in sorted(changed | pending):
            c = self.db.execute("SELECT * FROM news_clusters WHERE id=?", (cid,)).fetchone()
            if c is None or cid in done:
                continue                       # merged away earlier in this cycle
            ev = self.to_event(cid)
            need, potential = self._needs_verify(ev, c) if not baseline else (False, "none")
            if need:
                try:
                    cid = await self.verify(cid, potential)
                    if cid in done:
                        continue
                    ev = self.to_event(cid)
                except BudgetExceeded:
                    await self.app.budget_exhausted()
                except LLMError as e:
                    log.error("verify failed for %s: %s", cid, e)
            done.add(cid)
            events.append(ev)
        return events

    def cleanup(self) -> None:
        cutoff = time.time() - 7 * 86400
        self.db.execute("DELETE FROM news_items WHERE fetched < ?", (cutoff,))
        self.db.execute("DELETE FROM news_clusters WHERE last_update < ?", (time.time() - 30 * 86400,))

    def health_lines(self) -> list[str]:
        h = self.store.kv_get("news_health", {}) or {}
        ok = [k for k, v in h.items() if v.get("ok")]
        bad = {k: v for k, v in h.items() if not v.get("ok")}
        lines = [f"• news feeds: {len(ok)}/{len(h)} OK"]
        for k, v in bad.items():
            lines.append(f"   ✗ {k}: {htmlmod.escape(v.get('err', ''))[:90]}")
        return lines
