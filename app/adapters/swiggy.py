"""Swiggy Food via its official MCP server.

Facts from the probes:
- prices are in rupees
- addresses have no coordinates (Swiggy never exposes them)
- search_restaurants marks sponsored results with ' (Ad)' in the name
- get_restaurant_menu is paginated by category (page, hasMore)
- update_food_cart only takes items without variants unless variant ids are passed
"""
from __future__ import annotations

from typing import Any, Optional

from app.adapters.base import (
    FoodAdapter,
    clean_ad_name,
    extract_payment_bits,
    find_pincode,
    first_value,
    in_stock_flag,
    payment_state,
    strip_name_prefix,
    to_float,
)
from app.approval import OrderApproval
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

SERVER = "swiggy_food"


def _first_key(d: dict[str, Any], keys: list[str]) -> Any:
    """First non-empty value among `keys`, checked in the order given."""
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None
MAX_MENU_PAGES = 10
MAX_ADDRESS_PAGES = 5
SKIP_CATEGORIES = {"recommended"}


class SwiggyAdapter(FoodAdapter):
    platform = "swiggy"

    # ---- addresses

    async def list_addresses(self) -> list[SavedAddress]:
        out: list[SavedAddress] = []
        for page in range(1, MAX_ADDRESS_PAGES + 1):
            data = await self.hub.call(SERVER, "get_addresses", {"page": page})
            for a in (data or {}).get("addresses", []):
                text = strip_name_prefix(a.get("addressLine") or "")
                out.append(SavedAddress(
                    platform="swiggy",
                    address_id=str(a["id"]),
                    label=a.get("addressTag") or a.get("addressCategory"),
                    text=text,
                    pincode=find_pincode(text),
                ))
            if not ((data or {}).get("pagination") or {}).get("hasMore"):
                break
        return out

    # ---- search

    def _restaurant(self, r: dict[str, Any]) -> RestaurantListing:
        name, is_ad = clean_ad_name(r.get("name") or "")
        return RestaurantListing(
            platform="swiggy",
            restaurant_id=str(r.get("id")),
            name=name,
            rating=to_float(r.get("avgRating")),
            eta_min=to_float(r.get("deliveryTimeMinutes")),
            distance_km=to_float(r.get("distanceKm")),
            area=r.get("areaName"),
            cuisines=list(r.get("cuisines") or []),
            is_open=(r.get("availabilityStatus") or "OPEN").upper() == "OPEN",
            is_ad=is_ad,
            raw=r,
        )

    def _dish(self, d: dict[str, Any]) -> MenuItem:
        name, _ = clean_ad_name(d.get("restaurant_name") or "")
        return MenuItem(
            platform="swiggy",
            restaurant_id=str(d.get("restaurant_id")),
            restaurant_name=name or None,
            item_id=str(d.get("menu_item_id") or d.get("id")),
            name=d.get("name") or "",
            price=to_float(d.get("price")) or 0.0,
            in_stock=in_stock_flag(d.get("inStock")),
            has_variants=bool(d.get("hasVariants") or d.get("variations") or d.get("variantsV2")),
            has_addons=bool(d.get("hasAddons")),
            rating=to_float(d.get("rating")),
            is_veg=d.get("isVeg"),
            raw=d,
        )

    async def search(self, address_id: str, query: str) -> FoodSearch:
        restaurants = await self.hub.call(SERVER, "search_restaurants", {"addressId": address_id, "query": query})
        dishes = await self.hub.call(SERVER, "search_menu", {"addressId": address_id, "query": query})
        return FoodSearch(
            platform="swiggy",
            restaurants=[self._restaurant(r) for r in (restaurants or {}).get("restaurants", []) if r.get("id")],
            dishes=[self._dish(d) for d in (dishes or {}).get("items", []) if d.get("menu_item_id")],
        )

    # ---- menu

    async def get_menu(self, address_id: str, restaurant_id: str) -> list[MenuItem]:
        by_id: dict[str, MenuItem] = {}
        restaurant_name: Optional[str] = None

        def add(item: dict[str, Any], category: str) -> None:
            item_id = str(item.get("id"))
            existing = by_id.get(item_id)
            if existing:
                # first seen under 'Recommended'? prefer the real category
                if (existing.category or "").lower() in SKIP_CATEGORIES and category.lower() not in SKIP_CATEGORIES:
                    by_id[item_id] = existing.model_copy(update={"category": category})
                return
            by_id[item_id] = MenuItem(
                platform="swiggy",
                restaurant_id=str(restaurant_id),
                restaurant_name=restaurant_name,
                item_id=item_id,
                name=item.get("name") or "",
                price=to_float(item.get("price")) or 0.0,
                is_veg=item.get("isVeg"),
                in_stock=in_stock_flag(item.get("inStock")),
                has_variants=bool(item.get("hasVariants")),
                has_addons=bool(item.get("hasAddons")),
                rating=to_float(item.get("rating")),
                description=item.get("description"),
                category=category,
                raw=item,
            )

        for page in range(1, MAX_MENU_PAGES + 1):
            data = await self.hub.call(SERVER, "get_restaurant_menu",
                                       {"addressId": address_id, "restaurantId": str(restaurant_id), "page": page})
            data = data or {}
            restaurant_name = restaurant_name or (data.get("restaurant") or {}).get("name")
            for cat in data.get("categories", []):
                title = cat.get("title") or ""
                for item in cat.get("items") or []:
                    add(item, title)
                for sub in cat.get("subcategories") or []:
                    for item in sub.get("items") or []:
                        add(item, sub.get("title") or title)
            if not data.get("hasMore"):
                break

        return [m.model_copy(update={"restaurant_name": restaurant_name}) for m in by_id.values()]

    # ---- cart

    async def add_to_cart(self, address_id: str, restaurant_id: str,
                          items: list[tuple[MenuItem, int]]) -> CartResult:
        needs_variant = [m.name for m, _ in items if m.has_variants]
        if needs_variant:
            raise ValueError(f"Swiggy: these need a size/variant choice first: {', '.join(needs_variant)}")
        if any(m.restaurant_id != str(restaurant_id) for m, _ in items):
            raise ValueError("Swiggy: all items must come from the same restaurant")

        await self.hub.call(SERVER, "update_food_cart", {
            "restaurantId": str(restaurant_id),
            "addressId": address_id,
            "cartItems": [{"menu_item_id": m.item_id, "quantity": qty} for m, qty in items],
        })
        return await self.get_cart(address_id)

    async def get_cart(self, address_id: str) -> CartResult:
        data = await self.hub.call(SERVER, "get_food_cart", {"addressId": address_id})
        data = data if isinstance(data, dict) else {"value": data}
        body = data.get("data") if "data" in data else data

        # Real shape (from your test order):
        # data.items[]: {name, quantity, final_price: 209, subtotal: 209, total: 221.54 (item + its tax)}
        # data.pricing: {item_total: 209, delivery_charge: 83, taxes_and_charges: 56.54, to_pay: 349}
        # data.offers:  {coupon_applied, coupon_discount, free_delivery_applied}
        body = body if isinstance(body, dict) else {}
        lines: list[CartLine] = []
        for it in body.get("items") or []:
            if not isinstance(it, dict):
                continue
            qty = int(to_float(it.get("quantity")) or 1)
            line = _first_key(it, ["final_price", "subtotal", "total"])     # priority order, not dict order
            lines.append(CartLine(name=str(it.get("name") or "item"), quantity=qty, price=to_float(line)))

        pricing = body.get("pricing") or {}
        offers = body.get("offers") or {}
        charges: dict[str, float] = {}
        for key, name in (("delivery_charge", "delivery"), ("packing_charge", "packaging"),
                          ("packaging_charge", "packaging"), ("platform_fee", "platform"),
                          ("taxes_and_charges", "taxes_and_charges")):
            v = to_float(pricing.get(key))
            if v:
                charges[name] = round(charges.get(name, 0.0) + v, 2)
        coupon = to_float(offers.get("coupon_discount"))
        if coupon:                                  # 0 means 'suggested', not applied (Swiggy docs)
            charges["coupon"] = -abs(coupon)

        return CartResult(
            platform="swiggy",
            ok=bool(lines),
            lines=lines,
            item_total=to_float(pricing.get("item_total")),
            total=to_float(pricing.get("to_pay")),
            charges=charges,
            cart_id=str(body["cart_id"]) if body.get("cart_id") is not None else None,
            message=str(data.get("statusMessage") or ""),
            raw=data,
        )

    # ---- ordering
    # Swiggy's documented UPI flow: get_payment_options -> place_food_order (PENDING_PAYMENT)
    # -> you pay -> check_payment_status -> confirm_order. confirm_order is also called when
    # the wait times out, which tells Swiggy to mark the unpaid order failed.

    async def place_order(self, address_id: str, cart: CartResult, approval: OrderApproval) -> PaymentRequest:
        fresh = await self.get_cart(address_id)
        if not fresh.lines:
            raise RuntimeError("Swiggy cart is empty; nothing to order")
        if fresh.total is None:
            raise RuntimeError("Swiggy did not return a bill total; not placing the order")
        if abs(fresh.total - approval.amount) > 1:
            raise RuntimeError(f"Swiggy bill changed from ₹{approval.amount:g} to ₹{fresh.total:g}; "
                               "confirm the new amount")

        options = await self.hub.call_order(SERVER, "get_payment_options", {"addressId": address_id})
        platforms = first_value(options, ["platforms"]) or {}
        all_methods = first_value(options, ["allMethods"]) or []
        upi_ok = (isinstance(platforms, dict) and bool(platforms.get("desktop") or platforms.get("mobile"))) \
            or "upi" in str(all_methods).lower()
        if not upi_ok:
            raise RuntimeError("Swiggy says UPI isn't available for this cart right now "
                               "(cash on delivery is never used by this agent)")

        data = await self.hub.call_order(SERVER, "place_food_order", {
            "addressId": address_id,
            "paymentMethod": "UPI",
            "generateUPIQR": True,          # laptop flow: a QR you scan with any UPI app
        }, approval)

        extra = {
            "paasId": first_value(data, ["paasId"]),
            "cartId": first_value(data, ["cartId", "cart_id"]),
            "lat": to_float(first_value(data, ["lat", "latitude"])),
            "lng": to_float(first_value(data, ["lng", "longitude"])),
            "addressId": first_value(data, ["addressId"]) or address_id,
            "status": first_value(data, ["status", "orderStatus"]),
        }
        order_id = first_value(data, ["orderId", "order_id"])
        return PaymentRequest(
            platform="swiggy",
            order_id=str(order_id) if order_id is not None else None,
            amount=to_float(first_value(data, ["amount", "totalAmount", "toPay", "to_pay", "orderTotal",
                                               "finalAmount", "payableAmount"])) or fresh.total,
            approved_amount=approval.amount,
            message=str(first_value(data, ["message", "statusMessage"]) or ""),
            extra={k: v for k, v in extra.items() if v is not None},
            raw=data if isinstance(data, dict) else {"value": data},
            **extract_payment_bits(data),
        )

    async def payment_status(self, payment: PaymentRequest) -> PaymentState:
        paas_id = payment.extra.get("paasId")
        if not paas_id:
            return "UNKNOWN"
        args = {"paasId": str(paas_id), "orderId": payment.order_id,
                "addressId": payment.extra.get("addressId"), "cartId": payment.extra.get("cartId"),
                "lat": payment.extra.get("lat"), "lng": payment.extra.get("lng")}
        data = await self.hub.call_order(SERVER, "check_payment_status",
                                         {k: v for k, v in args.items() if v is not None})
        return payment_state(first_value(data, ["paymentStatus", "payment_status", "status", "state"]))

    async def finalize(self, payment: PaymentRequest, state: PaymentState) -> None:
        """PAID -> confirm (order becomes PLACED). Still PENDING/UNKNOWN at timeout -> confirm,
        which Swiggy documents as marking the unpaid order failed. FAILED/REFUND -> nothing."""
        if state in ("FAILED", "REFUND") or not payment.order_id:
            return
        args = {"orderId": payment.order_id, "addressId": payment.extra.get("addressId"),
                "cartId": payment.extra.get("cartId"),
                "lat": payment.extra.get("lat"), "lng": payment.extra.get("lng")}
        await self.hub.call_order(SERVER, "confirm_order", {k: v for k, v in args.items() if v is not None})