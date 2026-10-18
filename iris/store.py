"""SQLite persistence. Small, WAL mode, few writes (SD-card friendly)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS quake_reports (
    src_id TEXT PRIMARY KEY, src TEXT, t REAL, lat REAL, lon REAL, depth REAL,
    mag REAL, place TEXT, url TEXT, cluster INTEGER, seen REAL);
CREATE INDEX IF NOT EXISTS qr_t ON quake_reports(t);
CREATE INDEX IF NOT EXISTS qr_cluster ON quake_reports(cluster);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, kind TEXT, title TEXT,
    lat REAL, lon REAL, t REAL, level TEXT, reason TEXT, payload TEXT,
    first_seen REAL, last_update REAL);
CREATE INDEX IF NOT EXISTS ev_first ON events(first_seen);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT, level TEXT, mode TEXT,
    text TEXT, created REAL);
CREATE TABLE IF NOT EXISTS digest (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT, level TEXT, text TEXT,
    held INTEGER, created REAL, sent INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT, useful INTEGER, level TEXT, kind TEXT, created REAL);
CREATE TABLE IF NOT EXISTS source_health (
    name TEXT PRIMARY KEY, last_ok REAL, last_err REAL, err_msg TEXT,
    consecutive_fail INTEGER DEFAULT 0, items INTEGER DEFAULT 0, notified INTEGER DEFAULT 0);
"""


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    # --- kv ---
    def kv_get(self, k: str, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def kv_set(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))

    # --- quakes ---
    def upsert_quake(self, src_id, src, t, lat, lon, depth, mag, place, url) -> tuple[int, bool]:
        """Insert/update a report and assign it to a cluster. Returns (cluster_id, changed)."""
        old = self.db.execute("SELECT * FROM quake_reports WHERE src_id=?", (src_id,)).fetchone()
        if old:
            changed = (abs((old["mag"] or 0) - (mag or 0)) >= 0.05
                       or abs(old["lat"] - lat) > 0.05 or abs(old["lon"] - lon) > 0.05)
            if changed:
                self.db.execute("UPDATE quake_reports SET t=?,lat=?,lon=?,depth=?,mag=?,place=?,url=? WHERE src_id=?",
                                (t, lat, lon, depth, mag, place, url, src_id))
            return old["cluster"], changed
        from .models import haversine_km
        cluster = None
        for r in self.db.execute(
                "SELECT cluster, lat, lon, mag, src FROM quake_reports WHERE t BETWEEN ? AND ?",
                (t - 120, t + 120)):
            if r["src"] == src:
                continue
            if haversine_km(lat, lon, r["lat"], r["lon"]) <= 150 and abs((r["mag"] or 0) - (mag or 0)) <= 1.0:
                cluster = r["cluster"]
                break
        if cluster is None:
            # monotonic counter: never reuse a number, even after old reports are cleaned up
            cluster = max(int(self.kv_get("quake_cluster_seq", 0) or 0),
                          self.db.execute("SELECT COALESCE(MAX(cluster),0) FROM quake_reports").fetchone()[0]) + 1
            self.kv_set("quake_cluster_seq", cluster)
        self.db.execute("INSERT INTO quake_reports VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (src_id, src, t, lat, lon, depth, mag, place, url, cluster, time.time()))
        return cluster, True

    def cluster_reports(self, cluster: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM quake_reports WHERE cluster=? ORDER BY src", (cluster,)).fetchall()

    # --- events / alerts ---
    def get_event(self, key: str):
        return self.db.execute("SELECT * FROM events WHERE key=?", (key,)).fetchone()

    def get_event_by_id(self, eid: int):
        return self.db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()

    def save_event(self, key, kind, title, lat, lon, t, level, reason, payload: dict) -> int:
        now = time.time()
        old = self.get_event(key)
        if old:
            self.db.execute("UPDATE events SET title=?,lat=?,lon=?,t=?,level=?,reason=?,payload=?,last_update=? WHERE key=?",
                            (title, lat, lon, t, level, reason, json.dumps(payload), now, key))
            return old["id"]
        cur = self.db.execute(
            "INSERT INTO events(key,kind,title,lat,lon,t,level,reason,payload,first_seen,last_update) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (key, kind, title, lat, lon, t, level, reason, json.dumps(payload), now, now))
        return cur.lastrowid

    def recent_events(self, hours: float = 24, min_level: str = "info", limit: int = 15):
        from .models import LEVELS
        ok = LEVELS[LEVELS.index(min_level):]
        q = f"SELECT * FROM events WHERE last_update>=? AND level IN ({','.join('?' * len(ok))}) ORDER BY last_update DESC LIMIT ?"
        return self.db.execute(q, (time.time() - hours * 3600, *ok, limit)).fetchall()

    def log_alert(self, key, level, mode, text) -> None:
        self.db.execute("INSERT INTO alerts(event_key,level,mode,text,created) VALUES(?,?,?,?,?)",
                        (key, level, mode, text, time.time()))

    def add_digest(self, key, level, text, held: bool) -> None:
        self.db.execute("DELETE FROM digest WHERE event_key=? AND sent=0", (key,))
        self.db.execute("INSERT INTO digest(event_key,level,text,held,created) VALUES(?,?,?,?,?)",
                        (key, level, text, int(held), time.time()))

    def pending_digest(self):
        return self.db.execute("SELECT * FROM digest WHERE sent=0 ORDER BY held DESC, created").fetchall()

    def mark_digest_sent(self) -> None:
        self.db.execute("UPDATE digest SET sent=1 WHERE sent=0")

    # --- source health ---
    def source_ok(self, name: str, items: int) -> None:
        self.db.execute(
            "INSERT INTO source_health(name,last_ok,consecutive_fail,items,notified) VALUES(?,?,0,?,0) "
            "ON CONFLICT(name) DO UPDATE SET last_ok=excluded.last_ok, consecutive_fail=0, items=excluded.items, notified=0",
            (name, time.time(), items))

    def source_fail(self, name: str, msg: str) -> sqlite3.Row:
        self.db.execute(
            "INSERT INTO source_health(name,last_err,err_msg,consecutive_fail) VALUES(?,?,?,1) "
            "ON CONFLICT(name) DO UPDATE SET last_err=excluded.last_err, err_msg=excluded.err_msg, "
            "consecutive_fail=consecutive_fail+1", (name, time.time(), msg[:300]))
        return self.db.execute("SELECT * FROM source_health WHERE name=?", (name,)).fetchone()

    def set_source_notified(self, name: str) -> None:
        self.db.execute("UPDATE source_health SET notified=1 WHERE name=?", (name,))

    def health(self):
        return self.db.execute("SELECT * FROM source_health ORDER BY name").fetchall()

    def cleanup(self) -> None:
        now = time.time()
        self.db.execute("DELETE FROM quake_reports WHERE seen < ?", (now - 30 * 86400,))
        self.db.execute("DELETE FROM events WHERE last_update < ?", (now - 120 * 86400,))
        self.db.execute("DELETE FROM alerts WHERE created < ?", (now - 120 * 86400,))
        self.db.execute("DELETE FROM digest WHERE sent=1 AND created < ?", (now - 14 * 86400,))
