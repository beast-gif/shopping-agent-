"""END-TO-END TEST OF A REAL ORDER on one app (option 1: UPI after you type 'confirm <total>').

If you pay the QR, the order is real and will be delivered. If you don't pay, the script
waits payment_timeout_seconds and then closes the order out (Swiggy marks it failed).

Run from the project root:
    python scripts/try_order.py swiggy --query biryani
    python scripts/try_order.py zomato --query biryani --address 3
    python scripts/try_order.py zepto  --query "guilt free ice cream" --address 0

Saves every raw response to probes/order_<app>.json so the parsing can be checked.
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
from app.approval import ApprovalError, approve_order, confirm_phrase  # noqa: E402
from app.config import load_config  # noqa: E402
from app.mcp_client import McpHub, describe_error  # noqa: E402
from app.payment import show_payment, wait_for_payment  # noqa: E402
from app.schemas import CartResult  # noqa: E402

SERVERS = {"zepto": "zepto", "swiggy": "swiggy_food", "zomato": "zomato"}
LOG: dict[str, Any] = {}


def rs(v: Optional[float]) -> str:
    return "-" if v is None else f"₹{v:g}"


def pick(prompt: str, count: int) -> Optional[int]:
    raw = input(f"{prompt} [0-{count - 1}, Enter to stop]: ").strip()
    if not raw.isdigit() or int(raw) >= count:
        return None
    return int(raw)


def show_bill(cart: CartResult) -> None:
    print("\n  Bill")
    for ln in cart.lines:
        print(f"    {ln.quantity} x {ln.name[:50]:50} {rs(ln.price)}")
    print(f"    items: {rs(cart.item_total)}")
    for name, amount in cart.charges.items():
        print(f"    {name}: {rs(amount)}")
    print(f"    TO PAY: {rs(cart.total)}")


async def build_cart(app: str, hub: McpHub, a: argparse.Namespace):
    """Returns (adapter, address_id, cart) with the item you picked in the cart."""
    if app == "zepto":
        z = ZeptoAdapter(hub)
        addresses = await z.list_addresses()
        address = addresses[min(a.address, len(addresses) - 1)]
        print(f"  Delivering to: {address.text}")
        await z.set_address(address.address_id)
        products = [p for p in await z.search(a.query) if p.in_stock][:8]
        for i, p in enumerate(products):
            print(f"   [{i}] {p.name[:55]:55} {rs(p.price)}")
        i = pick("  Which one?", len(products)) if products else None
        if i is None:
            return z, address.address_id, None
        await z.add_to_cart(products[i], a.qty, replace=True)     # drop leftovers from earlier tests
        cart = await z.preview_order()
        LOG["preview_raw"] = cart.raw
        return z, address.address_id, cart

    adapter = SwiggyAdapter(hub) if app == "swiggy" else ZomatoAdapter(hub)
    addresses = await adapter.list_addresses()
    address = addresses[min(a.address, len(addresses) - 1)]
    print(f"  Delivering to: {address.text}")
    found = await adapter.search(address.address_id, a.query)
    dishes = [d for d in found.dishes if d.in_stock and not d.has_variants][:8]
    for i, d in enumerate(dishes):
        print(f"   [{i}] {d.name[:45]:45} {rs(d.unit_price):>7}  @ {d.restaurant_name}")
    i = pick("  Which one?", len(dishes)) if dishes else None
    if i is None:
        return adapter, address.address_id, None
    dish = dishes[i]
    cart = await adapter.add_to_cart(address.address_id, dish.restaurant_id, [(dish, a.qty)])
    LOG["cart_raw"] = cart.raw
    return adapter, address.address_id, cart


async def main(a: argparse.Namespace) -> None:
    cfg = load_config()
    app = a.app
    server = SERVERS[app]
    print("=" * 70)
    print(f"REAL ORDER TEST on {app.title()}. Paying the QR places a real, delivered order.")
    print(f"Cap: ₹{cfg.ordering.max_order_total:g}.  Unpaid orders are closed after "
          f"{cfg.ordering.payment_timeout_seconds} s.")
    print("=" * 70)

    async with McpHub(cfg) as hub:
        errors = await hub.connect_all([server])
        if errors:
            print(f"Could not connect: {errors[server]}")
            return
        try:
            adapter, address_id, cart = await build_cart(app, hub, a)
            if cart is None:
                print("Stopped before adding anything.")
                return
            show_bill(cart)
            total = cart.total if cart.total is not None else cart.item_total
            if cart.total is None:
                print("  (the app didn't return a final total; approving the item total, and the order "
                      "is refused later if the app asks for more)")
            if not total:
                print("No amount to approve; stopping.")
                return

            typed = input(f"\n  Type '{confirm_phrase(total)}' to place a UPI order, anything else to stop: ")
            try:
                approval = approve_order(cfg, server, total, typed)
            except ApprovalError as e:
                print(f"  Not placed: {e}")
                return

            if app == "zepto":
                payment = await adapter.place_order(approval)
            else:
                payment = await adapter.place_order(address_id, cart, approval)
            LOG["payment"] = payment.model_dump(mode="json")
            LOG["payment_raw"] = payment.raw

            if not show_payment(payment):
                await adapter.finalize(payment, "PENDING")      # close it out unpaid right away
                state, timed_out = "PENDING", True
            else:
                print(f"\n  Waiting for payment (checking every {cfg.ordering.payment_poll_seconds} s)...")
                state, timed_out = await wait_for_payment(adapter, payment, cfg)

            LOG["final_state"] = state
            LOG["timed_out"] = timed_out
            if state == "PAID":
                print(f"\nPaid. Order {payment.order_id} is placed on {app.title()}.")
            elif timed_out:
                print(f"\nNot paid in time (last status {state}). The order was closed out; check the app's orders.")
            else:
                print(f"\nPayment {state}. Nothing was charged unless your UPI app says so.")
        except Exception as e:  # noqa: BLE001
            print(f"\nFAILED: {describe_error(e)}")
            LOG["error"] = describe_error(e)
        finally:
            out = ROOT / "probes" / f"order_{app}.json"
            out.parent.mkdir(exist_ok=True)
            out.write_text(json.dumps(LOG, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            print(f"Raw responses saved to {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("app", choices=list(SERVERS))
    p.add_argument("--query", default="biryani")
    p.add_argument("--address", type=int, default=0, help="which saved address (0 = first)")
    p.add_argument("--qty", type=int, default=1)
    asyncio.run(main(p.parse_args()))