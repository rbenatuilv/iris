"""Decides how serious an event is for *you*: per-location profiles, world rules,
quiet hours. Pure functions: easy to test and tune."""
from __future__ import annotations

import html
from datetime import datetime, time as dtime, timedelta, timezone

from .config import Config
from .models import Event, haversine_km, level_rank, max_level

EMOJI = {"critical": "🔴 CRITICAL", "high": "🟠 HIGH", "info": "🔵 INFO"}


def _quake_level(rules: dict, mag: float, dist: float) -> str:
    for lvl in ("critical", "high", "info"):
        for min_mag, max_km in rules.get(lvl, []):
            if mag >= min_mag and dist <= max_km:
                return lvl
    return "none"


def evaluate(ev: Event, cfg: Config, now: datetime | None = None) -> tuple[str, str]:
    """Return (level, reason)."""
    now = now or datetime.now(timezone.utc)
    if not ev.ongoing and ev.time < now - timedelta(hours=cfg.get("max_event_age_hours", 6)):
        return "none", "too old"

    best, reason = "none", ""

    def consider(level: str, why: str):
        nonlocal best, reason
        if level_rank(level) > level_rank(best):
            best, reason = level, why

    # Location-bound items (warnings, forecasts)
    if ev.fixed_levels:
        for loc_id, lvl in ev.fixed_levels.items():
            loc = cfg.location(loc_id)
            consider(lvl, f"for {loc.name if loc else loc_id}")
        return best, reason

    dists = []
    if ev.lat is not None and ev.lon is not None:
        dists = sorted(((haversine_km(ev.lat, ev.lon, l.lat, l.lon), l) for l in cfg.locations), key=lambda x: x[0])

    world = cfg.get("world", {})
    if ev.kind == "quake" and ev.magnitude is not None:
        for d, loc in dists:
            consider(_quake_level(cfg.profile(loc.profile)["quake"], ev.magnitude, d), f"{d:.0f} km from {loc.name}")
        wq = world.get("quake", {})
        if ev.magnitude >= wq.get("high", 99):
            consider("high", "major earthquake (world)")
        elif ev.magnitude >= wq.get("info", 99):
            consider("info", "strong earthquake (world)")

    elif ev.kind == "gdacs":
        alert, etype = ev.details.get("alertlevel"), ev.details.get("eventtype")
        local_map = {"green": "info", "orange": "high", "red": "critical"}
        for d, loc in dists:
            if d <= cfg.profile(loc.profile).get("gdacs_radius_km", 0):
                lvl = local_map.get(alert, "none")
                if etype == "EQ" and alert != "red":
                    lvl = "none"           # local quakes are handled by the seismic feeds
                consider(lvl, f"{d:.0f} km from {loc.name}")
        wg = world.get("gdacs", {})
        if alert in ("red", "orange"):
            lvl = wg.get(alert, "none")
            if etype == "DR":
                lvl = "info" if alert == "red" else "none"
            if etype == "EQ" and alert == "orange":
                lvl = "none"
            consider(lvl, f"GDACS {alert} alert (world)")

    elif ev.kind == "news":
        d_ = ev.details
        sev = int(d_.get("severity") or 0)
        for d, loc in dists:
            prof = cfg.profile(loc.profile)
            th = prof.get("news", {})
            if d <= prof.get("news_radius_km", 30):
                where = "in " + loc.name if d < 8 else f"{d:.0f} km from {loc.name}"
                for lvl in ("critical", "high", "info"):
                    if lvl in th and sev >= th[lvl]:
                        consider(lvl, where)
                        break
            elif (d_.get("country") or "").upper() == loc.country:
                nc = cfg.get("news_country", {})
                for lvl in ("high", "info"):
                    if lvl in nc and sev >= nc[lvl]:
                        consider(lvl, f"{d_.get('place') or 'elsewhere in ' + loc.country}")
                        break
        if d_.get("world_significant"):
            nw = cfg.get("news_world", {})
            for lvl in ("high", "info"):
                if lvl in nw and sev >= nw[lvl]:
                    consider(lvl, f"{d_.get('place') or 'world'} (world)")
                    break
        # Never alert on news that has not been verified
        if d_.get("verified") == "rejected":
            return "none", "rejected by verification"
        if d_.get("verified") != "confirmed" and level_rank(best) > level_rank("info"):
            best = "info"
            reason = (reason + " · unconfirmed").strip(" ·")
        elif d_.get("ended") and level_rank(best) > level_rank("info"):
            best = "info"                      # already over: worth knowing, not worth a ping
            reason = (reason + " · over").strip(" ·")

    elif ev.kind == "tsunami":
        if ev.details.get("threat"):
            for d, loc in dists:
                if d <= 3000 or (ev.details.get("mentions_chile") and loc.country == "CL"):
                    if loc.coastal:
                        consider("critical", f"tsunami threat for the coast at {loc.name}")
                    else:
                        consider("high", f"tsunami threat for the coast near {loc.name} ({loc.name} itself is inland)")
            consider("high", "tsunami threat (world)")

    return best, reason


