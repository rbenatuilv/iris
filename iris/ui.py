"""Telegram UI: categories and places for every event, the Recent view with filters, the /menu panel,
buttons on alerts (Details / Mute story / 👍 👎) and the "/" command list.

Button callbacks carry a tiny state string (Telegram allows 64 bytes):
  r:<hours>:<area>:<cat>:<lvl>   Recent view           c:<hours>:<area>:<lvl>   category picker
  d:<eid>  details    m:<eid>  mute story    f:<eid>:<1|0>  feedback    M  menu    s  status
  g  digest    h  here    u  mute 2h    U  unmute
"""
from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime

from .models import LEVELS, level_rank

LEVEL_ICON = {"critical": "🔴", "high": "🟠", "info": "🔵"}

# ---------------------------------------------------------------- categories
CATS = {  # id: (icon, label)
    "quake": ("🌋", "Earthquakes & tsunamis"),
    "weather": ("⛈", "Weather & warnings"),
    "crime": ("🚨", "Crime & security"),
    "unrest": ("✊", "Protests & unrest"),
    "transport": ("🚆", "Transport & strikes"),
    "infra": ("🔌", "Outages & accidents"),
    "health": ("🌫", "Air quality & health"),
    "world": ("🌍", "Disasters & conflicts"),
}
CAT_ALIASES = {
    "quake": "quake", "quakes": "quake", "earthquake": "quake", "earthquakes": "quake", "tsunami": "quake", "sismo": "quake",
    "weather": "weather", "warnings": "weather", "meteo": "weather", "clima": "weather", "storm": "weather",
    "crime": "crime", "security": "crime", "crimen": "crime", "seguridad": "crime",
    "unrest": "unrest", "protest": "unrest", "protests": "unrest", "riots": "unrest", "protestas": "unrest",
    "transport": "transport", "strikes": "transport", "strike": "transport", "sciopero": "transport", "huelga": "transport",
    "infra": "infra", "outages": "infra", "accidents": "infra", "blackout": "infra",
    "health": "health", "air": "health", "aire": "health",
    "disasters": "world", "disaster": "world", "conflict": "world", "conflicts": "world", "war": "world",
}
NEWS_CAT = {"crime": "crime", "terrorism": "crime", "unrest": "unrest", "strike_transport": "transport",
            "infrastructure": "infra", "accident": "infra", "fire": "weather", "weather": "weather",
            "geohazard": "weather", "health": "health", "conflict": "world", "other": "infra"}


def categorize(kind: str, key: str, details: dict | None) -> str:
    details = details or {}
    if kind in ("quake", "tsunami"):
        return "quake"
    if kind == "gdacs":
        return "quake" if details.get("eventtype") == "EQ" else "world"
    if kind == "news":
        return NEWS_CAT.get(details.get("category"), "infra")
    if key.startswith("strike:"):
        return "transport"
    if key.startswith("air:"):
        return "health"
    return "weather"                                   # dpc, meteoalarm, forecasts


# ---------------------------------------------------------------- places
def area_of(cfg, fixed: dict | None, reason: str) -> str:
    """'home' (Milan + wherever you are), a country code for other watched places, or 'world'."""
    loc = None
    if fixed:
        loc = cfg.location(next(iter(fixed)))
    elif reason and "(world)" not in reason:
        loc = next((l for l in sorted(cfg.locations, key=lambda l: -len(l.name)) if l.name in reason), None)
        if loc is None and "from you" in reason:
            return "home"
    if loc is None:
        return "world"
    return "home" if loc.profile == "home" else loc.country


def area_label(cfg, area: str) -> str:
    if area == "home":
        homes = [l.name.replace(" (you)", "") for l in cfg.locations if l.profile == "home" and l.id != "here"]
        return "📍 " + (" & ".join(homes) or "Home") + " & near you"
    if area == "world":
        return "🌍 World & elsewhere"
    flag = "".join(chr(0x1F1E6 + ord(c) - 65) for c in area.upper()) if len(area) == 2 else "📌"
    names = [l.name for l in cfg.locations if l.country == area and l.profile != "home"]
    return f"{flag} {', '.join(names) or area}"


AREA_ORDER = {"home": 0, "world": 9}


