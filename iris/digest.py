"""Morning digest: grouped by place, most serious first, related stories merged, low-value items folded.

Layout:
    ☀️ Iris digest · Tue 20 Oct
    <AI overview, 1-2 sentences>
    ⚠️ Held overnight          (alerts held by quiet hours or mute: never merged or folded)
    📍 Milan & around          (home + wherever you are)
    🇮🇹 Italy                   (elsewhere in your home country)
    🇨🇱 Chile                   (watched places)
    🌍 World
Each section: at most SECTION_CAP lines, ordered by level, then severity, then newest; the rest fold into
"+N more". The AI call that writes the overview also proposes which items are the same story; code checks
the proposal (same section, existing ids, each item used once) and falls back to one line per item.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import ui
from .models import level_rank

SECTION_CAP = 5
HELD_CAP = 10
COUNTRY_NAMES = {"IT": "Italy", "CL": "Chile", "ES": "Spain", "FR": "France", "DE": "Germany", "CH": "Switzerland",
                 "US": "United States", "GB": "United Kingdom", "AR": "Argentina", "PE": "Peru"}


@dataclass
class Item:
    eid: int
    key: str
    title: str
    reason: str
    level: str
    t: float
    sev: int
    section: str
    held: bool
    started: bool = False                 # "active when Iris started"


@dataclass
class Entry:                              # one rendered line: a single item or a merged story
    items: list
    label: str = ""
    summary: str = ""
    level: str = field(init=False)
    sev: int = field(init=False)
    t: float = field(init=False)

    def __post_init__(self):
        self.level = max((i.level for i in self.items), key=level_rank)
        self.sev = max(i.sev for i in self.items)
        self.t = max(i.t for i in self.items)


# ---------------------------------------------------------------- collecting
def section_of(cfg, area: str, details: dict) -> str:
    if area != "world":
        return area
    country = (details.get("country") or "").upper()
    if country in {l.country for l in cfg.locations if l.profile == "home"}:
        return "nat:" + country
    if country in {l.country for l in cfg.locations if l.profile != "home"}:
        return country
    return "world"


def collect(app) -> list[Item]:
    """Pending digest rows → one Item per event (current event data, not the text queued at the time)."""
    out, seen = [], set()
    for r in app.store.pending_digest():
        if r["event_key"] in seen:
            continue
        seen.add(r["event_key"])
        ev = app.store.get_event(r["event_key"])
        started = "(active at start)" in (r["text"] or "")
        if ev is None:                    # event row gone: keep the queued text as the title
            title = re.sub(r"<[^>]+>", "", r["text"] or "").split("\n")[0].lstrip("🔴🟠🔵• ").strip()
            out.append(Item(0, r["event_key"], title, "", r["level"], r["created"] or 0, 0, "world", bool(r["held"]), started))
            continue
        p = json.loads(ev["payload"] or "{}")
        details = p.get("details") or {}
        area = p.get("area") or ui.area_of(app.cfg, p.get("fixed"), ev["reason"] or "")
        try:
            sev = int(details.get("severity") or 0)
        except (TypeError, ValueError):
            sev = 0
        out.append(Item(ev["id"], ev["key"], ev["title"] or "", ev["reason"] or "", r["level"] or ev["level"],
                        ev["t"] or r["created"] or 0, sev, section_of(app.cfg, area, details), bool(r["held"]), started))
    return out


def section_order(cfg, sec: str) -> tuple:
    if sec == "home":
        return (0, "")
    if sec.startswith("nat:"):
        return (1, sec)
    if sec == "world":
        return (9, "")
    return (5, sec)


def section_label(cfg, sec: str) -> str:
    if sec == "home":
        homes = [l.name for l in cfg.locations if l.profile == "home" and l.id != "here"]
        return "📍 " + (" & ".join(homes) or "Home") + " & around"
    if sec == "world":
        return "🌍 World"
    code = sec.split(":")[-1]
    flag = "".join(chr(0x1F1E6 + ord(c) - 65) for c in code) if len(code) == 2 and code.isalpha() else "📌"
    if code in COUNTRY_NAMES:
        return f"{flag} {COUNTRY_NAMES[code]}"
    return ui.area_label(cfg, code)


# ---------------------------------------------------------------- lines
def short_reason(cfg, it: Item, in_section: bool) -> str:
    """Drop what the section heading already says: '(world)', 'in Milan', 'from Milan'."""
    r = it.reason.replace(" (world)", "").replace("(world)", "").strip()
    if not in_section:
        return r
    out = []
    for part in [p.strip() for p in r.split(" · ") if p.strip()]:
        if it.section == "home":
            homes = [l.name for l in cfg.locations if l.profile == "home"]
            if any(part == f"in {h}" for h in homes):
                continue
            for h in homes:
                part = re.sub(rf"^(\d[\d.,]* k?m) from {re.escape(h)}$", r"\1", part)
        elif it.section != "world":
            names = [l.name for l in cfg.locations if l.country == it.section.split(":")[-1]]
            if len(names) == 1 and part == f"in {names[0]}":
                continue
        out.append(part)
    return " · ".join(out)


def fmt_when(t: float, now: datetime) -> str:
    if not t:
        return ""
    d = datetime.fromtimestamp(t, now.tzinfo)
    days = (now.date() - d.date()).days
    if days == 0:
        return d.strftime("%H:%M")
    if 0 < days < 6:
        return d.strftime("%a %H:%M")
    return d.strftime("%a %d %b")


def item_line(cfg, it: Item, now: datetime, in_section: bool = True) -> str:
    bits = [b for b in (short_reason(cfg, it, in_section), fmt_when(it.t, now)) if b]
    if it.started:
        bits.append("active when Iris started")
    if it.eid:
        bits.append(f"/e{it.eid}")
    tail = " · ".join(html.escape(b) if not b.startswith("/e") else b for b in bits)
    return f"{ui.LEVEL_ICON.get(it.level, '•')} {html.escape(it.title)}" + (f" · {tail}" if tail else "")


def entry_line(cfg, e: Entry, now: datetime) -> str:
    if len(e.items) == 1:
        return item_line(cfg, e.items[0], now)
    latest = max(e.items, key=lambda i: i.t)
    summary = f": {html.escape(e.summary)}" if e.summary else ""
    return (f"{ui.LEVEL_ICON.get(e.level, '•')} <b>{html.escape(e.label)}</b> ({len(e.items)} reports){summary}"
            + (f" · /e{latest.eid}" if latest.eid else ""))


def _rank(e) -> tuple:
    return (-level_rank(e.level), -e.sev, -e.t)


def apply_groups(items: list[Item], groups: list[dict] | None) -> list[Entry]:
    """Turn the model's proposed groups into Entries; anything invalid is ignored."""
    by_id = {i.eid: i for i in items if i.eid and not i.held}
    used, entries = set(), []
    for g in groups or []:
        ids = [x for x in dict.fromkeys(g.get("ids") or []) if isinstance(x, int) and x in by_id and x not in used]
        label = str(g.get("label") or "").strip()[:60]
        if len(ids) < 2 or not label or len({by_id[x].section for x in ids}) != 1:
            continue
        used.update(ids)
        entries.append(Entry([by_id[x] for x in ids], label, str(g.get("summary") or "").strip()[:180]))
    entries += [Entry([i]) for i in items if not i.held and i.eid not in used]
    return entries


