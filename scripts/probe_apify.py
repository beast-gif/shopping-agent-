"""Probe the Apify MCP server for Blinkit and Flipkart Minutes, before writing their adapters
(the same way Zepto/Swiggy/Zomato were probed first).

1. Lists the tools the Apify server exposes (names + input schemas) -> probes/apify_tools.json
2. Runs ONE small search on each Actor at your place's coordinates   -> probes/apify_blinkit.json
                                                                       probes/apify_flipkart.json
   and prints how long each took and the first few products.

Cost: 2 small Actor runs (max 10 results each), a few US cents of Apify credit.

Run from the project root (APIFY_TOKEN in .env):
    python scripts/probe_apify.py
    python scripts/probe_apify.py --query "guilt free ice cream" --place clg
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.debug import save_debug  # noqa: E402
from app.mcp_client import McpHub, describe_error  # noqa: E402
from app.places import load_places, match_place_name  # noqa: E402

SERVER = "apify"


def find_tool(tools: dict[str, Any], word: str) -> str | None:
    """Apify names an Actor's tool after the Actor (exact naming varies), so match by word."""
    return next((name for name in tools if word in name.lower()), None)


def only_known(args: dict[str, Any], tool) -> dict[str, Any]:
    """Keeps only the arguments the tool's input schema declares (if it declares any)."""
    props = ((getattr(tool, "input_schema", None) or {}).get("properties") or {})
    return {k: v for k, v in args.items() if not props or k in props}


def preview(data: Any) -> list[dict]:
    """The product rows, wherever the response put them."""
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = next((v for k, v in data.items() if isinstance(v, list) and v and isinstance(v[0], dict)), [])
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)]


async def run(hub: McpHub, tool_name: str, args: dict[str, Any], label: str, out: str) -> None:
    print(f"\n[{label}] calling {tool_name} with {json.dumps(args)[:200]}")
    started = time.monotonic()
    try:
        result = await hub.sessions[SERVER].call_tool(tool_name, args)
    except Exception as e:  # noqa: BLE001
        print(f"  failed: {describe_error(e)}")
        return
    took = time.monotonic() - started
    text = "\n".join(getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text")
    raw = {"is_error": result.is_error, "structured": result.structured_content, "text": text}
    save_debug(out, raw)
    print(f"  {took:.1f} s, is_error={result.is_error}, text {len(text)} chars -> probes/{out}")

    data: Any = result.structured_content
    if data is None:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            data = None
    rows = preview(data)
    if not rows:
        print("  (no product list found directly; the raw response is in the probe file)")
        print("  first 600 chars:", text[:600].replace("\n", " "))
        return
    print(f"  {len(rows)} rows. Keys of the first: {sorted(rows[0].keys())[:30]}")
    for r in rows[:5]:
        name = r.get("title") or r.get("name")
        print(f"   - {str(name)[:55]:<55} price={r.get('price')} mrp={r.get('mrp')} "
              f"size={r.get('unit') or r.get('packSize')} stock={r.get('availability') or r.get('inStock')}")


async def main(a: argparse.Namespace) -> None:
    cfg = load_config()
    if "apify" not in cfg.mcp.servers:
        print("No 'apify' server in config.yaml (mcp.servers).")
        return
    book = load_places(cfg)
    place = match_place_name(book, a.place) if a.place else (book.get("home") or (book.places[0] if book.places else None))
    if not place or place.lat is None or place.lng is None:
        print("Need a place with coordinates (from its Zepto address). Run scripts/setup_places.py, or use --place.")
        return
    print(f"Place: {place.name} ({place.lat:.5f}, {place.lng:.5f})")

    async with McpHub(cfg) as hub:
        errors = await hub.connect_all([SERVER])
        if errors:
            print(f"Couldn't connect to Apify: {errors[SERVER]}")
            print("Check APIFY_TOKEN in .env (Apify Console -> Settings -> API & Integrations).")
            return
        tools = {t.name: t for t in hub.all_tools(SERVER)}
        save_debug("apify_tools.json", [{"name": t.name, "description": (t.description or "")[:1500],
                                          "input_schema": t.input_schema} for t in tools.values()])
        print("Tools:", ", ".join(tools) or "(none)", " -> probes/apify_tools.json")

        blinkit = find_tool(tools, "blinkit")
        flipkart = find_tool(tools, "flipkart")
        jobs = []
        if blinkit:
            args = {"queries": [a.query], "locations": [{"lat": place.lat, "lon": place.lng}],
                    "max_items": a.max, "include_out_of_stock": False, "include_stores": True}
            jobs.append(run(hub, blinkit, only_known(args, tools[blinkit]), "Blinkit", "apify_blinkit.json"))
        else:
            print("No Blinkit tool exposed; check the ?tools= list in config.yaml.")
        if flipkart:
            args = {"customLocations": [f"{place.lat},{place.lng},{place.name}"], "searchTerms": [a.query],
                    "maxItems": a.max, "onlyInStock": True, "includeDetails": False}
            jobs.append(run(hub, flipkart, only_known(args, tools[flipkart]), "Flipkart Minutes", "apify_flipkart.json"))
        else:
            print("No Flipkart Minutes tool exposed; check the ?tools= list in config.yaml.")
        await asyncio.gather(*jobs)          # both Actors run at the same time

    print("\nSend me probes/apify_tools.json, probes/apify_blinkit.json and probes/apify_flipkart.json.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--query", default="ice cream")
    p.add_argument("--place", help="a place from places.yaml (default: home)")
    p.add_argument("--max", type=int, default=10, help="results per Actor (keep small: you pay per result)")
    asyncio.run(main(p.parse_args()))