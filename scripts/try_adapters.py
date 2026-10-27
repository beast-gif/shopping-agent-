"""Runs each adapter once and prints the normalized results.

Read-only by default. With --cart it offers to add ONE cheap item per app to that app's
cart, asking y/N first. No order tool is reachable (config.yaml blocks them).

Run from the project root:
    python scripts/try_adapters.py
    python scripts/try_adapters.py --grocery "protein ice cream" --dish "chicken biryani"
    python scripts/try_adapters.py --zepto-address 2 --swiggy-address 2 --zomato-address 0
    python scripts/try_adapters.py --cart

Saves what it printed to probes/normalized_<app>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters import SwiggyAdapter, ZeptoAdapter, ZomatoAdapter  # noqa: E402
from app.adapters.base import FoodAdapter  # noqa: E402
from app.config import load_config  # noqa: E402
from app.mcp_client import McpHub, describe_error  # noqa: E402
from app.schemas import CartResult, MenuItem, Product, SavedAddress  # noqa: E402

OUT = ROOT / "probes"


def save(name: str, payload: Any) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / f"normalized_{name}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def rs(v: Optional[float]) -> str:
    return "-" if v is None else f"₹{v:g}"


def show_addresses(addresses: list[SavedAddress], chosen: int) -> Optional[SavedAddress]:
    for i, a in enumerate(addresses):
        mark = "->" if i == chosen else "  "
        coords = f" ({a.lat:.4f}, {a.lng:.4f})" if a.lat is not None and a.lng is not None else ""
        print(f"  {mark} [{i}] {a.label or '-'}: {a.text[:70]}{coords}")
    if not addresses:
        print("  no saved addresses")
        return None
    return addresses[min(chosen, len(addresses) - 1)]


def ask(question: str) -> bool:
    return input(f"  {question} [y/N] ").strip().lower() in ("y", "yes")


def show_cart(cart: CartResult) -> None:
    print(f"  cart ok={cart.ok}  item_total={rs(cart.item_total)}  total={rs(cart.total)}")
    for ln in cart.lines:
        print(f"    {ln.quantity} x {ln.name}  {rs(ln.price)}")
    if cart.charges:
        print("    charges: " + ", ".join(f"{k}={rs(v)}" for k, v in cart.charges.items()))
    if cart.cart_url:
        print(f"    link: {cart.cart_url}")
    if cart.message:
        print(f"    message: {cart.message[:200]}")


# ---------------------------------------------------------------- grocery

async def try_zepto(hub: McpHub, a: argparse.Namespace) -> None:
    print("\n=== Zepto ===")
    z = ZeptoAdapter(hub)
    address = show_addresses(await z.list_addresses(), a.zepto_address)
    if not address:
        return
    await z.set_address(address.address_id)
    print(f"  store: {z.store_id}")

    products = await z.search(a.grocery)
    detailed: list[Product] = []
    for p in products:
        if p.in_stock and len(detailed) < a.details:
            detailed.append(await z.details(p))

    print(f"\n  '{a.grocery}': {len(products)} results, details for {len(detailed)}")
    for p in detailed:
        n = p.nutrition
        nut = f"kcal={n.kcal} sugar={n.sugar_g} protein={n.protein_g}" if n else "no nutrition"
        size = f"{p.pack_size:g} {p.pack_unit}" if p.pack_size else "?"
        print(f"   - {p.name[:55]:55} {rs(p.price):>6} {size:>8}  per100={rs(p.price_per_100)}  {nut}")
    for p in products[len(detailed):]:
        print(f"     {p.name[:55]:55} {rs(p.price):>6} {'in stock' if p.in_stock else 'OUT'}")

    cart: Optional[CartResult] = None
    if a.cart:
        candidates = [p for p in products if p.in_stock]
        if candidates:
            cheapest = min(candidates, key=lambda p: p.price)
            if ask(f"Add 1 x {cheapest.name} ({rs(cheapest.price)}) to your Zepto cart?"):
                cart = await z.add_to_cart(cheapest, 1)
                show_cart(cart)
    else:
        show_cart(await z.get_cart())

    save("zepto", {
        "address": address.model_dump(mode="json"),
        "store_id": z.store_id,
        "products": [p.model_dump(mode="json") for p in products],
        "detailed": [p.model_dump(mode="json") for p in detailed],
        "cart": cart.model_dump(mode="json") if cart else None,
        "cart_raw": cart.raw if cart else None,
    })


# ---------------------------------------------------------------- food

async def try_food(name: str, adapter: FoodAdapter, a: argparse.Namespace, address_index: int) -> None:
    print(f"\n=== {name} ===")
    address = show_addresses(await adapter.list_addresses(), address_index)
    if not address:
        return

    found = await adapter.search(address.address_id, a.dish)
    print(f"\n  '{a.dish}': {len(found.restaurants)} restaurants, {len(found.dishes)} dishes")
    for r in found.restaurants[:6]:
        ad = " (ad)" if r.is_ad else ""
        print(f"   - {r.name[:40]:40}{ad:5} ★{r.rating}  {r.eta_min} min  {r.distance_km} km  "
              f"{'open' if r.is_open else 'CLOSED'}  {r.offer or ''}")
    for d in found.dishes[:6]:
        print(f"     dish: {d.name[:45]:45} {rs(d.unit_price):>7}  @ {d.restaurant_name}")

    menu: list[MenuItem] = []
    open_restaurants = [r for r in found.restaurants if r.is_open]
    if open_restaurants:
        r = open_restaurants[0]
        menu = await adapter.get_menu(address.address_id, r.restaurant_id)
        print(f"\n  menu of {r.name}: {len(menu)} items")
        for m in menu[:8]:
            extra = f"  {m.kcal:g} kcal" if m.kcal else ""
            flags = ("V " if m.is_veg else "NV") + (" +variants" if m.has_variants else "")
            print(f"     {m.name[:45]:45} {rs(m.unit_price):>7}  {flags}  [{m.category}]{extra}")

    cart: Optional[CartResult] = None
    if a.cart:
        candidates = [d for d in found.dishes if d.in_stock and not d.has_variants]
        if candidates:
            cheapest = min(candidates, key=lambda d: d.unit_price)
            if ask(f"Add 1 x {cheapest.name} ({rs(cheapest.unit_price)}) from "
                   f"{cheapest.restaurant_name} to your {name} cart?"):
                cart = await adapter.add_to_cart(address.address_id, cheapest.restaurant_id, [(cheapest, 1)])
                show_cart(cart)

    save(name.lower(), {
        "address": address.model_dump(mode="json"),
        "restaurants": [r.model_dump(mode="json") for r in found.restaurants],
        "dishes": [d.model_dump(mode="json") for d in found.dishes],
        "menu": [m.model_dump(mode="json") for m in menu],
        "cart": cart.model_dump(mode="json") if cart else None,
        "cart_raw": cart.raw if cart else None,
    })


# ---------------------------------------------------------------- main

async def main(a: argparse.Namespace) -> None:
    cfg = load_config()
    async with McpHub(cfg) as hub:
        errors = await hub.connect_all(["zepto", "swiggy_food", "zomato"])
        for server, err in errors.items():
            print(f"[{server}] CONNECT FAILED: {err}")

        steps = [
            ("zepto", lambda: try_zepto(hub, a)),
            ("swiggy_food", lambda: try_food("Swiggy", SwiggyAdapter(hub), a, a.swiggy_address)),
            ("zomato", lambda: try_food("Zomato", ZomatoAdapter(hub), a, a.zomato_address)),
        ]
        for server, run in steps:
            if server in errors:
                continue
            try:
                await run()
            except Exception as e:  # noqa: BLE001  keep going with the other apps
                print(f"  FAILED: {describe_error(e)}")

    print(f"\nSaved normalized output to {OUT}\\normalized_*.json")
    if a.cart:
        print("Now open each app and check whether the item shows in its cart.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--grocery", default="guilt free ice cream")
    p.add_argument("--dish", default="biryani")
    p.add_argument("--details", type=int, default=4, help="how many Zepto products to fetch full details for")
    p.add_argument("--zepto-address", type=int, default=0)
    p.add_argument("--swiggy-address", type=int, default=0)
    p.add_argument("--zomato-address", type=int, default=0)
    p.add_argument("--cart", action="store_true", help="offer to add one cheap item per app to its cart")
    asyncio.run(main(p.parse_args()))