def render(app, items: list[Item], overview: str = "", groups: list[dict] | None = None,
           now: datetime | None = None) -> str:
    cfg = app.cfg
    now = now or datetime.now(cfg.tz)
    parts = [f"<b>☀️ Iris digest · {now:%a %d %b}</b>"]
    if overview:
        parts.append(f"<i>{html.escape(overview)}</i>")
    if not items:
        parts.append("\nAll quiet. Nothing to report for your locations.")
    held = sorted([i for i in items if i.held], key=_rank)
    if held:
        parts.append("\n<b>⚠️ Held overnight</b>")
        parts += [item_line(cfg, i, now, in_section=False) for i in held[:HELD_CAP]]
        if len(held) > HELD_CAP:
            parts.append(f"<i>+{len(held) - HELD_CAP} more in 📋 Recent</i>")
    entries = apply_groups(items, groups)
    for sec in sorted({e.items[0].section for e in entries}, key=lambda s: section_order(cfg, s)):
        mine = sorted([e for e in entries if e.items[0].section == sec], key=_rank)
        parts.append(f"\n<b>{html.escape(section_label(cfg, sec))}</b>")
        shown, rest = mine[:SECTION_CAP], mine[SECTION_CAP:]
        parts += [entry_line(cfg, e, now) for e in shown]
        if rest:
            n = sum(len(e.items) for e in rest)
            if len(rest) <= 2:
                names = "; ".join(_short(e.label or e.items[0].title) for e in rest)
                parts.append(f"<i>+{n} more: {html.escape(names)}</i>")
            else:
                parts.append(f"<i>+{n} more in 📋 Recent</i>")
    parts.append("\n" + app.health_summary(short=True))
    return "\n".join(parts)


def _short(s: str, n: int = 45) -> str:
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


# ---------------------------------------------------------------- AI overview + grouping
SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string", "description": "1-2 short sentences, max 45 words"},
        "groups": {"type": "array", "items": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "integer"}},
            "label": {"type": "string", "description": "2-4 word story name, e.g. 'Riot aftermath'"},
            "summary": {"type": "string", "description": "max 20 words: the key facts across the reports"}},
            "required": ["ids", "label", "summary"]}},
    },
    "required": ["overview", "groups"],
}


def prompt_lines(app, items: list[Item]) -> str:
    return "\n".join(f"{i.eid} [{section_label(app.cfg, i.section)}]{' [held alert]' if i.held else ''} "
                     f"{i.title} — {i.reason}" for i in items if i.eid)[:7000]


def system_prompt(home: str) -> str:
    return (f"You prepare a morning digest of alert items for someone living in {home}. Each line is: id [section] "
            "title — where.\n"
            f"1. overview: 1-2 short plain sentences in English. Lead with anything near {home}, then other places, "
            "then the world. No greeting, no lists, no advice unless something is still active nearby.\n"
            "2. groups: merge items that are reports on the SAME underlying story (follow-ups, arrests after the "
            "same riots, the same unrest spreading in one country). Only items in the same [section], never "
            "[held alert] items, at least 2 ids per group. Different incidents that merely share a topic stay "
            "separate. Return an empty list if nothing should be merged.")


async def ai_pass(app, items: list[Item]) -> tuple[str, list]:
    llm_cfg = app.cfg.get("llm") or {}
    if not items or not app.llm.enabled or not llm_cfg.get("digest_overview", True):
        return "", []
    home = next((l.name for l in app.cfg.locations if l.profile == "home" and l.id != "here"), "")
    out = await app.llm.structured(model=app.llm.triage_model, purpose="digest", max_tokens=900,
                                   system=system_prompt(home), user=prompt_lines(app, items),
                                   tool_name="record_digest", schema=SCHEMA)
    return str(out.get("overview") or "").strip(), list(out.get("groups") or [])