def areas_available(cfg) -> list[str]:
    out = ["home"] + sorted({l.country for l in cfg.locations if l.profile != "home"})
    return out + ["world"]


# ---------------------------------------------------------------- state
LVL_CODES = {"0": None, "i": "info", "h": "high", "c": "critical"}
LVL_LABEL = {"0": "All", "i": "Info+", "h": "Alerts", "c": "Critical"}
HOURS = [6, 24, 72, 168]


def hours_label(h: int) -> str:
    return f"{h // 24}d" if h >= 48 else f"{h}h"


def parse_recent_args(arg: str, cfg) -> dict:
    """'/recent crime chile 3d alerts' → state. Unknown words are ignored."""
    st = {"h": 24, "a": "all", "c": "all", "l": "0"}
    for w in re.split(r"[\s,]+", (arg or "").lower()):
        if not w:
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([hd]?)", w)
        if m:
            st["h"] = max(1, int(float(m.group(1)) * (24 if m.group(2) == "d" else 1)))
        elif w in CAT_ALIASES:
            st["c"] = CAT_ALIASES[w]
        elif w in ("critical", "critico", "crítico"):
            st["l"] = "c"
        elif w in ("alerts", "high", "alertas", "important", "importantes"):
            st["l"] = "h"
        elif w in ("info", "all-levels"):
            st["l"] = "i"
        elif w in ("world", "mundo", "mondo"):
            st["a"] = "world"
        elif w in ("home", "milan", "milano", "here", "me", "casa"):
            st["a"] = "home"
        else:
            for l in cfg.locations:
                if w in (l.name.lower(), l.country.lower()) or (w in ("chile", "cl") and l.country == "CL") \
                        or (w in ("italy", "italia", "it") and l.country == "IT"):
                    st["a"] = "home" if l.profile == "home" else l.country
    return st


def encode(st: dict) -> str:
    return f"r:{st['h']}:{st['a']}:{st['c']}:{st['l']}"


def decode(data: str) -> dict:
    _, h, a, c, l = data.split(":")
    return {"h": int(h), "a": a, "c": c, "l": l}


# ---------------------------------------------------------------- recent view
ONGOING_KINDS = ("warning", "gdacs", "forecast")


def recent_rows(app, st: dict, limit: int = 30) -> tuple[list, int]:
    since = time.time() - st["h"] * 3600
    rows = app.store.db.execute(
        "SELECT * FROM events WHERE level IN ('info','high','critical') AND last_update >= ? "
        "ORDER BY t DESC", (since,)).fetchall()
    min_lvl = LVL_CODES.get(st["l"])
    out = []
    for r in rows:
        if (r["t"] or 0) < since and r["kind"] not in ONGOING_KINDS:
            continue                                   # old event that was merely re-checked
        p = json.loads(r["payload"] or "{}")
        cat = p.get("cat") or categorize(r["kind"], r["key"], p.get("details"))
        area = p.get("area") or area_of(app.cfg, p.get("fixed"), r["reason"] or "")
        if st["c"] != "all" and cat != st["c"]:
            continue
        if st["a"] != "all" and area != st["a"]:
            continue
        if min_lvl and level_rank(r["level"]) < level_rank(min_lvl):
            continue
        out.append((r, cat, area))
    total = len(out)
    out.sort(key=lambda x: (AREA_ORDER.get(x[2], 5), -level_rank(x[0]["level"]), -(x[0]["t"] or 0)))
    return out[:limit], total


def render_recent(app, st: dict) -> tuple[str, dict]:
    cfg = app.cfg
    rows, total = recent_rows(app, st)
    filt = [f"last {hours_label(st['h'])}"]
    if st["a"] != "all":
        filt.append(area_label(cfg, st["a"]).split(" ", 1)[1])
    if st["c"] != "all":
        filt.append(CATS[st["c"]][1])
    if st["l"] != "0":
        filt.append(LVL_LABEL[st["l"]])
    lines = [f"<b>📋 Recent</b> · {html.escape(' · '.join(filt))}"]
    if not rows:
        lines.append("\nNothing matches these filters.")
    current = None
    muted = set(app.store.kv_get("muted_stories", []) or [])
    for r, cat, area in rows:
        if area != current:
            current = area
            lines.append(f"\n<b>{html.escape(area_label(cfg, area))}</b>")
        when = datetime.fromtimestamp(r["t"], cfg.tz).strftime("%a %d %H:%M") if r["t"] else "?"
        reason = (r["reason"] or "").strip()
        lines.append(f"{LEVEL_ICON.get(r['level'], '•')}{CATS[cat][0]} <b>{html.escape(r['title'])}</b>\n"
                     f"      {html.escape(reason)}{' · ' if reason else ''}{when} · /e{r['id']}"
                     + (" · 🔕" if r["key"] in muted else ""))
    if total > len(rows):
        lines.append(f"\n…and {total - len(rows)} more: narrow the filters.")
    lines.append("\n<i>Times are yours. Tap /eN for details.</i>")
    return "\n".join(lines), recent_keyboard(cfg, st)


