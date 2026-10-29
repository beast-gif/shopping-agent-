"""Try the Amazon Now adapter.

Run from the project root (after scripts/amazon_login.py):
    python scripts/try_amazon.py ice cream              read-only: address, search, details of the top 3
    python scripts/try_amazon.py ice cream --cart       also: add one to your cart, show the real bill,
                                                        then offer to take it out again

Nothing is ordered. The cart is your real Amazon Now cart, so the script asks before removing
anything that was already in it.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.amazon_now import AmazonNowAdapter, AmazonNowError  # noqa: E402
from app.browser import amazon_browser, signed_in_name  # noqa: E402
from app.config import load_config  # noqa: E402
from app.debug import save_debug  # noqa: E402
from app.schemas import CartResult  # noqa: E402


def ask(prompt: str) -> asyncio.Future:
    return asyncio.to_thread(input, prompt)


def show_cart(title: str, cart: CartResult) -> None:
    print(f"\n{title}")
    for ln in cart.lines:
        price = f"₹{ln.price:g}" if ln.price is not None else "?"
        print(f"   {ln.quantity} x {ln.name[:60]:<60} {price:>8}")
    if cart.item_total is not None:
        print(f"   {'items':<64} ₹{cart.item_total:g}")
    for name, amount in cart.charges.items():
        print(f"   {name:<64} ₹{amount:g}")
    if cart.total is not None:
        print(f"   {'TOTAL':<64} ₹{cart.total:g}")
    if cart.message:
        print(f"   {cart.message}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+")
    parser.add_argument("--cart", action="store_true", help="add one product to the cart and show the bill")
    args = parser.parse_args()
    query = " ".join(args.query)
    cfg = load_config()

    async with amazon_browser(cfg) as context:
        name = await signed_in_name(context, cfg.amazon_now.base_url)
        if not name:
            print("Not signed in. Run scripts/amazon_login.py first.")
            return
        amazon = AmazonNowAdapter(context, cfg)

        address = await amazon.current_address()
        print(f"Signed in as {name}. Amazon Now delivers to: {address.label} | {address.text}")
        try:
            await amazon.set_address(address.address_id)
        except AmazonNowError as e:
            print(f"  {e}")
            return

        products = await amazon.search(query)
        save_debug("amazon_search.json", [p.raw for p in products[:5]])
        print(f"\n{len(products)} results for {query!r}:")
        for i, p in enumerate(products, 1):
            pack = f"{p.pack_size:g} {p.pack_unit}" if p.pack_size else "?"
            mrp = f"(MRP ₹{p.mrp:g})" if p.mrp and p.mrp > p.price else ""
            flags = " ".join(f for f in ["AD" if p.is_ad else "", "" if p.in_stock else "OUT"] if f)
            rating = f"★{p.rating:g}" if p.rating else ""
            print(f" {i:>2}. {p.name[:55]:<55} {pack:>9}  ₹{p.price:<6g} {mrp:<12} {rating:<5} {flags}")

        print("\nDetails of the top 3:")
        detailed = []
        for p in products[:3]:
            d = await amazon.details(p)
            detailed.append(d)
            n = d.nutrition
            facts = (f"{n.kcal} kcal, sugar {n.sugar_g} g, protein {n.protein_g} g per 100 {n.basis}"
                     if n else "no nutrition table on Amazon Now")
            print(f"   {d.name[:50]:<50} maker: {d.brand or '?':<22} {facts}")
        save_debug("amazon_details.json", [d.raw.get("details") for d in detailed])

        if not args.cart:
            return

        pick = (await ask("\nAdd which number to your Amazon Now cart? (Enter = skip) ")).strip()
        if not pick.isdigit() or not 1 <= int(pick) <= len(products):
            return
        product = products[int(pick) - 1]
        qty_text = (await ask("Quantity? (Enter = 1) ")).strip()
        quantity = int(qty_text) if qty_text.isdigit() and int(qty_text) > 0 else 1

        replace = False
        others = await amazon.other_items([product.product_id])
        if others:
            print("\nYour Amazon Now cart already has:")
            for ln in others:
                print(f"   {ln.quantity} x {ln.name}")
            replace = (await ask("Remove those first? (y/N) ")).strip().lower() == "y"

        try:
            cart = await amazon.add_to_cart(product, quantity, replace=replace)
        except AmazonNowError as e:
            print(f"  {e}")
            return
        show_cart("Cart (fees estimated from the cart):", cart)

        bill = await amazon.preview_order()
        show_cart("Bill from the cart page (what you'd pay):", bill)

        if (await ask(f"\nTake {product.name[:40]!r} out of the cart again? (Y/n) ")).strip().lower() != "n":
            cart = await amazon.remove_items([product.product_id])
            show_cart("Cart now:", cart)


if __name__ == "__main__":
    asyncio.run(main())