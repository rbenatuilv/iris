"""Main service: runs collectors on their schedules, evaluates events, sends
alerts, answers Telegram commands, sends the morning digest and heartbeats."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import __version__
from .collectors import feeds, quakes
from .config import Config
from .llm import LLM, BudgetExceeded, LLMError
from .models import Event, level_rank, max_level
from .policy import digest_line, evaluate, format_alert, in_quiet_hours
from .store import Store
from .telegram import Telegram

log = logging.getLogger("iris")
UA = f"Iris/{__version__} (personal hazard monitor; contact via project owner)"


def make_client(force_ipv4: bool = True) -> httpx.AsyncClient:
    # Binding to 0.0.0.0 forces IPv4: some home networks advertise IPv6 but can't route it.
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=2) if force_ipv4 \
        else httpx.AsyncHTTPTransport(retries=2)
    return httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0), follow_redirects=True,
                             headers={"User-Agent": UA}, transport=transport)


class Iris:
    def __init__(self, cfg: Config, store: Store | None = None, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.client = client or make_client(cfg.get("force_ipv4", True))
        self.tg = Telegram(self.client, cfg.secrets.get("TELEGRAM_BOT_TOKEN", ""), cfg.secrets.get("TELEGRAM_CHAT_ID"))
        self.started = time.time()
        self.crashed: set[str] = set()
        self.llm = LLM(self.client, cfg.secrets.get("ANTHROPIC_API_KEY"), self.store, cfg)
        from .news import NewsPipeline
        self.news = NewsPipeline(self)
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ sources
    def source_jobs(self) -> dict:
        cfg = self.cfg
        return {
            "usgs": lambda: self._quakes(quakes.fetch_usgs),
            "emsc": lambda: self._quakes(quakes.fetch_emsc),
            "ingv": lambda: self._quakes(quakes.fetch_ingv),
            "gdacs": lambda: feeds.fetch_gdacs(self.client),
            "ptwc": lambda: feeds.fetch_ptwc(self.client),
            "meteoalarm": lambda: feeds.fetch_meteoalarm(self.client, cfg.raw),
            "openmeteo": lambda: feeds.fetch_openmeteo(self.client, cfg),
            "news": lambda: self.news.run_cycle(),
        }

    async def _quakes(self, fetcher) -> list[Event]:
        reports = await fetcher(self.client)
        changed = set()
        for r in reports:
            cluster, ch = self.store.upsert_quake(r.src_id, r.src, r.t, r.lat, r.lon, r.depth, r.mag, r.place, r.url)
            if ch:
                changed.add(cluster)
        return [quakes.cluster_to_event(c, self.store.cluster_reports(c)) for c in changed]

    async def run_source(self, name: str, job, interval: int) -> None:
        await asyncio.sleep(random.uniform(0, 5))
        while True:
            try:
                events = await job()
                self.store.source_ok(name, len(events))
                await self.process(name, events)
            except Exception as e:  # noqa: BLE001 - a broken source must never kill the service
                log.warning("source %s failed: %s", name, e)
                row = self.store.source_fail(name, f"{type(e).__name__}: {e}")
                await self._maybe_report_source(row)
            await asyncio.sleep(interval * random.uniform(0.95, 1.05))

    async def _maybe_report_source(self, row) -> None:
        limit = self.cfg.get("source_failure_alert_minutes", 120) * 60
        since = row["last_ok"] or self.started
        if not row["notified"] and time.time() - since > limit:
            self.store.set_source_notified(row["name"])
            msg = (f"⚠️ Source <b>{html.escape(row['name'])}</b> has been failing for "
                   f"{(time.time() - since) / 3600:.1f} h: {html.escape(row['err_msg'] or '')}")
            await self.deliver_text("system:" + row["name"], "high", msg, msg)

    # ------------------------------------------------------------------ pipeline
    async def process(self, source: str, events: list[Event]) -> None:
        baseline = not self.store.kv_get(f"baseline:{source}", False)
        for ev in events:
            level, reason = evaluate(ev, self.cfg)
            prev = self.store.get_event(ev.key)
            prev_level = prev["level"] if prev else "none"
            if prev is None and level == "none":
                continue                   # irrelevant: don't even store
            new_level = max_level(prev_level, level)
            payload = {"sources": ev.sources, "url": ev.url, "details": ev.details, "magnitude": ev.magnitude,
                       "time": ev.time.isoformat(), "kind": ev.kind}
            eid = self.store.save_event(ev.key, ev.kind, ev.title, ev.lat, ev.lon, ev.time.timestamp(),
                                        new_level, reason if level_rank(level) >= level_rank(prev_level) else
                                        (prev["reason"] if prev else reason), payload)
            if level_rank(level) <= level_rank(prev_level):
                continue
            if baseline:
                if level_rank(level) >= level_rank("high"):
                    self.store.add_digest(ev.key, level, digest_line(ev, level, reason, self.cfg, eid) + " (active at start)", held=True)
                continue
            text = format_alert(ev, level, reason, self.cfg, eid, update=prev_level != "none")
            mode = await self.deliver_text(ev.key, level, text, digest_line(ev, level, reason, self.cfg, eid))
            if (mode == "sent" and ev.kind in ("quake", "tsunami", "gdacs") and "(world)" not in reason
                    and prev_level in ("none", "info") and (self.cfg.get("llm") or {}).get("context_followups", True)
                    and self.llm.enabled):
                task = asyncio.create_task(self.context_followup(ev, eid, reason))
                self._bg.add(task)
                task.add_done_callback(self._bg.discard)
        if baseline:
            self.store.kv_set(f"baseline:{source}", True)

    def muted(self) -> bool:
        return time.time() < (self.store.kv_get("mute_until", 0) or 0)

    async def deliver_text(self, key: str, level: str, text: str, digest_text: str) -> str:
        if level == "critical" or (level == "high" and not in_quiet_hours(self.cfg) and not self.muted()):
            try:
                await self.tg.send(text)
                self.store.log_alert(key, level, "sent", text)
                log.info("ALERT %s %s", level, key)
                return "sent"
            except Exception as e:  # noqa: BLE001
                log.error("telegram send failed (%s); queued for retry", e)
                outbox = self.store.kv_get("outbox", [])
                outbox.append(text)
                self.store.kv_set("outbox", outbox[-50:])
                return "queued"
        held = level != "info"
        self.store.add_digest(key, level, digest_text, held=held)
        self.store.log_alert(key, level, "held" if held else "digest", digest_text)
        return "held" if held else "digest"

    # ------------------------------------------------------------------ AI extras
    CONTEXT_SYSTEM = ("You add context to an automatic hazard alert that was just sent to someone who lives in "
                      "{home}. Search the news for reports about this specific event's impact (damage, casualties, "
                      "outages, evacuations, tsunami status, official statements). Reply in ENGLISH with at most 3 "
                      "short sentences of facts, citing outlet names in brackets. If nothing has been reported yet, "
                      "say so in one sentence. No speculation, no safety boilerplate.")

    async def context_followup(self, ev: Event, eid: int, reason: str) -> None:
        delay = float((self.cfg.get("llm") or {}).get("context_delay_minutes", 12)) * 60
        await asyncio.sleep(delay)
        home = next((l.name for l in self.cfg.locations if l.profile == "home"), "")
        when = ev.time.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        user = (f"Alert #E{eid}: {ev.title}. {reason}. Time: {when}. Coordinates: {ev.lat}, {ev.lon}. "
                f"Details: {json.dumps(ev.details, default=str)[:600]}")
        try:
            text = await self.llm.text(model=self.llm.context_model, system=self.CONTEXT_SYSTEM.format(home=home),
                                       user=user, purpose="context", max_tokens=500, search=2)
        except BudgetExceeded:
            await self.budget_exhausted()
            return
        except Exception as e:  # noqa: BLE001
            log.error("context follow-up failed: %s", e)
            return
        if text:
            await self.tg.send(f"ℹ️ <b>Context · #E{eid}</b> {html.escape(ev.title)}\n{html.escape(text)}", silent=True)

    async def budget_exhausted(self) -> None:
        day = datetime.now(self.cfg.tz).date().isoformat()
        if self.store.kv_get("budget_notified") == day:
            return
        self.store.kv_set("budget_notified", day)
        msg = (f"💸 Daily AI budget (${self.llm.daily_budget:.2f}) reached. News checks pause until midnight; "
               "earthquake, tsunami and weather alerts keep working.")
        await self.deliver_text("system:budget", "high", msg, msg)

    async def digest_overview(self, rows) -> str:
        if not rows or not self.llm.enabled or not (self.cfg.get("llm") or {}).get("digest_overview", True):
            return ""
        plain = "\n".join(re.sub(r"<[^>]+>", "", r["text"]) for r in rows)[:6000]
        home = next((l.name for l in self.cfg.locations if l.profile == "home"), "")
        try:
            return await self.llm.text(
                model=self.llm.triage_model, purpose="digest", max_tokens=250,
                system=(f"Write a 2-3 sentence morning overview, in English, of these overnight alert items for someone "
                        f"living in {home}. Lead with anything near {home}, then the other locations, then the world. "
                        "Plain text, no lists, no greeting, no advice unless an item is still active nearby."),
                user=plain)
        except Exception as e:  # noqa: BLE001
            log.warning("digest overview failed: %s", e)
            return ""

    async def flush_outbox(self) -> None:
        outbox = self.store.kv_get("outbox", [])
        while outbox:
            try:
                await self.tg.send(outbox[0])
            except Exception:  # noqa: BLE001
                break
            outbox.pop(0)
            self.store.kv_set("outbox", outbox)

    # ------------------------------------------------------------------ digest
    def _digest_text(self, r) -> str:
        """Render a digest line from the event's CURRENT data (titles/levels may have improved since)."""
        ev = self.store.get_event(r["event_key"])
        if ev is None:
            return r["text"]
        icon = {"critical": "🔴", "high": "🟠", "info": "🔵"}.get(r["level"], "•")
        when = datetime.fromtimestamp(ev["t"], self.cfg.tz).strftime("%a %d %b %H:%M") if ev["t"] else "?"
        line = (f"{icon} <b>{html.escape(ev['title'])}</b>\n    {html.escape(ev['reason'] or '')} · {when} · #E{ev['id']}")
        if "(active at start)" in (r["text"] or ""):
            line += " · active when Iris started"
        return line

    def build_digest(self) -> str:
        rows = self.store.pending_digest()
        # one line per event, even if it was queued several times
        seen, uniq = set(), []
        for r in rows:
            if r["event_key"] not in seen:
                seen.add(r["event_key"])
                uniq.append(r)
        rows = uniq
        held = [r for r in rows if r["held"]]
        info = [r for r in rows if not r["held"]]
        now = datetime.now(self.cfg.tz)
        parts = [f"<b>☀️ Iris digest — {now:%a %d %b}</b>"]
        if held:
            parts.append("\n<b>Held overnight / while muted:</b>")
            parts += [self._digest_text(r) for r in held]
        if info:
            parts.append("\n<b>For your information:</b>")
            parts += [self._digest_text(r) for r in info[-40:]]
            if len(info) > 40:
                parts.append(f"…and {len(info) - 40} more (/recent)")
        if not rows:
            parts.append("All quiet. Nothing to report for your locations.")
        parts.append("\n" + self.health_summary(short=True))
        return "\n".join(parts)

    async def send_digest(self) -> None:
        text = self.build_digest()
        overview = await self.digest_overview(self.store.pending_digest())
        if overview:
            head, _, rest = text.partition("\n")
            text = f"{head}\n<i>{html.escape(overview)}</i>\n{rest}"
        await self.tg.send(text, silent=True)
        self.store.mark_digest_sent()

    async def digest_loop(self) -> None:
        hh, mm = map(int, self.cfg.get("digest_time", "07:30").split(":"))
        while True:
            now = datetime.now(self.cfg.tz)
            today = now.date().isoformat()
            if (now.hour, now.minute) >= (hh, mm) and self.store.kv_get("last_digest") != today:
                try:
                    await self.send_digest()
                    self.store.kv_set("last_digest", today)
                    self.store.cleanup()
                    self.news.cleanup()
                except Exception as e:  # noqa: BLE001
                    log.error("digest failed: %s", e)
            await self.flush_outbox()
            await asyncio.sleep(60)

    # ------------------------------------------------------------------ health
    def health_summary(self, short: bool = False) -> str:
        now = time.time()
        bad, lines = [], []
        enabled = [n for n in self.source_jobs() if self.cfg.source(n).get("enabled")]
        rows = {r["name"]: r for r in self.store.health()}
        for name in enabled:
            r = rows.get(name)
            if not r or not r["last_ok"]:
                status = "⏳ not yet" if not r or not r["consecutive_fail"] else f"❌ failing: {html.escape((r['err_msg'] or '')[:80])}"
            elif r["consecutive_fail"]:
                status = f"⚠️ failing {r['consecutive_fail']}× (last OK {(now - r['last_ok']) / 60:.0f} min ago)"
            else:
                status = f"✅ {(now - r['last_ok']) / 60:.0f} min ago"
            if "✅" not in status:
                bad.append(name)
            lines.append(f"• {name}: {status}")
        if short:
            return "Sources: all OK ✅" if not bad else f"Sources with problems: {', '.join(bad)} (see /status)"
        return "\n".join(lines)

    async def heartbeat_loop(self) -> None:
        url = self.cfg.secrets.get("HEALTHCHECK_URL")
        if not url:
            return
        while True:
            try:
                if not self.crashed:
                    await self.client.get(url, timeout=15)
                else:
                    await self.client.post(url.rstrip("/") + "/fail", content=f"crashed: {self.crashed}", timeout=15)
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat failed: %s", e)
            await asyncio.sleep(self.cfg.get("heartbeat_interval", 300))

    # ------------------------------------------------------------------ telegram commands
    HELP = ("<b>Iris commands</b>\n"
            "/status — sources, quiet hours, mute\n"
            "/recent [hours] — recent events (default 24 h)\n"
            "/event N — details of event #EN\n"
            "/digest — send the digest now\n"
            "/mute 2h — only critical alerts for a while (/unmute)\n"
            "/locations — monitored places\n"
            "/test — send a test alert")

    async def handle(self, text: str) -> str | None:
        cmd, _, arg = text.strip().partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd in ("/start", "/help"):
            return self.HELP
        if cmd == "/status":
            up = timedelta(seconds=int(time.time() - self.started))
            mute = self.store.kv_get("mute_until", 0) or 0
            q = self.cfg.get("quiet_hours") or {}
            parts = [f"<b>Iris {__version__}</b> — up {up}",
                     (f"Quiet hours: {q['start']}–{q['end']}" if q.get("start") else "Quiet hours: off")
                     + (" (now quiet)" if in_quiet_hours(self.cfg) else ""),
                     ("Muted until " + datetime.fromtimestamp(mute, self.cfg.tz).strftime("%a %H:%M")) if self.muted() else "Not muted",
                     f"Pending in digest: {len(self.store.pending_digest())}",
                     (f"AI: ${self.llm.spent_today():.3f} today of ${self.llm.daily_budget:.2f} · "
                      f"${self.llm.spent_month():.2f} this month") if self.llm.enabled else "AI: no API key",
                     *(f"   {k}: {v['calls']} calls, ${v['usd']:.3f}" + (f", {v['searches']} searches" if v.get('searches') else "")
                       for k, v in sorted(self.llm.spend_stats().items(), key=lambda kv: -kv[1]['usd'])),
                     "", self.health_summary(), *self.news.health_lines()]
            return "\n".join(parts)
        if cmd == "/recent":
            hours = float(arg) if re.fullmatch(r"\d+(\.\d+)?", arg) else 24
            rows = self.store.recent_events(hours=hours, limit=25)
            if not rows:
                return f"Nothing relevant in the last {hours:g} h."
            icon = {"critical": "🔴", "high": "🟠", "info": "🔵"}
            def when(r):
                return datetime.fromtimestamp(r["t"], self.cfg.tz).strftime("%a %d %b %H:%M") if r["t"] else "?"
            return f"<b>Last {hours:g} h</b> (times are yours)\n" + "\n".join(
                f"{icon.get(r['level'], '•')} <b>{html.escape(r['title'])}</b>\n    {html.escape(r['reason'] or '')}"
                f" · {when(r)} · #E{r['id']}" for r in rows)
        if cmd == "/event":
            m = re.search(r"\d+", arg)
            row = self.store.get_event_by_id(int(m.group())) if m else None
            if not row:
                return "Usage: /event 12 (the number after #E)"
            p = json.loads(row["payload"] or "{}")
            t = datetime.fromtimestamp(row["t"], self.cfg.tz).strftime("%a %d %b %H:%M") if row["t"] else "?"
            lines = [f"<b>#E{row['id']} {html.escape(row['title'])}</b>", f"Level: {row['level']} — {html.escape(row['reason'] or '')}",
                     f"Time: {t} (your time)"]
            if row["lat"] is not None:
                lines.append(f"Location: {row['lat']:.3f}, {row['lon']:.3f}")
            if p.get("sources"):
                lines.append("Sources: " + html.escape(", ".join(p["sources"])))
            d = p.get("details") or {}
            for k in ("summary", "place", "what_to_do", "verified", "n_sources", "category",
                      "depth_km", "severity", "area", "expires", "country", "description"):
                if d.get(k):
                    lines.append(f"{k}: {html.escape(str(d[k]))[:500]}")
            for name, url in (d.get("links") or {}).items():
                lines.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(name)}</a>')
            if p.get("url") and not d.get("links"):
                lines.append(f'<a href="{html.escape(p["url"], quote=True)}">Details</a>')
            return "\n".join(lines)
        if cmd == "/digest":
            await self.send_digest()
            return None
        if cmd == "/mute":
            m = re.fullmatch(r"(\d+)\s*([mh]?)", arg or "2h")
            if not m:
                return "Usage: /mute 2h or /mute 30m"
            secs = int(m.group(1)) * (60 if m.group(2) == "m" else 3600)
            self.store.kv_set("mute_until", time.time() + secs)
            return f"🔕 Muted for {arg or '2h'}. Critical alerts still come through. /unmute to undo."
        if cmd == "/unmute":
            self.store.kv_set("mute_until", 0)
            return "🔔 Unmuted."
        if cmd == "/locations":
            return "\n".join(f"• {l.name} ({l.country}) — profile {l.profile}" for l in self.cfg.locations)
        if cmd == "/test":
            return "<b>🟠 HIGH · TEST</b>\nThis is a test alert. If you can read this, Iris can reach you. #E0"
        return "I don't know that command yet. /help lists what I can do. (Free-text questions arrive in phase 3.)"

    async def telegram_loop(self) -> None:
        offset = self.store.kv_get("tg_offset")
        while True:
            try:
                batch = await self.tg.updates(offset)
                if not batch:
                    await asyncio.sleep(1)
                for u in batch:
                    offset = u["update_id"] + 1
                    self.store.kv_set("tg_offset", offset)
                    msg = u.get("message") or {}
                    chat = str((msg.get("chat") or {}).get("id", ""))
                    if chat != self.tg.chat_id:
                        log.warning("ignored message from unknown chat %s", chat)
                        continue
                    try:
                        reply = await self.handle(msg.get("text") or "")
                    except Exception as e:  # noqa: BLE001
                        log.exception("command failed")
                        reply = f"Sorry, that failed: {html.escape(str(e))}"
                    if reply:
                        await self.tg.send(reply)
            except Exception as e:  # noqa: BLE001
                log.warning("telegram poll error: %s", e)
                await asyncio.sleep(10)

    # ------------------------------------------------------------------ main
    async def _guard(self, name: str, coro) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            log.exception("task %s crashed", name)
            self.crashed.add(name)
            raise

    async def run(self) -> None:
        names = [l.name for l in self.cfg.locations]
        last = self.store.kv_get("last_start", 0) or 0
        self.store.kv_set("last_start", time.time())
        if self.store.kv_get("last_digest") is None:     # first install: first digest tomorrow morning
            self.store.kv_set("last_digest", datetime.now(self.cfg.tz).date().isoformat())
        if time.time() - last > 3600:
            try:
                await self.tg.send(f"✅ Iris {__version__} started. Watching: {', '.join(names)}. /help for commands.",
                                   silent=True)
            except Exception as e:  # noqa: BLE001
                log.error("cannot reach Telegram at startup: %s", e)
        tasks = []
        for name, job in self.source_jobs().items():
            sc = self.cfg.source(name)
            if sc.get("enabled"):
                tasks.append(self._guard(name, self.run_source(name, job, int(sc.get("interval", 300)))))
        tasks += [self._guard("telegram", self.telegram_loop()), self._guard("digest", self.digest_loop()),
                  self._guard("heartbeat", self.heartbeat_loop())]
        await asyncio.gather(*tasks)
