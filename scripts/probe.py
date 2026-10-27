"""Read-only probe: calls the search, address and menu tools once per app and saves
the raw responses, so Phase 2's adapters are written against real data.

Nothing is added to any cart and no order tool is reachable (the allowlist in
config.yaml is enforced on every call).

Run from the project root:
    python scripts/probe.py                       # all three apps
    python scripts/probe.py zepto                 # one app
    python scripts/probe.py --grocery "ice cream" --dish biryani
    python scripts/probe.py --address 1           # use the 2nd saved address in each app

Writes probes/<app>__<tool>.json. Send back the whole probes folder.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.mcp_client import McpHub, describe_error  # noqa: E402

OUT = ROOT / "probes"


def save(app: str, tool: str, data: Any) -> None:
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{app}__{tool}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    size = len(data) if isinstance(data, (list, dict)) else 1
    print(f"  saved {path.name}  ({type(data).__name__}, {size} top-level entries)")


def find_all(obj: Any, keys: Iterable[str]) -> list[Any]:
    """Every value stored under any of `keys`, anywhere in a nested dict/list, in order."""
    keys = set(keys)
    found: list[Any] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k in keys and isinstance(v, (str, int)) and str(v):
                    found.append(v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return found


def pick(obj: Any, keys: Iterable[str], index: int = 0) -> Optional[str]:
    values = find_all(obj, keys)
    if not values:
        return None
    return str(values[min(index, len(values) - 1)])


async def step(hub: McpHub, app: str, tool: str, args: dict) -> Any:
    print(f"  -> {tool}({json.dumps(args, ensure_ascii=False)})")
    try:
        data = await hub.call(app, tool, args)
    except Exception as e:  # noqa: BLE001
        print(f"     FAILED: {describe_error(e)}")
        save(app, f"{tool}__ERROR", {"args": args, "error": describe_error(e)})
        return None
    save(app, tool, data)
    return data


async def probe_zepto(hub: McpHub, a: argparse.Namespace) -> None:
    addresses = await step(hub, "zepto", "list_saved_addresses", {})
    address_id = pick(addresses, ["id", "addressId"], a.address)
    if not address_id:
        print("  no address id found; stopping Zepto probe")
        return
    await step(hub, "zepto", "select_saved_address", {"addressId": address_id})
    results = await step(hub, "zepto", "search_products", {"query": a.grocery})
    pvid = pick(results, ["productVariantId"])
    if pvid:
        await step(hub, "zepto", "get_product_details", {"product_variant_id": pvid})
    await step(hub, "zepto", "view_cart", {})


async def probe_swiggy(hub: McpHub, a: argparse.Namespace) -> None:
    addresses = await step(hub, "swiggy_food", "get_addresses", {})
    address_id = pick(addresses, ["id", "addressId"], a.address)
    if not address_id:
        print("  no address id found; stopping Swiggy probe")
        return
    restaurants = await step(hub, "swiggy_food", "search_restaurants",
                             {"addressId": address_id, "query": a.dish})
    await step(hub, "swiggy_food", "search_menu", {"addressId": address_id, "query": a.dish})
    restaurant_id = pick(restaurants, ["restaurantId", "id"])
    if restaurant_id:
        await step(hub, "swiggy_food", "get_restaurant_menu",
                   {"addressId": address_id, "restaurantId": restaurant_id})
    await step(hub, "swiggy_food", "get_food_cart", {"addressId": address_id})


async def probe_zomato(hub: McpHub, a: argparse.Namespace) -> None:
    addresses = await step(hub, "zomato", "get_saved_addresses_for_user", {})
    address_id = pick(addresses, ["address_id", "id"], a.address)
    if not address_id:
        print("  no address id found; stopping Zomato probe")
        return
    restaurants = await step(hub, "zomato", "get_restaurants_for_keyword",
                             {"address_id": address_id, "keyword": a.dish})
    res_id = pick(restaurants, ["res_id", "restaurant_id", "id"])
    if res_id:
        await step(hub, "zomato", "get_menu_items_listing",
                   {"res_id": res_id, "address_id": address_id})


PROBES = {"zepto": probe_zepto, "swiggy_food": probe_swiggy, "zomato": probe_zomato}


async def main(a: argparse.Namespace) -> None:
    cfg = load_config()
    apps = a.apps or [n for n in PROBES if cfg.mcp.servers.get(n) and cfg.mcp.servers[n].enabled]
    async with McpHub(cfg) as hub:
        errors = await hub.connect_all(apps)
        for app in apps:
            print(f"\n=== {app} ===")
            if app in errors:
                print(f"  CONNECT FAILED: {errors[app]}")
                continue
            await PROBES[app](hub, a)
    print(f"\nDone. Send back every file in {OUT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("apps", nargs="*", help="any of: zepto swiggy_food zomato (default: all)")
    parser.add_argument("--grocery", default="ice cream")
    parser.add_argument("--dish", default="biryani")
    parser.add_argument("--address", type=int, default=0, help="which saved address to use (0 = first)")
    args = parser.parse_args()
    unknown = [x for x in args.apps if x not in PROBES]
    if unknown:
        parser.error(f"unknown app(s): {', '.join(unknown)}. Use: {', '.join(PROBES)}")
    asyncio.run(main(args))