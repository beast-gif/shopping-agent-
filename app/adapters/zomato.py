"""Zomato via its official MCP server.

Facts from the probes:
- prices are in rupees; search returns both `price` and a lower `min_price` (the offer price)
- addresses have no coordinates, only text
- res_id is an integer; the cart needs each item's variant_id ('v_...')
- get_menu_items_listing returns every item name + variant_id, but full details
  (price, veg, nutrition) only for the first ~10 ('partial_menu'); the rest come from
  get_restaurant_menu_by_categories
- there is no 'read cart' tool: create_cart returns the cart (with fees and a
  shareable link), so that response is the cart
- create_cart requires payment_type; we pass 'upi'. Creating a cart is not an order:
  checkout_cart stays blocked in config.yaml.
"""
from __future__ import annotations

import re
from typing import Any, Iterator, Optional

from app.adapters.base import (
    FoodAdapter,
    eta_minutes,
    find_pincode,
    first_value,
    in_stock_flag,
    to_float,
)
from app.approval import OrderApproval
from app.mcp_client import McpHub
from app.schemas import (
    CartLine,
    CartResult,
    FoodSearch,
    MenuItem,
    PaymentRequest,
    PaymentState,
    RestaurantListing,
    SavedAddress,
)

SERVER = "zomato"
CATEGORY_CHUNK = 6
SKIP_CATEGORIES = {"recommended for you", "your orders and collections"}
# promo shelves like 'Items up to 60% off', 'Items starting at ₹99', 'Pair with your non-veg dishes'
PROMO_CATEGORY = re.compile(r"(% off|starting at|^items |^pair with|newly launched|^recommended)", re.IGNORECASE)


def _iter_item_dicts(obj: Any) -> Iterator[dict[str, Any]]:
    """Every dict anywhere in `obj` that looks like a menu item (has variant_id + name + price)."""
    if isinstance(obj, dict):
        if obj.get("variant_id") and obj.get("name") and obj.get("price") is not None:
            yield obj
            return                      # don't descend into its own variants/add-ons
        for v in obj.values():
            yield from _iter_item_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_item_dicts(v)


