import asyncio, json
from datetime import datetime, timedelta, timezone
import httpx, respx
from iris.app import Iris
from iris.models import Event
from iris.store import Store
from test_news import cfg  # noqa: F401


def test_all_clear_for_warning_and_tsunami(cfg):
    sent = []
    now = datetime.now(timezone.utc)

    async def go():
        with respx.mock(assert_all_called=False) as m:
            m.post(url__regex=r".*/sendMessage").mock(side_effect=lambda r: (sent.append(json.loads(r.content)["text"]),
                                                      httpx.Response(200, json={"ok": True, "result": {}}))[1])
            async with httpx.AsyncClient() as c:
                app = Iris(cfg, store=Store(":memory:"), client=c)
                for s in ("dpc", "ptwc"):
                    app.store.kv_set(f"baseline:{s}", True)
                warn = Event(key="dpc:milan:x:thunderstorm", kind="warning", title="Orange alert: severe thunderstorms — Milan",
                             time=now - timedelta(hours=20), fixed_levels={"milan": "high"}, ongoing=True,
                             details={"expires": (now + timedelta(hours=1)).isoformat()})
                await app.process("dpc", [warn])
                await app.close_expired_warnings()                     # not expired yet
                n1 = len(sent)
                warn.details["expires"] = (now - timedelta(minutes=1)).isoformat()
                await app.process("dpc", [warn])
                await app.close_expired_warnings()
                await app.close_expired_warnings()                     # only once
                tsu = Event(key="tsunami:1:threat", kind="tsunami", title="Tsunami THREAT — Central Chile", time=now,
                            lat=-35.5, lon=-72.9, details={"threat": True, "mentions_chile": True})
                await app.process("ptwc", [tsu])
                await app.close_missing("tsunami", {"tsunami:1:threat"})   # still in the feed
                n2 = len(sent)
                await app.close_missing("tsunami", set())                  # gone from the feed
                return n1, n2
    n1, n2 = asyncio.run(go())
    assert n1 == 1                                                     # the HIGH warning itself
    over = [s for s in sent if s.startswith("✅")]
    assert len(over) == 2 and "Over:" in over[0] and "thunderstorms" in over[0] and "Lifted:" in over[1]
    assert n2 == 3                                                     # warning, over, tsunami alert