def in_quiet_hours(cfg: Config, now: datetime | None = None) -> bool:
    now = (now or datetime.now(timezone.utc)).astimezone(cfg.tz)
    q = cfg.get("quiet_hours") or {}
    if not q.get("start") or not q.get("end"):
        return False
    s = dtime.fromisoformat(q["start"])
    e = dtime.fromisoformat(q["end"])
    t = now.time()
    return (s <= t or t < e) if s > e else (s <= t < e)


def _fmt_time(ev: Event, cfg: Config, reason: str) -> str:
    mine = ev.time.astimezone(cfg.tz)
    s = f"{mine:%a %d %b %H:%M} your time"
    # also show local time if the event is closest to a location in another timezone
    for loc in cfg.locations:
        if loc.name in reason and loc.tz != cfg.get("timezone"):
            s += f" ({ev.time.astimezone(loc.zone):%H:%M} in {loc.name})"
            break
    return s


def format_alert(ev: Event, level: str, reason: str, cfg: Config, event_id: int, update: bool = False) -> str:
    e = html.escape
    head = EMOJI.get(level, level.upper())
    if ev.kind == "news":
        from .news import CATEGORY_LABEL
        head += " · " + CATEGORY_LABEL.get(ev.details.get("category"), "Incident")
    if update:
        head += " · UPDATE"
    lines = [f"<b>{head}</b>", f"<b>{e(ev.title)}</b>"]
    if ev.kind == "news":
        d_ = ev.details
        if d_.get("summary"):
            lines.append(e(d_["summary"]))
        place = d_.get("place") or ""
        lines.append("📍 " + e(place + (" — " if place and reason else "") + (reason or "")))
        lines.append("🕒 " + _fmt_time(ev, cfg, reason))
        n = max(d_.get("n_sources", len(ev.sources)), d_.get("independent_sources") or 0)
        status = "✅ Confirmed" if d_.get("verified") == "confirmed" else "⚠️ Unconfirmed"
        if d_.get("official"):
            status = "🏛 Official: " + ", ".join(d_["official"])
        lines.append(status + f" · {n} source{'s' if n != 1 else ''}")
        if d_.get("what_to_do"):
            lines.append("➡️ " + e(d_["what_to_do"]))
        links = d_.get("links") or {}
        if links:
            lines.append("Sources: " + ", ".join(f'<a href="{e(u, quote=True)}">{e(dm)}</a>' for dm, u in links.items()))
        if ev.lat is not None and ev.lon is not None:
            lines.append(f'<a href="https://www.openstreetmap.org/?mlat={ev.lat:.4f}&amp;mlon={ev.lon:.4f}#map=12/{ev.lat:.4f}/{ev.lon:.4f}">Map</a>')
        lines.append(f"#E{event_id}")
        return "\n".join(lines)
    if reason:
        lines.append(e(reason[0].upper() + reason[1:]))
    if ev.kind == "quake":
        depth = ev.details.get("depth_km")
        lines.append(f"{_fmt_time(ev, cfg, reason)}" + (f" · depth {depth:.0f} km" if depth is not None else ""))
    elif ev.kind == "warning":
        exp = ev.details.get("expires")
        exp_s = datetime.fromisoformat(exp).astimezone(cfg.tz).strftime("%a %d %H:%M") if exp else "?"
        lines.append(f"From {ev.time.astimezone(cfg.tz):%a %d %H:%M} to {exp_s} (your time)")
    elif ev.kind == "gdacs":
        lines.append(("Since " if ev.ongoing else "") + _fmt_time(ev, cfg, reason))
        sev = ev.details.get("severity")
        if sev:
            lines.append(e(sev))
    elif ev.kind == "tsunami":
        lines.append(_fmt_time(ev, cfg, reason))
        if ev.details.get("bulletin"):
            lines.append(e(ev.details["bulletin"]))
    if ev.sources:
        lines.append("Sources: " + e(", ".join(ev.sources)))
    links = []
    if ev.url:
        links.append(f'<a href="{e(ev.url, quote=True)}">Details</a>')
    if ev.lat is not None and ev.lon is not None and ev.kind != "forecast":
        links.append(f'<a href="https://www.openstreetmap.org/?mlat={ev.lat:.4f}&amp;mlon={ev.lon:.4f}#map=7/{ev.lat:.4f}/{ev.lon:.4f}">Map</a>')
    if links:
        lines.append(" · ".join(links))
    lines.append(f"#E{event_id}")
    return "\n".join(lines)


def digest_line(ev: Event, level: str, reason: str, cfg: Config, event_id: int) -> str:
    e = html.escape
    icon = {"critical": "🔴", "high": "🟠", "info": "🔵"}.get(level, "•")
    when = ev.time.astimezone(cfg.tz).strftime("%a %d %b %H:%M")
    return f"{icon} <b>{e(ev.title)}</b>\n    {e(reason)} · {when} · #E{event_id}"
