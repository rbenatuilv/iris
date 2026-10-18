"""Configuration: defaults.yaml deep-merged with local.yaml, secrets from .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

HOME = Path(os.environ.get("IRIS_HOME", Path(__file__).resolve().parent.parent))


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


@dataclass
class Location:
    id: str
    name: str
    lat: float
    lon: float
    tz: str
    country: str
    profile: str
    comune: str | None = None      # Italian municipality name, for zone-level civil protection alerts
    coastal: bool = False          # tsunami threats are CRITICAL only for coastal places
    region: str | None = None      # for regional strikes (Italy), e.g. Lombardia
    province: str | None = None    # for local strikes (Italy), e.g. Milano

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


@dataclass
class Config:
    raw: dict
    home: Path
    locations: list[Location] = field(default_factory=list)
    secrets: dict[str, str] = field(default_factory=dict)
    me: tuple[float, float] | None = None     # your precise position (live location / pin), for "near you"

    @property
    def tz(self) -> ZoneInfo:
        """Your clock: the live location's timezone while travelling (if enabled), else the configured one."""
        here = self.location("here")
        if here and (self.raw.get("here") or {}).get("follow_timezone", True):
            return ZoneInfo(here.tz)
        return ZoneInfo(self.raw.get("timezone", "UTC"))

    def set_here(self, loc: "Location | None") -> None:
        self.locations = [l for l in self.locations if l.id != "here"] + ([loc] if loc else [])

    def profile(self, name: str) -> dict:
        return self.raw["profiles"][name]

    def source(self, name: str) -> dict:
        return self.raw.get("sources", {}).get(name, {"enabled": False})

    def location(self, loc_id: str) -> Location | None:
        return next((l for l in self.locations if l.id == loc_id), None)

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def db_path(self) -> Path:
        p = self.home / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p / "iris.db"


def load_config(home: Path | None = None) -> Config:
    home = Path(home or HOME)
    raw = yaml.safe_load((home / "config" / "defaults.yaml").read_text())
    local = home / "config" / "local.yaml"
    if local.exists():
        raw = deep_merge(raw, yaml.safe_load(local.read_text()) or {})
    locs = [Location(**l) for l in (raw.get("locations") or []) + (raw.get("extra_locations") or [])]
    secrets = load_env(home / ".env")
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "HEALTHCHECK_URL", "ANTHROPIC_API_KEY"):
        if os.environ.get(k):
            secrets[k] = os.environ[k]
    return Config(raw=raw, home=home, locations=locs, secrets=secrets)
