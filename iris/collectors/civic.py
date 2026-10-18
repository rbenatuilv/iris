"""Everyday disruptions: Italian transport strikes (official MIT calendar) and air quality (Open-Meteo / CAMS)."""
from __future__ import annotations

import html as htmlmod
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import defusedxml.ElementTree as ET

from ..models import Event

# ---------------------------------------------------------------- strikes (Italy)
STRIKES_URL = "https://scioperi.mit.gov.it/mit2/public/scioperi/rss"
TITLE_RE = re.compile(r"Data inizio:\s*(\d{2}/\d{2}/\d{4}).*?Settore:\s*(.*?)\s*-\s*Rilevanza:\s*(.*?)\s*-\s*"
                      r"Regione:\s*(.*?)\s*-\s*Provincia:\s*(.*)$", re.S)
SECTOR_EN = {"trasporto pubblico locale": "local public transport", "ferroviario": "rail", "aereo": "air travel",
             "generale": "general strike", "plurisettoriale": "multi-sector", "marittimo": "ferries/shipping",
             "trasporto merci": "freight", "autostrade": "motorways", "taxi": "taxis"}


def parse_strikes(xml: str) -> list[dict]:
    root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        m = TITLE_RE.search(title)
        if not m:
            continue
        d = datetime.strptime(m.group(1), "%d/%m/%Y").date()
        desc = re.sub(r"<[^>]+>", " ", htmlmod.unescape(item.findtext("description") or ""))
        out.append({"date": d, "sector": m.group(2).strip(), "scope": m.group(3).strip(), "region": m.group(4).strip(),
                    "province": m.group(5).strip(), "text": re.sub(r"\s+", " ", desc).strip(),
                    "guid": (item.findtext("guid") or item.findtext("link") or title).strip(),
                    "link": (item.findtext("link") or "https://scioperi.mit.gov.it").strip()})
    return out


def strike_events(strikes: list[dict], locations, scfg: dict, today: date | None = None) -> list[Event]:
    today = today or date.today()
    window = int(scfg.get("days_ahead", 2))
    sectors = [s.lower() for s in scfg.get("sectors", ["trasporto pubblico locale", "ferroviario", "aereo",
                                                       "generale", "plurisettoriale"])]
    out = []
    for s in strikes:
        if not (today <= s["date"] <= today + timedelta(days=window)):
            continue
        sector = s["sector"].lower()
        if not any(x in sector for x in sectors):
            continue
        scope = s["scope"].lower()
        for loc in locations:
            if loc.country != "IT":
                continue
            region, province = (getattr(loc, "region", None) or "").lower(), (getattr(loc, "province", None) or "").lower()
            national = scope.startswith("nazional") or s["region"].lower() == "italia"
            regional = scope.startswith(("regional", "interregional")) and region and region in s["region"].lower()
            local = (region and region in s["region"].lower()
                     and (s["province"].lower() in ("tutte", "") or (province and province in s["province"].lower())))
            if not (national or regional or local):
                continue
            # what matters most day to day: your own city's transport, and nationwide general/rail strikes
            big = (("pubblico locale" in sector and local and not national)
                   or (national and any(x in sector for x in ("generale", "ferroviario", "plurisettoriale"))))
            level = scfg.get("level_major", "high") if big else "info"
            en = next((v for k, v in SECTOR_EN.items() if k in sector), s["sector"])
            start = datetime.combine(s["date"], datetime.min.time(), ZoneInfo(loc.tz))
            where = "nationwide" if national else (s["province"] if s["province"].lower() not in ("tutte", "") else s["region"])
            out.append(Event(
                key=f"strike:{loc.id}:{s['guid']}", kind="warning",
                title=f"Strike {start:%a %d %b}: {en} ({where})", time=start, url=s["link"],
                sources=["MIT strike calendar"], fixed_levels={loc.id: level}, ongoing=True,
                details={"area": where, "sector": s["sector"], "scope": s["scope"], "description": s["text"][:400],
                         "onset": start.isoformat(), "expires": (start + timedelta(days=1)).isoformat()}))
    return out


async def fetch_strikes(client, cfg) -> list[Event]:
    if not any(l.country == "IT" for l in cfg.locations):
        return []
    r = await client.get(STRIKES_URL)
    r.raise_for_status()
    return strike_events(parse_strikes(r.text), cfg.locations, cfg.get("strikes") or {})


# ---------------------------------------------------------------- air quality
AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"


def air_events(data: dict, loc, acfg: dict) -> list[Event]:
    """Daily mean PM2.5 (µg/m³) from the hourly forecast. Defaults follow Chile's episode thresholds
    (80 alert, 110 pre-emergency): ≥ info_pm25 → digest, ≥ high_pm25 → alert."""
    h = data.get("hourly", {})
    times, pm = h.get("time", []), h.get("pm2_5", [])
    by_day: dict[str, list[float]] = {}
    for t, v in zip(times, pm):
        if v is not None:
            by_day.setdefault(t[:10], []).append(v)
    out = []
    tz = ZoneInfo(loc.tz)
    for day, vals in sorted(by_day.items()):
        if len(vals) < 12:
            continue
        mean = sum(vals) / len(vals)
        level = "high" if mean >= acfg.get("high_pm25", 110) else ("info" if mean >= acfg.get("info_pm25", 75) else None)
        if not level:
            continue
        start = datetime.fromisoformat(day).replace(tzinfo=tz)
        label = "Hazardous" if mean >= acfg.get("high_pm25", 110) else "Very poor"
        out.append(Event(key=f"air:{loc.id}:{day}", kind="forecast",
                         title=f"{label} air quality in {loc.name} on {start:%a %d %b}: PM2.5 ≈ {mean:.0f} µg/m³",
                         time=start, lat=loc.lat, lon=loc.lon, url="https://open-meteo.com/en/docs/air-quality-api",
                         sources=["Open-Meteo air quality (CAMS)"], fixed_levels={loc.id: level}, ongoing=True,
                         details={"pm2_5": round(mean, 1)}))
    return out


async def fetch_air(client, cfg) -> list[Event]:
    out: list[Event] = []
    for loc in cfg.locations:
        r = await client.get(AQ_URL, params={"latitude": loc.lat, "longitude": loc.lon, "hourly": "pm2_5",
                                             "timezone": loc.tz, "forecast_days": 2})
        r.raise_for_status()
        out += air_events(r.json(), loc, cfg.get("air") or {})
    return out
