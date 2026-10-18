from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

LEVELS = ["none", "info", "high", "critical"]


def level_rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


def max_level(*levels: str) -> str:
    return max(levels, key=level_rank) if levels else "none"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Event:
    """A normalized, alertable thing (already deduplicated per source family)."""
    key: str                      # stable dedup key
    kind: str                     # quake | gdacs | tsunami | warning | forecast
    title: str
    time: datetime                # when it happened / starts (UTC, aware)
    lat: float | None = None
    lon: float | None = None
    magnitude: float | None = None
    url: str = ""
    sources: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    # For location-bound items (warnings, forecasts): {location_id: level}
    fixed_levels: dict[str, str] | None = None
    # For global items: level regardless of distance
    world_level: str = "none"
    ongoing: bool = False         # multi-day items (warnings, cyclones): not subject to max age