def _btn(text: str, data: str, on: bool = False) -> dict:
    return {"text": ("✓ " if on else "") + text, "callback_data": data}


def recent_keyboard(cfg, st: dict) -> dict:
    rows = [[_btn(hours_label(h), encode({**st, "h": h}), st["h"] == h) for h in HOURS]]
    rows.append([_btn("All", encode({**st, "a": "all"}), st["a"] == "all")] +
                [_btn(area_label(cfg, a).split(" ", 1)[0] + " " + _short_area(cfg, a), encode({**st, "a": a}), st["a"] == a)
                 for a in areas_available(cfg)])
    rows.append([_btn(LVL_LABEL[k], encode({**st, "l": k}), st["l"] == k) for k in ("0", "i", "h", "c")])
    cat_txt = "🗂 Category: " + ("All" if st["c"] == "all" else CATS[st["c"]][0] + " " + CATS[st["c"]][1])
    rows.append([_btn(cat_txt, f"c:{st['h']}:{st['a']}:{st['l']}"), _btn("⬅️ Menu", "M")])
    return {"inline_keyboard": rows}


def _short_area(cfg, a: str) -> str:
    if a == "home":
        return next((l.name for l in cfg.locations if l.profile == "home" and l.id != "here"), "Home")
    if a == "world":
        return "World"
    names = {"CL": "Chile", "IT": "Italy"}
    return names.get(a, a)


def category_keyboard(h: int, a: str, l: str) -> dict:
    items = [_btn("All categories", f"r:{h}:{a}:all:{l}")] + \
            [_btn(f"{icon} {label}", f"r:{h}:{a}:{cid}:{l}") for cid, (icon, label) in CATS.items()]
    return {"inline_keyboard": [items[i:i + 2] for i in range(0, len(items), 2)]}


# ---------------------------------------------------------------- menu
def menu() -> tuple[str, dict]:
    kb = [[_btn("📋 Recent", encode({"h": 24, "a": "all", "c": "all", "l": "0"})),
           _btn("🗂 Categories", "c:24:all:0")],
          [_btn("🟠 Alerts only (3d)", encode({"h": 72, "a": "all", "c": "all", "l": "h"})),
           _btn("📍 Here", "h")],
          [_btn("☀️ Digest now", "g"), _btn("⚙️ Status", "s")],
          [_btn("⚙️ Settings", "P"), _btn("🔕 Mute 2h", "u"), _btn("🔔 Unmute", "U")]]
    return "<b>Iris</b> — what would you like to see?", {"inline_keyboard": kb}


def alert_keyboard(eid: int) -> dict:
    return {"inline_keyboard": [[_btn("ℹ️ Details", f"d:{eid}"), _btn("🔕 Mute story", f"m:{eid}")],
                                [_btn("👍 Useful", f"f:{eid}:1"), _btn("👎 Not useful", f"f:{eid}:0")]]}


COMMANDS = [
    ("menu", "Buttons for everything"),
    ("recent", "Recent events — e.g. /recent crime chile 3d alerts"),
    ("status", "Sources, quiet hours, AI spend"),
    ("here", "Where Iris thinks you are (share live location to start)"),
    ("trip", "Watch a destination: /trip Lisbon 3"),
    ("digest", "Send the digest now"),
    ("prefs", "Your settings — or just write what you want changed"),
    ("mute", "Only critical alerts for a while: /mute 2h"),
    ("unmute", "End mute"),
    ("locations", "Watched places"),
    ("help", "All commands"),
]
