"""The shopping agent as a web page on this laptop.

Run from the project root:
    python scripts/web.py                  # opens http://127.0.0.1:8000 in your browser
    python scripts/web.py --port 8080
    python scripts/web.py --no-amazon      # never open Brave (Zepto only for groceries)
    python scripts/web.py --auto-place     # detect the place from Wi-Fi/GPS instead of asking

Stop with Ctrl+C. An unfinished request is saved and offered again next start.
The page listens on 127.0.0.1 only (it can place orders).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser
from contextlib import AsyncExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: E402

from app.config import load_config, resolve  # noqa: E402
from app.engine import Engine, Services  # noqa: E402
from app.web.server import EventHub, create_app  # noqa: E402


async def main(a: argparse.Namespace) -> None:
    cfg = load_config()
    cfg.locations.always_ask = not a.auto_place          # web page: tap the place every time
    db = resolve(cfg.checkpoint_db)
    db.parent.mkdir(parents=True, exist_ok=True)
    hub = EventHub()

    # Everything is opened and closed in this one task: MCP requires it, and running uvicorn
    # inside our own asyncio.run keeps Windows' default event loop, which Playwright needs.
    async with AsyncExitStack() as stack:
        services = Services(cfg, use_amazon=not a.no_amazon,
                            log=lambda text: hub.publish({"type": "log", "text": text}))
        await services.connect(stack)
        if not services.food and not services.grocery:
            print("No app connected; nothing to do.")
            return
        saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(db)))
        engine = Engine(cfg, services, saver, emit=hub.publish)
        hub.engine = engine

        question = await engine.restore()
        if question:
            hub.publish({"type": "log", "text": f"You didn't finish '{engine.thread['message']}'. "
                                                "Answer below to continue, or press Start over."})
            hub.publish({"type": "question", **question})

        server = uvicorn.Server(uvicorn.Config(create_app(hub), host="127.0.0.1", port=a.port,
                                               log_level="warning"))
        url = f"http://127.0.0.1:{a.port}"

        async def open_page() -> None:
            while not server.started:
                await asyncio.sleep(0.2)
            print(f"\nShopping agent running at {url}  (Ctrl+C to stop)")
            if not a.no_open:
                webbrowser.open(url)

        opener = asyncio.create_task(open_page())
        await server.serve()
        opener.cancel()
    print("Stopped.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-amazon", action="store_true", help="don't open Brave (Zepto only for groceries)")
    p.add_argument("--auto-place", action="store_true", help="detect the place from Wi-Fi/GPS instead of asking")
    p.add_argument("--no-open", action="store_true", help="don't open the page in your browser")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        print("\nStopped.")