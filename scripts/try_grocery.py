"""Runs the grocery workflow in your terminal: find, compare, pick, real bills, pay.
Nothing is ordered unless you type the exact 'confirm <total>' (Zepto) and pay the UPI QR,
or press 'Proceed to pay' yourself in the Amazon window.

Run from the project root:
    python scripts/try_grocery.py guilt free ice cream
    python scripts/try_grocery.py "guilt free ice cream, dark chocolate, chips"   # a basket, one bill
    python scripts/try_grocery.py guilt free ice cream + dark chocolate + chips   # same, no quotes needed
      (PowerShell drops unquoted commas, so use quotes or + on the command line;
       at the basket prompt plain commas work)
    python scripts/try_grocery.py protein bar --budget 300
    python scripts/try_grocery.py milk --place college
    python scripts/try_grocery.py curd --no-amazon          # Zepto only (no browser)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.adapters.amazon_now import AmazonNowAdapter  # noqa: E402
from app.adapters.zepto import ZeptoAdapter  # noqa: E402
from app.browser import amazon_browser, signed_in_name  # noqa: E402
from app.config import load_config  # noqa: E402
from app.mcp_client import McpHub, describe_error  # noqa: E402
from app.workflows.grocery import build_grocery_graph  # noqa: E402


async def main(a: argparse.Namespace) -> None:
    cfg = load_config()
    async with AsyncExitStack() as stack:
        adapters = {}

        if not a.no_zepto:
            hub = await stack.enter_async_context(McpHub(cfg))
            errors = await hub.connect_all(["zepto"])
            if "zepto" in errors:
                print(f"[zepto] not connected: {errors['zepto']}")
            else:
                adapters["zepto"] = ZeptoAdapter(hub)

        if not a.no_amazon:
            try:
                context = await stack.enter_async_context(amazon_browser(cfg))
                if await signed_in_name(context, cfg.amazon_now.base_url):
                    adapters["amazon_now"] = AmazonNowAdapter(context, cfg)
                else:
                    print("[amazon_now] not signed in (run scripts/amazon_login.py); left out")
            except Exception as e:  # noqa: BLE001
                print(f"[amazon_now] browser didn't start: {describe_error(e)}")

        if not adapters:
            print("No grocery app available.")
            return

        graph = build_grocery_graph(cfg, adapters).compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": 100}
        step = {"request": " ".join(a.query), "place_hint": a.place, "budget": a.budget}

        while True:
            question = None
            async for chunk in graph.astream(step, config, stream_mode="updates"):
                if "__interrupt__" in chunk:
                    question = chunk["__interrupt__"][0].value
            if question is None:
                break
            print("\n" + question["prompt"])
            step = Command(resume=await asyncio.to_thread(input, "> "))

        final = (await graph.aget_state(config)).values
        print("\n" + (final.get("done") or "Finished."))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("query", nargs="+",
                   help="what to buy: guilt free ice cream   or several: \"ice cream, chips\" / ice cream + chips")
    p.add_argument("--place", help="where to deliver, e.g. home / college (default: auto-detect)")
    p.add_argument("--budget", type=int, help="budget in rupees (default: it asks; 0 = no limit)")
    p.add_argument("--no-amazon", action="store_true", help="Zepto only")
    p.add_argument("--no-zepto", action="store_true", help="Amazon Now only")
    asyncio.run(main(p.parse_args()))