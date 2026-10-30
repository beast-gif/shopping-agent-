"""The shopping agent as one chat in the terminal: food and groceries, with memory on disk.
(For the web page instead: python scripts/web.py)

Run from the project root:
    python scripts/chat.py
    python scripts/chat.py --no-amazon        # never open the browser (Zepto only for groceries)

Then type what you want:
    biryani
    meghana foods
    guilt free ice cream under 200
    ice cream, chips, curd
    chicken momos at college
    help / quit

- Zepto, Swiggy and Zomato connect once at start (saved logins, no browser).
- Brave (Amazon Now) opens only with your first grocery request.
- Every request is a LangGraph thread saved in .checkpoints/shop.sqlite. If you close the
  terminal in the middle (Ctrl+C), the next start offers to continue at the same question.
- Nothing is ordered unless you type the exact 'confirm <total>' and pay (or press
  'Proceed to pay' yourself on Amazon).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: E402

from app.config import load_config, resolve  # noqa: E402
from app.engine import Engine, Event, Services  # noqa: E402

QUIT = {"quit", "exit", "bye", "q"}


def show(event: Event) -> None:
    kind = event["type"]
    if kind == "log":
        print(event["text"])
    elif kind == "question":
        print("\n" + event.get("prompt", ""))
    elif kind == "done":
        print("\n" + event["text"])
    elif kind == "error":
        print("\n⚠ " + event["text"])
    # 'user', 'image' (the QR also opens in Photos), 'busy': nothing to print


async def main(a: argparse.Namespace) -> None:
    cfg = load_config()
    db = resolve(cfg.checkpoint_db)
    db.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncExitStack() as stack:
        services = Services(cfg, use_amazon=not a.no_amazon)
        await services.connect(stack)
        if not services.food and not services.grocery:
            print("No app connected; nothing to do.")
            return
        saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(db)))
        engine = Engine(cfg, services, saver, emit=show)
        print("\nShopping agent ready. Type what you want (help, quit).")

        if await engine.restore():
            answer = (await asyncio.to_thread(
                input, f"\nYou didn't finish '{engine.thread['message']}'. Continue it? (Y/n) ")).strip().lower()
            if answer in ("n", "no"):
                engine.abandon()
            else:
                show({"type": "question", **engine.question})

        while True:
            prompt = "> " if engine.question else "\nYou: "
            text = (await asyncio.to_thread(input, prompt)).strip()
            if not engine.question and text.lower() in QUIT:
                break
            await engine.send(text)
    print("Bye.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--no-amazon", action="store_true", help="don't open the browser (Zepto only for groceries)")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        print("\nStopped. If you were in the middle of a request, run again to continue it.")