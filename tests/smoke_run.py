"""Run the whole service for a few seconds against mocked sources."""
import asyncio, json, sys, httpx, respx, tempfile, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent)); import test_iris as t
from iris.app import Iris
from iris.config import load_config
from iris.store import Store
from iris.collectors import quakes, feeds
d=pathlib.Path(tempfile.mkdtemp()); (d/"config").mkdir(); (d/"config/defaults.yaml").write_text((t.ROOT/"config/defaults.yaml").read_text())
(d/".env").write_text("TELEGRAM_BOT_TOKEN=T\nTELEGRAM_CHAT_ID=42\nHEALTHCHECK_URL=https://hc-ping.com/abc\n")
cfg=load_config(d); cfg.raw["quiet_hours"]={}
sent=[]; polled=[0]
def upd(r):
    polled[0]+=1
    cmds={1:"/status",2:"/recent",3:"/event 1",4:"hello"}
    res=[{"update_id":polled[0],"message":{"chat":{"id":42},"text":cmds[polled[0]]}}] if polled[0] in cmds else []
    return httpx.Response(200,json={"ok":True,"result":res})
async def main():
    with respx.mock(assert_all_called=False) as m:
        m.post(url__regex=r".*/sendMessage").mock(side_effect=lambda r:(sent.append(json.loads(r.content)["text"]),httpx.Response(200,json={"ok":True,"result":{}}))[1])
        m.post(url__regex=r".*/getUpdates").mock(side_effect=upd)
        m.get(quakes.USGS_URL).mock(return_value=httpx.Response(200,json=t.usgs_doc(5.0,0,0)))
        m.get(url__startswith=quakes.EMSC_URL).mock(return_value=httpx.Response(204))
        m.get(url__startswith=quakes.INGV_URL).mock(return_value=httpx.Response(500))
        m.get(feeds.GDACS_URL).mock(return_value=httpx.Response(200,text=t.GDACS_XML))
        m.get(feeds.PTWC_URL).mock(return_value=httpx.Response(200,text=t.PTWC_XML.format(t=t.iso(t.NOW))))
        m.get(url__startswith="https://feeds.meteoalarm.org").mock(return_value=httpx.Response(200,text="<feed xmlns='http://www.w3.org/2005/Atom'/>"))
        m.get(url__startswith=feeds.OM_URL).mock(return_value=httpx.Response(200,json={"daily":{}}))
        hc=m.get("https://hc-ping.com/abc").mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as c:
            app=Iris(cfg,store=Store(":memory:"),client=c)
            for s in ("gdacs","ptwc"): app.store.kv_set(f"baseline:{s}",True)
            try: await asyncio.wait_for(app.run(), 8)
            except asyncio.TimeoutError: pass
    for s in sent: print("-----\n"+s[:500])
    print("\nheartbeats:", hc.call_count)
asyncio.run(main())
