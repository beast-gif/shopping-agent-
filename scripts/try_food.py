"""Runs the food workflow in your terminal. You choose at every step; nothing is ordered
unless you type the exact 'confirm <total>' AND pay the UPI QR.

Run from the project root:
    python scripts/try_food.py biryani
    python scripts/try_food.py paradise                 # a restaurant name -> straight to its menu
    python scripts/try_food.py chicken biryani --place college
    python scripts/try_food.py meghana --restaurant      # force restaurant-name mode
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.adapters import SwiggyAdapter, ZomatoAdapter  # noqa: E402
from app.config import load_config  # noqa: E402
from app.mcp_client import McpHub  # noqa: E402
from app.workflows.food import build_food_graph  # noqa: E402


async def main(a: argparse.Namespace) -> None:
    cfg = load_config()
    async with McpHub(cfg) as hub:
        errors = await hub.connect_all(["swiggy_food", "zomato"])
        for server, err in errors.items():
            print(f"[{server}] not connected: {err}")
        adapters = {}
        if "swiggy_food" not in errors:
            adapters["swiggy"] = SwiggyAdapter(hub)
        if "zomato" not in errors:
            adapters["zomato"] = ZomatoAdapter(hub)
        if not adapters:
            return

        graph = build_food_graph(cfg, adapters).compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": uuid.uuid4().hex}}
        step = {"request": " ".join(a.query), "entry": "restaurant" if a.restaurant else ("dish" if a.dish else "auto"),
                "place_hint": a.place}

        while True:
            question = None
            async for chunk in graph.astream(step, config, stream_mode="updates"):
                if "__interrupt__" in chunk:
                    question = chunk["__interrupt__"][0].value
            if question is None:
                break
            print("\n" + question["prompt"])
            step = Command(resume=input("> "))

        final = (await graph.aget_state(config)).values
        print("\n" + (final.get("done") or "Finished."))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("query", nargs="+", help="a dish (masala dosa) or a restaurant name (nandhana palace); no quotes needed")
    p.add_argument("--place", help="where to deliver, e.g. home / college (default: auto-detect)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--restaurant", action="store_true", help="treat the query as a restaurant name")
    g.add_argument("--dish", action="store_true", help="treat the query as a dish")
    asyncio.run(main(p.parse_args()))