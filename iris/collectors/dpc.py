"""Italian Civil Protection (DPC) daily criticality bulletin, per alert zone.

The bulletin is published as open data (CC-BY-4.0) on GitHub by the Dipartimento della Protezione Civile:
https://github.com/pcm-dpc/DPC-Bollettini-Criticita-Idrogeologica-Idraulica
Every day (normally by 16:00) it rates ~187 alert zones for today and tomorrow, separately for flood (idraulico),
landslide / flash-flood (idrogeologico) and thunderstorm (temporali) risk: none / yellow / orange / red.
Each zone lists its municipalities, so a location just names its comune (Milan → "Nodo Idraulico di Milano").
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..models import Event

REPO = "pcm-dpc/DPC-Bollettini-Criticita-Idrogeologica-Idraulica"
API = f"https://api.github.com/repos/{REPO}"
RAW = f"https://raw.githubusercontent.com/{REPO}/master/"
NAME_RE = re.compile(r"^files/(\d{8})_(\d{4})\.json$")

RISKS = {"Per rischio idraulico": ("flood", "river flooding"),
         "Per rischio idrogeologico": ("landslide", "landslides and flash floods"),
         "Per rischio temporali": ("thunderstorm", "severe thunderstorms")}
COLOUR_LEVEL = {"yellow": "info", "orange": "high", "red": "critical"}


def colour_of(text: str) -> str | None:
    t = (text or "").upper()
    if "ROSSA" in t:
        return "red"
    if "ARANCIONE" in t:
        return "orange"
    if "GIALLA" in t:
        return "yellow"
    return None


def zone_levels(topo: dict, comune: str) -> tuple[str, dict[str, str]] | None:
    """Return (zone name, {risk_property: colour}) for the zone that contains `comune`."""
    want = comune.strip().lower()
    for obj in topo.get("objects", {}).values():
        for g in obj.get("geometries", []):
            p = g.get("properties", {})
            if want in (c.lower() for c in p.get("Comuni", [])):
                return p.get("Nome zona", "?"), {k: colour_of(p.get(k, "")) for k in RISKS}
    return None


def events_for(day: str, topo: dict, locations, bulletin_name: str, tz_default: str = "Europe/Rome") -> list[Event]:
    out = []
    d = datetime.strptime(day, "%Y-%m-%d")
    for loc in locations:
        if loc.country != "IT":
            continue
        found = zone_levels(topo, getattr(loc, "comune", None) or loc.name)
        if not found:
            continue
        zone, levels = found
        tz = ZoneInfo(loc.tz or tz_default)
        start = d.replace(tzinfo=tz)
        end = start + timedelta(days=1)
        if end < datetime.now(timezone.utc):
            continue
        for prop, colour in levels.items():
            if not colour:
                continue
            key_risk, label = RISKS[prop]
            out.append(Event(
                key=f"dpc:{loc.id}:{day}:{key_risk}", kind="warning",
                title=f"{colour.title()} alert: {label} — {zone} ({start:%a %d %b})",
                time=start, url=f"https://github.com/{REPO}", sources=["Protezione Civile (DPC)"],
                fixed_levels={loc.id: COLOUR_LEVEL[colour]}, ongoing=True,
                details={"area": zone, "risk": key_risk, "colour": colour, "bulletin": bulletin_name,
                         "onset": start.isoformat(), "expires": end.isoformat()}))
    return out


async def latest_bulletin_name(client) -> str | None:
    """Find the newest bulletin file via the last commits touching files/ (2 small API calls)."""
    h = {"Accept": "application/vnd.github+json"}
    r = await client.get(f"{API}/commits", params={"path": "files", "per_page": 3}, headers=h)
    r.raise_for_status()
    for c in r.json():
        rc = await client.get(f"{API}/commits/{c['sha']}", headers=h)
        rc.raise_for_status()
        names = sorted(f["filename"] for f in rc.json().get("files", []) if NAME_RE.match(f["filename"]))
        if names:
            return names[-1]
    return None


async def fetch_dpc(client, cfg, store) -> list[Event]:
    """Returns warning events for all Italian locations. Downloads the zone data only when a new bulletin appears."""
    head = await latest_bulletin_name(client)
    cache = store.kv_get("dpc_cache") or {}
    if head and head != cache.get("name"):
        r = await client.get(RAW + head)
        r.raise_for_status()
        b = r.json()
        m = NAME_RE.match(head)
        day0 = datetime.strptime(m.group(1), "%Y%m%d").date()
        days = {}
        for part, offset in (("today", 0), ("tomorrow", 1)):
            url = (b.get(part) or {}).get("topo_json")
            if not url:
                continue
            t = await client.get(url, timeout=60)
            t.raise_for_status()
            topo = t.json()
            # keep only what we need: the zones of our locations (the full file is ~1 MB)
            slim = {"objects": {"z": {"geometries": []}}}
            for loc in cfg.locations:
                if loc.country == "IT":
                    zl = zone_levels(topo, getattr(loc, "comune", None) or loc.name)
                    if zl:
                        zone, levels = zl
                        props = {k: {"yellow": "GIALLA", "orange": "ARANCIONE", "red": "ROSSA"}.get(v, "NESSUNA")
                                 for k, v in levels.items()}
                        slim["objects"]["z"]["geometries"].append(
                            {"properties": {"Nome zona": zone, "Comuni": [getattr(loc, "comune", None) or loc.name],
                                            **props}})
            days[(day0 + timedelta(days=offset)).isoformat()] = slim
        cache = {"name": head, "title": b.get("name", ""), "days": days}
        store.kv_set("dpc_cache", cache)
    out: list[Event] = []
    for day, topo in (cache.get("days") or {}).items():
        out += events_for(day, topo, cfg.locations, cache.get("title", ""))
    return out
