import asyncio, json, tarfile
from datetime import datetime
import httpx, respx
from iris.app import Iris
from iris.backup import make_backup
from iris.store import Store
from test_news import cfg  # noqa: F401


def test_backup_roundtrip_and_rotation(cfg, tmp_path):
    db = cfg.db_path
    s = Store(db)
    s.kv_set("hello", "world")
    (cfg.home / "config" / "local.yaml").write_text("quiet_hours: {start: '22:00', end: '07:00'}\n")
    for day in range(1, 10):
        out = make_backup(db, cfg.home, keep=7, now=datetime(2026, 10, day))
    files = sorted((cfg.home / "data" / "backups").glob("*.tar.gz"))
    assert len(files) == 7 and files[-1].name == "iris-backup-20261009.tar.gz"
    with tarfile.open(out) as t:
        t.extractall(tmp_path / "r", filter="data")
    assert Store(tmp_path / "r" / "iris.db").kv_get("hello") == "world"
    assert "22:00" in (tmp_path / "r" / "local.yaml").read_text()


def test_weekly_backup_goes_to_telegram(cfg):
    docs = []

    async def go():
        with respx.mock(assert_all_called=False) as m:
            m.post(url__regex=r".*/sendDocument").mock(side_effect=lambda r: (docs.append(r), httpx.Response(200, json={"ok": True, "result": {}}))[1])
            async with httpx.AsyncClient() as client:
                app = Iris(cfg, store=Store(cfg.db_path), client=client)
                await app.maybe_backup(datetime(2026, 10, 18, 3, 31, tzinfo=cfg.tz))   # a Sunday
                await app.maybe_backup(datetime(2026, 10, 18, 4, 0, tzinfo=cfg.tz))    # same day: nothing
                await app.maybe_backup(datetime(2026, 10, 19, 3, 31, tzinfo=cfg.tz))   # Monday: local only
    asyncio.run(go())
    assert len(docs) == 1 and b"iris-backup-" in docs[0].content
    assert len(list((cfg.home / "data" / "backups").glob("*.tar.gz"))) == 2
