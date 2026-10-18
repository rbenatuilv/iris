"""Earthquake sources: USGS (global), EMSC (global, fast in Europe), INGV (Italy/Med).

Each parser returns QuakeReport tuples; the app clusters reports from different
agencies into one event (see Store.upsert_quake) and builds Events from clusters.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..models import Event


@dataclass
class QuakeReport:
    src: str
    src_id: str
    t: float          # epoch seconds
    lat: float
    lon: float
    depth: float | None
    mag: float | None
    place: str
    url: str


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_time(s: str) -> float:
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ---------------- USGS ----------------
USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"


def parse_usgs(data: dict) -> list[QuakeReport]:
    out = []
    for f in data.get("features", []):
        p, g = f.get("properties", {}), f.get("geometry", {})
        c = g.get("coordinates") or [None, None, None]
        if p.get("type", "earthquake") != "earthquake" or c[0] is None or p.get("time") is None:
            continue
        out.append(QuakeReport("USGS", f"usgs:{f['id']}", p["time"] / 1000, c[1], c[0], c[2],
                               p.get("mag"), p.get("place") or "", p.get("url") or ""))
    return out


async def fetch_usgs(client) -> list[QuakeReport]:
    r = await client.get(USGS_URL)
    r.raise_for_status()
    return parse_usgs(r.json())


# ---------------- EMSC ----------------
EMSC_URL = "https://www.seismicportal.eu/fdsnws/event/1/query"


def parse_emsc(data: dict) -> list[QuakeReport]:
    out = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        try:
            lat, lon = float(p["lat"]), float(p["lon"])
            t = _parse_time(p["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if p.get("evtype") not in (None, "ke", "fe"):   # ke = known earthquake, fe = felt
            continue
        uid = p.get("unid") or f.get("id")
        out.append(QuakeReport("EMSC", f"emsc:{uid}", t, lat, lon, p.get("depth"),
                               p.get("mag"), (p.get("flynn_region") or "").title(),
                               f"https://www.seismicportal.eu/eventdetails.html?unid={uid}"))
    return out


async def fetch_emsc(client) -> list[QuakeReport]:
    start = datetime.now(timezone.utc) - timedelta(hours=24)
    r = await client.get(EMSC_URL, params={"format": "json", "minmag": "2.5", "limit": "500",
                                           "starttime": _iso(start), "orderby": "time"})
    if r.status_code == 204:
        return []
    r.raise_for_status()
    return parse_emsc(r.json())


# ---------------- INGV ----------------
INGV_URL = "https://webservices.ingv.it/fdsnws/event/1/query"


def parse_ingv(text: str) -> list[QuakeReport]:
    # #EventID|Time|Latitude|Longitude|Depth/Km|Author|Catalog|Contributor|ContributorID|MagType|Magnitude|MagAuthor|EventLocationName|EventType
    out = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 13:
            continue
        try:
            eid, t = parts[0], _parse_time(parts[1])
            lat, lon = float(parts[2]), float(parts[3])
            depth = float(parts[4]) if parts[4] else None
            mag = float(parts[10]) if parts[10] else None
        except ValueError:
            continue
        if len(parts) > 13 and parts[13] and parts[13] not in ("earthquake",):
            continue
        out.append(QuakeReport("INGV", f"ingv:{eid}", t, lat, lon, depth, mag, parts[12],
                               f"https://terremoti.ingv.it/event/{eid}"))
    return out


async def fetch_ingv(client) -> list[QuakeReport]:
    start = datetime.now(timezone.utc) - timedelta(hours=24)
    r = await client.get(INGV_URL, params={"format": "text", "minmag": "2.0",
                                           "starttime": _iso(start), "orderby": "time"})
    if r.status_code == 204:
        return []
    r.raise_for_status()
    return parse_ingv(r.text)


# ---------------- cluster -> Event ----------------
PREFERENCE = {"INGV": 0, "USGS": 1, "EMSC": 2}


def cluster_to_event(cluster: int, rows) -> Event:
    rows = sorted(rows, key=lambda r: PREFERENCE.get(r["src"], 9))
    best = rows[0]
    mags = [r["mag"] for r in rows if r["mag"] is not None]
    mag = round(statistics.median(mags), 1) if mags else None
    place = next((r["place"] for r in rows if r["place"]), "")
    srcs = [f"{r['src']} M{r['mag']:.1f}" if r["mag"] is not None else r["src"] for r in rows]
    t = datetime.fromtimestamp(best["t"], timezone.utc)
    title = f"M{mag:.1f} earthquake" if mag is not None else "Earthquake"
    if place:
        title += f" — {place}"
    return Event(key=f"quake:{cluster}", kind="quake", title=title, time=t,
                 lat=best["lat"], lon=best["lon"], magnitude=mag, url=best["url"],
                 sources=srcs, details={"depth_km": best["depth"], "place": place,
                                        "links": {r["src"]: r["url"] for r in rows}})
