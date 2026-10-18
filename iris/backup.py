"""Backups: a consistent SQLite snapshot + your local.yaml, packed as .tar.gz.

- every night: kept on the Pi (last `keep` copies) in ~/iris/data/backups/
- once a week: also sent to your own Telegram chat, so a copy survives if the SD card dies
Secrets (.env) are never included: they are short and you can re-enter them with the installer.

Restore (on the Pi):
    sudo systemctl stop iris
    tar xzf iris-backup-YYYYMMDD.tar.gz -C /tmp/restore     # contains iris.db and local.yaml
    cp /tmp/restore/iris.db ~/iris/data/iris.db && rm -f ~/iris/data/iris.db-wal ~/iris/data/iris.db-shm
    cp /tmp/restore/local.yaml ~/iris/config/local.yaml      # if you had custom settings
    sudo systemctl start iris
"""
from __future__ import annotations

import sqlite3
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path


def make_backup(db_path: Path, home: Path, keep: int = 7, now: datetime | None = None) -> Path:
    now = now or datetime.now()
    dest_dir = home / "data" / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"iris-backup-{now:%Y%m%d}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "iris.db"
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(snap))
        with dst:
            src.backup(dst)                      # consistent even while Iris is writing
        dst.close()
        src.close()
        with tarfile.open(out, "w:gz") as tar:
            tar.add(snap, arcname="iris.db")
            local = home / "config" / "local.yaml"
            if local.exists():
                tar.add(local, arcname="local.yaml")
    backups = sorted(dest_dir.glob("iris-backup-*.tar.gz"))
    for old in backups[:-keep]:
        old.unlink()
    return out
