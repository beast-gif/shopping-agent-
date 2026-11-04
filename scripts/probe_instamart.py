"""Probe Swiggy Instamart's MCP server (read-only, nothing is ordered).

First run opens a browser to log in to Swiggy for Instamart (its own token, .tokens/instamart.json).

Checks what Swiggy's docs leave open:
  1. the tool list                                   -> probes/instamart_tools.json
  2. are Instamart's address ids the same as Swiggy Food's (places.yaml 'swiggy')?
                                                     -> probes/instamart_addresses.json
  3. one search: are prices rupees? what do sizes look like?   -> probes/instamart_search.json
  4. your cart as Instamart reports it right now     -> probes/instamart_cart_before.json
  5. with --cart-test (ONLY if your Instamart cart in the Swiggy app is empty): puts 1 cheap
     item in the agent's cart, shows the real bill, asks whether your Swiggy app now shows it
     (= the same cart), then empties it again          -> probes/instamart_cart_test.json

Run from the project root:
    python scripts/probe_instamart.py
    python scripts/probe_instamart.py --query "curd" --place clg
    python scripts/probe_instamart.py --cart-test        # empty your app's Instamart cart first
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.debug import save_debug  # noqa: E402
from app.mcp_client import McpHub, describe_error  # noqa: E402
from app.places import load_places, match_place_name  # noqa: E402

SERVER = "instamart"


async def main(a: argparse.Namespace) -> None:
    cfg = load_config()
    book = load_places(cfg)
    place = match_place_name(book, a.place) if a.place else (book.get("home") or (book.places[0] if book.places else None))
    async with McpHub(cfg) as hub:
        errors = await hub.connect_all([SERVER])
        if errors:
            print(f"Couldn't connect to Instamart: {errors[SERVER]}")
            return
        tools = hub.all_tools(SERVER)
        save_debug("instamart_tools.json", [{"name": t.name, "description": (t.description or "")[:1200],
                                              "input_schema": t.input_schema} for t in tools])
        print(f"{len(tools)} tools:", ", ".join(t.name for t in tools))
        print("Agent may call:", ", ".join(t.name for t in hub.tools(SERVER)))

        try:
            addresses = await hub.call(SERVER, "get_addresses", {})
        except Exception as e:  # noqa: BLE001
            print(f"get_addresses failed: {describe_error(e)}")
            return
        save_debug("instamart_addresses.json", addresses)
        ids = [str(x.get("id")) for x in (addresses or {}).get("addresses", [])] if isinstance(addresses, dict) else []
        swiggy_ids = {p.name: p.addresses.get("swiggy") for p in book.places}
        print(f"\nInstamart addresses: {len(ids)}")
        for name, sid in swiggy_ids.items():
            if sid:
                print(f"  {name:10} Swiggy Food id {sid}: {'same id on Instamart ✓' if sid in ids else 'NOT in Instamart list ✗'}")

        address = (place.addresses.get("instamart") or place.addresses.get("swiggy")) if place else None
        if not address:
            print("No Swiggy address for this place in places.yaml; skipping the search.")
            return
        try:
            found = await hub.call(SERVER, "search_products", {"query": a.query, "addressId": address})
        except Exception as e:  # noqa: BLE001
            print(f"search_products failed: {describe_error(e)}")
            return
        save_debug("instamart_search.json", found)
        products = (found or {}).get("products", []) if isinstance(found, dict) else []
        print(f"\nSearch '{a.query}' at {place.name}: {len(products)} products")
        for p in products[:6]:
            for v in (p.get("variations") or [])[:2]:
                price = v.get("price") or {}
                print(f"  {str(v.get('displayName') or p.get('displayName'))[:50]:<50} "
                      f"{str(v.get('quantityDescription')):<10} offer={price.get('offerPrice')} mrp={price.get('mrp')} "
                      f"stock={v.get('isInStockAndAvailable')}")

        try:
            cart = await hub.call(SERVER, "get_cart", {})
            save_debug("instamart_cart_before.json", cart)
            items = (cart or {}).get("items", []) if isinstance(cart, dict) else []
            print(f"\nInstamart cart right now: {len(items)} item(s)"
                  + "".join(f"\n  {it.get('quantity')} x {it.get('itemName')}" for it in items))
        except Exception as e:  # noqa: BLE001
            print(f"\nget_cart before any update: {describe_error(e)}"
                  "\n  (likely means the agent's session has no cart yet; --cart-test checks properly)")

        if not a.cart_test:
            print("\nNext: empty your Instamart cart in the Swiggy app, then run with --cart-test.")
        else:
            answer = (await asyncio.to_thread(input, "\nIs your Instamart cart in the Swiggy app EMPTY right now? (y/N) ")).strip().lower()
            if answer != "y":
                print("Skipped the cart test (it would replace your app cart if they're the same cart).")
            else:
                pick = None
                for p in products:
                    for v in p.get("variations") or []:
                        if v.get("isInStockAndAvailable") and v.get("spinId") and v.get("skuId"):
                            if pick is None or (v.get("price") or {}).get("offerPrice", 1e9) < (pick.get("price") or {}).get("offerPrice", 1e9):
                                pick = v
                if not pick:
                    print("No in-stock product to test with.")
                else:
                    item = {"spinId": str(pick["spinId"]), "skuId": str(pick["skuId"]), "quantity": 1}
                    print(f"Adding 1 x {pick.get('displayName')} ({pick.get('quantityDescription')}) to the agent's cart...")
                    try:
                        updated = await hub.call(SERVER, "update_cart", {"items": [item], "selectedAddressId": address})
                        save_debug("instamart_cart_test.json", updated)
                        bill = (updated or {}).get("billBreakdown") if isinstance(updated, dict) else None
                        if bill:
                            print("  bill lines:", [(li.get("label"), li.get("value")) for li in bill.get("lineItems") or []],
                                  "| to pay:", (bill.get("toPay") or {}).get("value"))
                        try:
                            again = await hub.call(SERVER, "get_cart", {})
                            print(f"  get_cart now works: {len((again or {}).get('items', []))} item(s)")
                        except Exception as e:  # noqa: BLE001
                            print(f"  get_cart after update: {describe_error(e)}")
                        seen = (await asyncio.to_thread(
                            input, "  Open the Swiggy app -> Instamart -> cart. Do you see that item? (y/n) ")).strip().lower()
                        print("  -> SAME cart as your app" if seen == "y" else "  -> separate agent cart (not in your app)")
                    except Exception as e:  # noqa: BLE001
                        print(f"  update_cart failed: {describe_error(e)}")
                    finally:
                        try:
                            await hub.call(SERVER, "clear_cart", {})
                            print("  Cleared the test item again.")
                        except Exception as e:  # noqa: BLE001
                            print(f"  clear_cart failed ({describe_error(e)}); remove the item in the app.")

    print("\nSend me the output above (and probes/instamart_cart_test.json after --cart-test).")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--query", default="ice cream")
    p.add_argument("--place", help="a place from places.yaml (default: home)")
    p.add_argument("--cart-test", action="store_true",
                   help="add 1 cheap item, check whether your Swiggy app shows it, then clear it")
    asyncio.run(main(p.parse_args()))