def _health(item: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    kcal = protein = None
    for h in ((item.get("health_info") or {}).get("item_health_info") or []):
        title = str(h.get("title", "")).lower()
        if title in ("calories", "energy"):
            kcal = to_float(h.get("value"))
        elif title == "protein":
            protein = to_float(h.get("value"))
    return kcal, protein


def _pick_category(categories: list[str], sub_categories: list[str]) -> Optional[str]:
    """Prefer a real menu section ('Non-Veg Dum Biryani') over a promo shelf ('Items up to 60% off')."""
    candidates = [c for c in list(categories or []) + list(sub_categories or []) if c]
    for c in candidates:
        if c.lower() not in SKIP_CATEGORIES and not PROMO_CATEGORY.search(c):
            return c
    for c in candidates:
        if c.lower() not in SKIP_CATEGORIES:
            return c
    return candidates[0] if candidates else None


class ZomatoAdapter(FoodAdapter):
    platform = "zomato"

    def __init__(self, hub: McpHub):
        super().__init__(hub)
        self._last_cart: Optional[CartResult] = None

    # ---- addresses

    async def list_addresses(self) -> list[SavedAddress]:
        data = await self.hub.call(SERVER, "get_saved_addresses_for_user", {})
        addresses = first_value(data, ["addresses"]) or []
        return [
            SavedAddress(
                platform="zomato",
                address_id=str(a["address_id"]),
                text=a.get("location_name") or "",
                pincode=find_pincode(a.get("location_name")),
            )
            for a in addresses if isinstance(a, dict) and a.get("address_id")
        ]

    # ---- search

    def _menu_item(self, it: dict[str, Any], restaurant_id: str, restaurant_name: Optional[str],
                   has_more_variants: bool = False) -> MenuItem:
        price = to_float(it.get("price")) or 0.0
        offer = to_float(it.get("discounted_price"))
        if offer is None:
            offer = to_float(it.get("min_price"))
        kcal, protein = _health(it)
        tags = [str(t).lower() for t in it.get("item_tags") or []]
        is_veg = it.get("is_veg")
        if is_veg is None and tags:
            is_veg = "veg" in tags and "non-veg" not in tags
        variants = it.get("variants")
        return MenuItem(
            platform="zomato",
            restaurant_id=str(restaurant_id),
            restaurant_name=restaurant_name,
            item_id=str(it["variant_id"]),
            name=it.get("name") or it.get("item_name") or "",
            price=price,
            discounted_price=offer if offer is not None and offer < price else None,
            is_veg=is_veg,
            in_stock=in_stock_flag(it.get("in_stock")),
            has_variants=has_more_variants or (isinstance(variants, list) and len(variants) > 1),
            has_addons=bool(it.get("available_add_ons") or it.get("item_customizations")),
            description=it.get("description"),
            category=_pick_category(it.get("categories") or [], it.get("sub_categories") or []),
            kcal=kcal,
            protein_g=protein,
            raw=it,
        )

    async def search(self, address_id: str, query: str) -> FoodSearch:
        data = await self.hub.call(SERVER, "get_restaurants_for_keyword",
                                   {"address_id": address_id, "keyword": query})
        restaurants: list[RestaurantListing] = []
        dishes: list[MenuItem] = []
        for r in (data or {}).get("results", []):
            res_id = str(r.get("res_id"))
            name = r.get("name") or ""
            restaurants.append(RestaurantListing(
                platform="zomato",
                restaurant_id=res_id,
                name=name,
                rating=to_float(r.get("rating")),
                eta_min=eta_minutes(r.get("eta")),
                distance_km=to_float(r.get("distance")),
                offer=r.get("res_offer"),
                is_open=str(r.get("serviceability_status", "serviceable")).lower() == "serviceable",
                raw={k: v for k, v in r.items() if k != "items"},
            ))
            for it in r.get("items") or []:
                if it.get("variant_id"):
                    dishes.append(self._menu_item(it, res_id, name))
        return FoodSearch(platform="zomato", restaurants=restaurants, dishes=dishes)

    # ---- menu

    async def get_menu(self, address_id: str, restaurant_id: str) -> list[MenuItem]:
        res_id = int(restaurant_id)
        data = await self.hub.call(SERVER, "get_menu_items_listing", {"res_id": res_id, "address_id": address_id})
        body = first_value(data, ["data"]) or data or {}
        restaurant_name = body.get("restaurant_name")
        mappings = {m["variant_id"]: m for m in body.get("item_mappings") or [] if m.get("variant_id")}

        items: dict[str, MenuItem] = {}
        for it in _iter_item_dicts(body.get("partial_menu") or []):
            more = bool((mappings.get(it["variant_id"]) or {}).get("has_more_variants"))
            items[it["variant_id"]] = self._menu_item(it, restaurant_id, restaurant_name, more)

        # fetch the rest, a few categories at a time
        missing = [m for vid, m in mappings.items() if vid not in items]
        categories: list[str] = []
        for m in missing:
            for c in m.get("categories") or []:
                if c not in categories and c.lower() not in SKIP_CATEGORIES:
                    categories.append(c)

        for i in range(0, len(categories), CATEGORY_CHUNK):
            if all(vid in items for vid in mappings):
                break
            chunk = categories[i:i + CATEGORY_CHUNK]
            extra = await self.hub.call(SERVER, "get_restaurant_menu_by_categories",
                                        {"res_id": res_id, "categories": chunk, "address_id": address_id})
            for it in _iter_item_dicts(extra):
                vid = it["variant_id"]
                if vid not in items:
                    more = bool((mappings.get(vid) or {}).get("has_more_variants"))
                    items[vid] = self._menu_item(it, restaurant_id, restaurant_name, more)

        return list(items.values())

    # ---- cart

    def _parse_cart(self, data: Any) -> CartResult:
        cart = first_value(data, ["cart"]) if isinstance(data, dict) else None
        if not isinstance(cart, dict):
            msg = (data or {}).get("message", "") if isinstance(data, dict) else str(data)
            return CartResult(platform="zomato", ok=False, message=msg or "No cart returned",
                              raw=data if isinstance(data, dict) else {"value": data})

        lines = []
        for entry in cart.get("items") or []:
            item = entry.get("item") or {}
            qty = int(to_float(entry.get("quantity")) or 1)
            unit = to_float(item.get("discounted_price"))
            if unit is None:
                unit = to_float(item.get("price"))
            lines.append(CartLine(name=item.get("name") or "item", quantity=qty,
                                  price=round(unit * qty, 2) if unit is not None else None))

        charges: dict[str, float] = {}
        breakdown = cart.get("charge_breakdown") or {}
        for c in breakdown.get("base_charges") or []:
            amount = (to_float(c.get("amount")) or 0.0) - (to_float(c.get("discount")) or 0.0)
            key = str(c.get("charge_type") or "charge").lower()
            charges[key] = round(charges.get(key, 0.0) + amount, 2)
        tax_total = sum(to_float(t.get("tax_amount")) or 0.0 for t in breakdown.get("taxes") or [])
        if tax_total:
            charges["taxes"] = round(tax_total, 2)
        promo = to_float(cart.get("promo_discount_amount"))
        if promo:
            charges["promo_discount"] = -abs(promo)

        return CartResult(
            platform="zomato",
            ok=bool(lines),
            lines=lines,
            item_total=to_float(cart.get("item_total")),
            total=to_float(cart.get("final_amount")),
            charges=charges,
            cart_id=cart.get("cart_id"),
            cart_url=cart.get("shareable_link"),
            message=(data or {}).get("message", "") if isinstance(data, dict) else "",
            raw=data,
        )

    async def add_to_cart(self, address_id: str, restaurant_id: str,
                          items: list[tuple[MenuItem, int]]) -> CartResult:
        needs_variant = [m.name for m, _ in items if m.has_variants]
        if needs_variant:
            raise ValueError(f"Zomato: these need a size/variant choice first: {', '.join(needs_variant)}")
        if any(m.restaurant_id != str(restaurant_id) for m, _ in items):
            raise ValueError("Zomato: all items must come from the same restaurant")

        data = await self.hub.call(SERVER, "create_cart", {
            "res_id": int(restaurant_id),
            "items": [{"variant_id": m.item_id, "quantity": qty} for m, qty in items],
            "address_id": address_id,
            "payment_type": "upi",
        })
        self._last_cart = self._parse_cart(data)
        return self._last_cart

    async def get_cart(self, address_id: str) -> CartResult:
        """Zomato has no read-cart tool; this is the cart from the last add_to_cart in this run."""
        if self._last_cart is None:
            return CartResult(platform="zomato", ok=False, message="No cart created in this session")
        return self._last_cart

    # ---- ordering
    # checkout_cart(upi) returns the order id, a QR image (base64) and a upi:// link.
    # Paid / placed status comes from get_order_tracking_info.

    async def place_order(self, address_id: str, cart: CartResult, approval: OrderApproval) -> PaymentRequest:
        if not cart.cart_id:
            raise RuntimeError("No Zomato cart id; build the cart first")
        if cart.total is None:
            raise RuntimeError("Zomato did not return a bill total; not placing the order")
        if abs(cart.total - approval.amount) > 1:
            raise RuntimeError(f"Zomato bill is ₹{cart.total:g} but ₹{approval.amount:g} was confirmed; "
                               "confirm the right amount")

        data = await self.hub.call_order(SERVER, "checkout_cart",
                                         {"cart_id": cart.cart_id, "payment_method_type": "upi"}, approval)
        success = first_value(data, ["success"])
        order_id = first_value(data, ["order_id"])
        if success is False or not order_id:
            raise RuntimeError(f"Zomato checkout failed: {first_value(data, ['message']) or data}")

        qr = first_value(data, ["qr_code"])
        return PaymentRequest(
            platform="zomato",
            order_id=str(order_id),
            amount=to_float(first_value(data, ["final_payment_amount"])) or cart.total,
            approved_amount=approval.amount,
            upi_link=first_value(data, ["upi_link"]),
            qr_png_base64=qr.split(",", 1)[-1] if isinstance(qr, str) else None,
            message=str(first_value(data, ["message"]) or ""),
            raw=data if isinstance(data, dict) else {"value": data},
        )

    async def payment_status(self, payment: PaymentRequest) -> PaymentState:
        if not payment.order_id:
            return "UNKNOWN"
        data = await self.hub.call_order(SERVER, "get_order_tracking_info", {"order_id": payment.order_id})
        items = first_value(data, ["order_tracking_items"]) or []
        for it in items if isinstance(items, list) else []:
            if str(it.get("order_id")) != str(payment.order_id):
                continue
            status = str(it.get("order_status") or "").lower()
            if it.get("is_order_paid"):          # is_order_placed alone can be true while payment is pending
                return "PAID"
            if any(w in status for w in ("fail", "cancel", "reject", "expire")):
                return "FAILED"
            return "PENDING"
        return "UNKNOWN"