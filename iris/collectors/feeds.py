"""GDACS (global multi-hazard), PTWC (Pacific tsunami), Meteoalarm (EU official
weather warnings) and Open-Meteo (forecast thresholds, mainly for Chile)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import defusedxml.ElementTree as ET

from ..models import Event


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag.split(":")[-1]


def _children(el) -> dict[str, str]:
    """Map local tag name -> text for direct children (first wins), plus nested geo point."""
    out: dict[str, str] = {}
    for c in el:
        name = _local(c.tag)
        if name not in out:
            out[name] = (c.text or "").strip()
        if name in ("Point", "point"):
            for cc in c:
                out.setdefault(_local(cc.tag), (cc.text or "").strip())
        if name == "link" and c.get("href"):
            out.setdefault("href", c.get("href"))
    return out


def _dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _float(s) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ---------------- GDACS ----------------
GDACS_URL = "https://www.gdacs.org/xml/rss.xml"
GDACS_TYPES = {"EQ": "Earthquake", "TC": "Tropical cyclone", "FL": "Flood", "VO": "Volcano",
               "DR": "Drought", "WF": "Wildfire", "TS": "Tsunami"}


def parse_gdacs(xml: str) -> list[Event]:
    root = ET.fromstring(xml)
    out = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        c = _children(item)
        if c.get("iscurrent", "true").lower() == "false":
            continue
        etype, eid = c.get("eventtype", ""), c.get("eventid", "")
        level = c.get("alertlevel", "").lower()
        lat, lon = _float(c.get("lat")), _float(c.get("long"))
        if not eid or level not in ("green", "orange", "red"):
            continue
        t = _dt(c.get("fromdate", "")) or _dt(c.get("pubDate", "")) or datetime.now(timezone.utc)
        # Build a short, consistent title: "<Level> <hazard> <name> — <country>"
        where = c.get("country", "").strip() or (f"{lat:.1f}, {lon:.1f}" if lat is not None and lon is not None else "")
        name = c.get("eventname", "").strip()
        title = f"{level.title()} {GDACS_TYPES.get(etype, etype).lower()}" + (f" {name}" if name else "")
        if where:
            title += f" — {where}"
        out.append(Event(key=f"gdacs:{etype}:{eid}", kind="gdacs", title=title, time=t, lat=lat, lon=lon,
                         url=c.get("link", ""), sources=["GDACS"], ongoing=etype != "EQ",
                         details={"alertlevel": level, "eventtype": etype, "country": c.get("country", ""),
                                  "severity": c.get("severity", ""), "description": c.get("description", "")[:500]}))
    return out


async def fetch_gdacs(client) -> list[Event]:
    r = await client.get(GDACS_URL)
    r.raise_for_status()
    return parse_gdacs(r.text)


# ---------------- PTWC tsunami ----------------
PTWC_URL = "https://www.tsunami.gov/events/xml/PHEBAtom.xml"
THREAT_PATTERNS = ("TSUNAMI THREAT MESSAGE", "TSUNAMI WARNING", "HAZARDOUS TSUNAMI WAVES ARE",
                   "TSUNAMI THREAT FORECAST", "TSUNAMI WAVES REACHING")


def _text(el) -> str:
    return " ".join(t.strip() for t in el.itertext() if t.strip())


def parse_ptwc(xml: str) -> list[Event]:
    root = ET.fromstring(xml)
    feed_title = ""
    for c in root:
        if _local(c.tag) == "title":
            feed_title = re.sub(r"\s+", " ", (c.text or "")).strip()
    out = []
    for entry in root:
        if _local(entry.tag) != "entry":
            continue
        c = _children(entry)
        summary = next((_text(x) for x in entry if _local(x.tag) == "summary"), "")
        blob = f"{feed_title} {c.get('title', '')} {summary}".upper()
        threat = any(p in blob for p in THREAT_PATTERNS) and "NO TSUNAMI THREAT" not in blob
        region = re.sub(r"\s+", " ", c.get("title", "")).strip().title()
        t = _dt(c.get("updated", "")) or datetime.now(timezone.utc)
        mag = None
        m = re.search(r"MAGNITUDE[^0-9]{0,30}(\d\.\d)", blob)
        if m:
            mag = float(m.group(1))
        kind = "Tsunami THREAT" if threat else "Tsunami information statement"
        out.append(Event(key=f"tsunami:{c.get('id', region)}:{'threat' if threat else 'info'}", kind="tsunami",
                         title=f"{kind} — {region}", time=t, lat=_float(c.get("lat")), lon=_float(c.get("long")),
                         magnitude=mag, url=c.get("href", "https://www.tsunami.gov"), sources=["PTWC"],
                         details={"threat": threat, "bulletin": feed_title, "summary": summary[:600],
                                  "mentions_chile": "CHILE" in blob}))
    return out


async def fetch_ptwc(client) -> list[Event]:
    r = await client.get(PTWC_URL)
    r.raise_for_status()
    return parse_ptwc(r.text)


# ---------------- Meteoalarm ----------------
MA_URL = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-{feed}"
SEVERITY_LEVEL = {"moderate": "info", "severe": "high", "extreme": "critical"}
COLOR = {"moderate": "Yellow", "severe": "Orange", "extreme": "Red"}


def parse_meteoalarm(xml: str, areas: list[str], location_id: str) -> list[Event]:
    root = ET.fromstring(xml)
    wanted = [a.lower() for a in areas]
    out = []
    now = datetime.now(timezone.utc)
    for entry in root:
        if _local(entry.tag) != "entry":
            continue
        c = _children(entry)
        area = c.get("areaDesc", "")
        if not any(w in area.lower() for w in wanted):
            continue
        if c.get("message_type", "").lower() == "cancel" or c.get("status", "Actual") != "Actual":
            continue
        sev = c.get("severity", "").lower()
        level = SEVERITY_LEVEL.get(sev)
        expires = _dt(c.get("expires", ""))
        if not level or (expires and expires < now):
            continue
        onset = _dt(c.get("onset", "")) or _dt(c.get("effective", "")) or now
        ev_name = re.sub(r"^(yellow|orange|red|green)\s+", "", c.get("event", "Warning"), flags=re.I).strip()
        key = f"ma:{area}:{ev_name.lower()}:{onset.date().isoformat()}"
        title = f"{COLOR.get(sev, '')} {ev_name} — {area}".strip()
        out.append(Event(key=key, kind="warning", title=title, time=onset, url=c.get("href", "https://meteoalarm.org"),
                         sources=["Meteoalarm"], fixed_levels={location_id: level}, ongoing=True,
                         details={"area": area, "severity": sev, "onset": onset.isoformat(),
                                  "expires": expires.isoformat() if expires else None,
                                  "urgency": c.get("urgency", ""), "identifier": c.get("identifier", "")}))
    return out


async def fetch_meteoalarm(client, cfg) -> list[Event]:
    out: list[Event] = []
    for m in cfg.get("meteoalarm", []) or []:
        r = await client.get(MA_URL.format(feed=m["feed"]))
        r.raise_for_status()
        out += parse_meteoalarm(r.text, m["areas"], m["location"])
    return out


# ---------------- Open-Meteo forecast thresholds ----------------
OM_URL = "https://api.open-meteo.com/v1/forecast"
OM_DAILY = "precipitation_sum,wind_gusts_10m_max,temperature_2m_max,temperature_2m_min"


def evaluate_forecast(data: dict, loc, profile: dict) -> list[Event]:
    th, level = profile.get("forecast", {}), profile.get("forecast_level", "info")
    d = data.get("daily", {})
    out = []
    tz = ZoneInfo(loc.tz)
    for i, day in enumerate(d.get("time", [])):
        checks = [
            ("rain", d.get("precipitation_sum", []), th.get("rain_mm"), lambda v, x: v >= x, "Heavy rain {v:.0f} mm"),
            ("gust", d.get("wind_gusts_10m_max", []), th.get("gust_kmh"), lambda v, x: v >= x, "Wind gusts {v:.0f} km/h"),
            ("heat", d.get("temperature_2m_max", []), th.get("tmax_c"), lambda v, x: v >= x, "Extreme heat {v:.0f}°C"),
            ("cold", d.get("temperature_2m_min", []), th.get("tmin_c"), lambda v, x: v <= x, "Severe cold {v:.0f}°C"),
        ]
        for metric, series, thr, cmp, label in checks:
            if thr is None or i >= len(series) or series[i] is None:
                continue
            v = series[i]
            if cmp(v, thr):
                start = datetime.fromisoformat(day).replace(tzinfo=tz)
                out.append(Event(key=f"om:{loc.id}:{day}:{metric}", kind="forecast",
                                 title=f"Forecast: {label.format(v=v)} in {loc.name} on {start:%a %d %b}",
                                 time=start, lat=loc.lat, lon=loc.lon, url="https://open-meteo.com",
                                 sources=["Open-Meteo forecast"], fixed_levels={loc.id: level}, ongoing=True,
                                 details={"metric": metric, "value": v, "threshold": thr}))
    return out


async def fetch_openmeteo(client, cfg) -> list[Event]:
    out: list[Event] = []
    for loc in cfg.locations:
        r = await client.get(OM_URL, params={"latitude": loc.lat, "longitude": loc.lon, "daily": OM_DAILY,
                                             "timezone": loc.tz, "forecast_days": 3})
        r.raise_for_status()
        out += evaluate_forecast(r.json(), loc, cfg.profile(loc.profile))
    return out
