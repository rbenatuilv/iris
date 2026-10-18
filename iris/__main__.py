"""iris run | selftest | test-alert | digest-preview"""
from __future__ import annotations

import asyncio
import logging
import sys

from . import __version__
from .app import Iris, make_client
from .config import load_config
from .store import Store


async def selftest(cfg) -> int:
    """Fetch every source once and check Telegram + heartbeat. Does not send alerts."""
    print(f"Iris {__version__} self-test\n")
    failures = 0
    async with make_client(cfg.get("force_ipv4", True)) as client:
        app = Iris(cfg, store=Store(":memory:"), client=client)
        for name, job in app.source_jobs().items():
            if not cfg.source(name).get("enabled"):
                print(f"  - {name:11s} disabled")
                continue
            if name == "news":
                _, health = await app.news.fetch_all()
                ok = [k for k, v in health.items() if v["ok"]]
                print(f"  {'✓' if ok else '✗'} {'news':11s} {len(ok)}/{len(health)} feeds OK")
                for k, v in health.items():
                    print(f"      {'✓' if v['ok'] else '✗'} {k:16s} " + (f"{v['n']} items" + (f" ({v['note']})" if v.get("note") else "") if v["ok"] else v["err"]))
                if not ok:
                    failures += 1
                continue
            try:
                events = await job()
                print(f"  ✓ {name:11s} OK ({len(events)} items)")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"  ✗ {name:11s} FAILED: {type(e).__name__}: {e}")
        token = cfg.secrets.get("TELEGRAM_BOT_TOKEN")
        if token:
            try:
                me = await app.tg.call("getMe")
                print(f"  ✓ telegram    bot @{me.get('username')}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"  ✗ telegram    FAILED: {e}")
        else:
            failures += 1
            print("  ✗ telegram    no TELEGRAM_BOT_TOKEN in .env")
        if not cfg.secrets.get("TELEGRAM_CHAT_ID"):
            failures += 1
            print("  ✗ telegram    no TELEGRAM_CHAT_ID in .env")
        if app.llm.enabled:
            try:
                for model in (app.llm.triage_model, app.llm.verify_model):
                    reply = await app.llm.text(model=model, system="Reply with the single word OK.", user="ping",
                                               purpose="selftest", max_tokens=5)
                    print(f"  ✓ claude      {model}: {reply!r}")
                print(f"      AI spend today ${app.llm.spent_today():.4f} (budget ${app.llm.daily_budget:.2f}/day)")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"  ✗ claude      FAILED: {e}")
        else:
            print("  - claude      no ANTHROPIC_API_KEY: news layer disabled")
        hc = cfg.secrets.get("HEALTHCHECK_URL")
        if hc:
            try:
                r = await client.get(hc, timeout=15)
                print(f"  ✓ healthcheck HTTP {r.status_code}")
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ healthcheck FAILED: {e}")
        else:
            print("  - healthcheck not configured")
    print(f"\n{'All good.' if not failures else f'{failures} problem(s).'}")
    return 1 if failures else 0


async def test_alert(cfg) -> int:
    async with make_client(cfg.get("force_ipv4", True)) as client:
        app = Iris(cfg, store=Store(":memory:"), client=client)
        await app.tg.send(await app.handle("/test"))
    print("Test alert sent.")
    return 0


async def digest_preview(cfg) -> int:
    app = Iris(cfg)
    print(app.build_digest())
    await app.client.aclose()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    cfg = load_config()
    if cmd == "run":
        asyncio.run(Iris(cfg).run())
        return 0
    if cmd == "selftest":
        return asyncio.run(selftest(cfg))
    if cmd == "test-alert":
        return asyncio.run(test_alert(cfg))
    if cmd == "digest-preview":
        return asyncio.run(digest_preview(cfg))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
