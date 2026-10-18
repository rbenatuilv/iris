"""'You are here': a dynamic location that follows you.

Sources, newest wins:
- Telegram live location shared with the bot (updates as you move, until you stop sharing or it expires)
- a location pin sent to the bot (kept 3 days)
- /trip <place> [days] (kept N days, default 7)

Only your latest position is stored, only on the Pi. To name the place and find its timezone, Iris asks
OpenStreetMap's Nominatim (place name, country, Italian comune) and Open-Meteo (timezone); both are free,
need no account, and are asked only when you have moved more than a few km.
While you are within `near_km` of one of your fixed places (e.g. at home in Milan) the extra location is
not used, to avoid duplicate alerts.
"""
from __future__ import annotations

import logging
import re
import time

from .config import Config, Location
from .models import haversine_km

log = logging.getLogger("iris.location")
NOMINATIM = "https://nominatim.openstreetmap.org"
PIN_DAYS = 3
FOREVER = 0x7FFFFFFF          # Telegram's live_period for "until I turn it off"


class Tracker:
    def __init__(self, app):
        self.app = app
        self.cfg: Config = app.cfg
        self.store = app.store
        self.lc = self.cfg.get("here") or {}

    # ------------------------------------------------------------ state
    def state(self) -> dict | None:
        return self.store.kv_get("here")

    def active(self) -> dict | None:
        s = self.state()
        if s and s.get("expires", 0) > time.time():
            return s
        return None

    def nearest_fixed(self, lat: float, lon: float):
        fixed = [l for l in self.cfg.locations if l.id != "here"]
        if not fixed:
            return None, 1e9
        d, loc = min(((haversine_km(lat, lon, l.lat, l.lon), l) for l in fixed), key=lambda x: x[0])
        return loc, d

    def apply(self) -> None:
        """Put the 'here' location into the config (or remove it)."""
        s = self.active()
        here = None
        if s and s.get("tz"):
            _, d = self.nearest_fixed(s["lat"], s["lon"])
            if d > float(self.lc.get("near_km", 25)):
                here = Location(id="here", name=f"{s.get('name') or 'your location'} (you)", lat=s["lat"], lon=s["lon"],
                                tz=s["tz"], country=s.get("country") or "", profile=self.lc.get("profile", "home"),
                                comune=s.get("comune"), coastal=False, region=s.get("region"),
                                province=s.get("province"))
        self.cfg.set_here(here)
        # precise position for "near you": live location or a pin, not a /trip (that's only a city)
        self.cfg.me = (s["lat"], s["lon"]) if s and s.get("source") in ("live", "pin") else None

    # ------------------------------------------------------------ lookups
    async def _reverse(self, lat: float, lon: float) -> dict:
        r = await self.app.client.get(f"{NOMINATIM}/reverse", params={"format": "jsonv2", "lat": lat, "lon": lon,
                                                                      "zoom": 10, "addressdetails": 1})
        r.raise_for_status()
        a = r.json().get("address", {})
        place = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or a.get("county") or ""
        cc = (a.get("country_code") or "").upper()
        province = re.sub(r"^(Città metropolitana di|Provincia di|Libero consorzio comunale di)\s+", "",
                          a.get("county") or a.get("province") or "", flags=re.I) or None
        return {"name": place or a.get("state") or a.get("country") or "", "country": cc,
                "comune": place if cc == "IT" else None, "region": a.get("state"), "province": province}

    async def _timezone(self, lat: float, lon: float) -> str:
        r = await self.app.client.get("https://api.open-meteo.com/v1/forecast",
                                      params={"latitude": lat, "longitude": lon, "timezone": "auto",
                                              "current": "temperature_2m", "forecast_days": 1})
        r.raise_for_status()
        return r.json().get("timezone") or "UTC"

    async def geocode(self, query: str) -> tuple[float, float, str] | None:
        r = await self.app.client.get(f"{NOMINATIM}/search", params={"q": query, "format": "jsonv2", "limit": 1})
        r.raise_for_status()
        res = r.json()
        if not res:
            return None
        return float(res[0]["lat"]), float(res[0]["lon"]), res[0].get("display_name", query)

    # ------------------------------------------------------------ updates
    async def update(self, lat: float, lon: float, source: str, expires: float) -> str | None:
        """Store a new position. Returns a message for the owner when the watched place changes."""
        old = self.state() or {}
        s = {**old, "lat": lat, "lon": lon, "source": source, "updated": time.time(), "expires": expires}
        moved = (not old.get("tz") or old.get("expires", 0) < time.time()
                 or haversine_km(lat, lon, old.get("geo_lat", 0), old.get("geo_lon", 0)) > 3)
        if moved:
            try:
                s.update(await self._reverse(lat, lon))
                s["tz"] = await self._timezone(lat, lon)
                s["geo_lat"], s["geo_lon"] = lat, lon
            except Exception as e:  # noqa: BLE001
                log.warning("location lookup failed: %s", e)
                s.setdefault("tz", old.get("tz") or self.cfg.get("timezone"))
                s.setdefault("name", old.get("name") or f"{lat:.2f}, {lon:.2f}")
        self.store.kv_set("here", s)
        before = old.get("announced")
        self.apply()
        loc, d = self.nearest_fixed(lat, lon)
        place = f"{s.get('name')}, {s.get('country')}".strip(", ")
        key = "near:" + loc.id if loc and d <= float(self.lc.get("near_km", 25)) else "at:" + place
        if key == before:
            return None
        s["announced"] = key
        self.store.kv_set("here", s)
        if key.startswith("near:"):
            return f"📍 You're near {loc.name}, which Iris already watches. Following your location quietly."
        until = "until you stop sharing" if expires - time.time() > 30 * 86400 else \
            f"until {time.strftime('%a %d %b %H:%M', time.localtime(expires))}"
        return (f"📍 Now also watching around <b>{place}</b> ({s.get('tz')}), {until}. "
                "Quiet hours and the digest follow your local time. /here for details, /here off to stop.")

    async def on_telegram_location(self, msg: dict) -> str | None:
        loc = msg.get("location") or {}
        if "latitude" not in loc:
            return None
        live = loc.get("live_period")
        if live:
            start = msg.get("date") or time.time()
            expires = start + (30 * 86400 * 12 if live >= FOREVER else live) + 600
            source = "live"
        else:
            expires = time.time() + PIN_DAYS * 86400
            source = "pin"
        return await self.update(loc["latitude"], loc["longitude"], source, expires)

    def stop(self) -> None:
        self.store.kv_set("here", None)
        self.cfg.set_here(None)
        self.cfg.me = None

    async def expire_check(self) -> str | None:
        s = self.state()
        if s and s.get("expires", 0) <= time.time():
            self.stop()
            return f"📍 Stopped watching {s.get('name') or 'your location'} (sharing ended)."
        return None

    def describe(self) -> str:
        s = self.active()
        if not s:
            return ("📍 Not following your location. Share your <b>live location</b> with me in this chat "
                    "(📎 → Location → Share My Live Location), send a pin, or use /trip &lt;place&gt;.")
        loc, d = self.nearest_fixed(s["lat"], s["lon"])
        near = f" — near {loc.name} ({d:.0f} km), already watched" if loc and d <= float(self.lc.get("near_km", 25)) else ""
        left = s["expires"] - time.time()
        until = "until you stop sharing" if left > 30 * 86400 else f"for {left / 3600:.0f} more h"
        age = (time.time() - s.get("updated", time.time())) / 60
        return (f"📍 <b>{s.get('name')}, {s.get('country')}</b>{near}\n"
                f"Source: {s.get('source')} · last update {age:.0f} min ago · {until}\n"
                f"Timezone: {s.get('tz')} (quiet hours follow it)\n"
                + (f"Near-you alerts: on (incidents within {self.lc.get('near_you_km', 1.5)} km are raised one level)\n"
                   if s.get("source") in ("live", "pin") else "")
                + "/here off to stop.")
