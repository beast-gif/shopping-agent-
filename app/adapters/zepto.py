"""Zepto via its official MCP server.

Facts from the probes:
- prices are in paisa (6900 = ₹69)
- the store (and so stock + price) is set per MCP session by select_saved_address
- nutrition is a text blob under highlights -> 'nutrition information'
- update_cart needs productVariantId + storeProductId and a deviceId
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Optional

from app.adapters.base import (
    GroceryAdapter,
    extract_payment_bits,
    find_pincode,
    first_value,
    in_stock_flag,
    paise_to_rupees,
    parse_nutrition_text,
    parse_pack_size,
    payment_state,
    to_float,
)
from app.approval import OrderApproval
from app.config import resolve
from app.mcp_client import McpHub
from app.schemas import CartLine, CartResult, PaymentRequest, PaymentState, Product, SavedAddress

_TOTAL_KEYS = ["toPay", "to_pay", "grandTotal", "grand_total", "finalAmount", "final_amount",
               "payableAmount", "totalAmount", "total_amount", "orderTotal", "totalPayable", "total"]

SERVER = "zepto"


def _device_id() -> str:
    """Stable per-laptop id, so the agent keeps using the same Zepto cart across runs."""
    path = resolve(".state/zepto_device_id")
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = f"ar7-shopping-agent-{uuid.uuid4()}"
    path.write_text(value, encoding="utf-8")
    return value


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _nutrition_text(facts: dict[str, str], data: dict[str, Any]) -> Optional[str]:
    """The label key varies by product ('nutrition information', 'nutritional facts per 100 ml', ...).
    Order: any key mentioning 'nutri', then any value mentioning kcal/energy, then the description."""
    for key, value in facts.items():
        if "nutri" in key and value:
            return value
    for value in facts.values():
        low = value.lower()
        if "kcal" in low or "energy" in low:
            return value
    description = data.get("description") or ""
    return description if ("kcal" in description.lower() or "energy" in description.lower()) else None


class ZeptoAdapter(GroceryAdapter):
    platform = "zepto"

    def __init__(self, hub: McpHub):
        super().__init__(hub)
        self.address_id: Optional[str] = None
        self.store_id: Optional[str] = None
        self.device_id = _device_id()

    # ---- addresses

    async def list_addresses(self) -> list[SavedAddress]:
        data = await self.hub.call(SERVER, "list_saved_addresses", {})
        out = []
        for a in (data or {}).get("addresses", []):
            text = a.get("addressLine") or ""
            out.append(SavedAddress(
                platform="zepto",
                address_id=str(a["id"]),
                label=a.get("label"),
                text=text,
                lat=to_float(a.get("latitude")),
                lng=to_float(a.get("longitude")),
                pincode=a.get("postalCode") or find_pincode(text),
            ))
        return out

    async def set_address(self, address_id: str) -> None:
        if address_id == self.address_id:
            return
        data = await self.hub.call(SERVER, "select_saved_address", {"addressId": address_id})
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"Zepto could not select address: {data.get('summary') or data}")
        self.address_id = address_id
        self.store_id = (data or {}).get("storeId") if isinstance(data, dict) else None

    def _require_address(self) -> None:
        if not self.address_id:
            raise RuntimeError("Zepto: call set_address() before searching (the address picks the store)")

    # ---- search

    def _product(self, p: dict[str, Any]) -> Product:
        pvid = str(p.get("productVariantId") or p.get("id"))
        name = p.get("name") or ""
        size, unit = parse_pack_size(p.get("packSize"))
        available = p.get("availableQuantity")
        return Product(
            platform="zepto",
            product_id=pvid,
            store_product_id=p.get("storeProductId"),
            name=name,
            price=paise_to_rupees(p.get("price") if p.get("price") is not None else p.get("sellingPrice")) or 0.0,
            mrp=paise_to_rupees(p.get("mrp")),
            pack_size=size,
            pack_unit=unit,
            in_stock=in_stock_flag(available) and in_stock_flag(p.get("isInStock")),
            is_ad=bool(p.get("isAd")),
            url=f"https://www.zepto.com/pn/{_slug(name)}/pvid/{pvid}" if name else None,
            image_url=p.get("imageUrl"),
            raw=p,
        )

    async def search(self, query: str) -> list[Product]:
        self._require_address()
        data = await self.hub.call(SERVER, "search_products", {"query": query})
        return [self._product(p) for p in (data or {}).get("products", []) if p.get("productVariantId") or p.get("id")]

    async def search_many(self, queries: list[str]) -> dict[str, list[Product]]:
        """One call for several queries (Zepto runs them in parallel server-side)."""
        self._require_address()
        data = await self.hub.call(SERVER, "search_multiple_products", {"queries": queries})
        out: dict[str, list[Product]] = {}
        for section in (data or {}).get("sections", []):
            out[section.get("query", "")] = [self._product(p) for p in section.get("products", [])]
        return out

    async def details(self, product: Product) -> Product:
        data = await self.hub.call(SERVER, "get_product_details", {"product_variant_id": product.product_id})
        if not isinstance(data, dict):
            return product

        facts: dict[str, str] = {}
        for group in ("highlights", "productDetails", "information", "keyHighlights"):
            for kv in data.get(group) or []:
                if isinstance(kv, dict) and kv.get("key"):
                    facts.setdefault(kv["key"].strip().lower(), str(kv.get("value", "")))

        size, unit = parse_pack_size(data.get("packSize")) if data.get("packSize") else (product.pack_size, product.pack_unit)
        if size is None and data.get("weightInGms"):
            size, unit = to_float(data["weightInGms"]), "g"
        if unit is None and (data.get("unitOfMeasure") or "").upper().startswith("MILLI"):
            unit = "ml"

        nutrition = parse_nutrition_text(_nutrition_text(facts, data), basis=unit if unit in ("g", "ml") else "g")

        return product.model_copy(update={
            "brand": data.get("brand") or facts.get("brand") or product.brand,
            "price": paise_to_rupees(data.get("sellingPrice")) or product.price,
            "mrp": paise_to_rupees(data.get("mrp")) or product.mrp,
            "pack_size": size,
            "pack_unit": unit,
            "in_stock": in_stock_flag(data.get("isInStock", product.in_stock)),
            "rating": to_float(data.get("averageRating")),
            "image_url": (data.get("images") or [product.image_url])[0],
            "nutrition": nutrition,
            "raw": {**product.raw, "details": data, "facts": facts},
        })

    # ---- cart

    def _cart_result(self, data: Any, message: str = "") -> CartResult:
        items = (data or {}).get("items", []) if isinstance(data, dict) else []
        lines = []
        for it in items:
            qty = int(to_float(it.get("quantity")) or 0)
            unit_price = paise_to_rupees(it.get("price"))
            lines.append(CartLine(
                name=it.get("name") or it.get("label") or it.get("productVariantId", "item"),
                quantity=qty,
                price=round(unit_price * qty, 2) if unit_price is not None else None,
            ))
        known = [ln.price for ln in lines if ln.price is not None]
        return CartResult(
            platform="zepto",
            ok=bool(lines),
            lines=lines,
            item_total=round(sum(known), 2) if known and len(known) == len(lines) else None,
            message=message or (data.get("summary", "") if isinstance(data, dict) else ""),
            raw=data if isinstance(data, dict) else {"value": data},
        )

    @staticmethod
    def _cart_item(product: Product, quantity: int) -> dict[str, Any]:
        if not product.store_product_id:
            raise ValueError(f"Zepto product {product.name!r} has no storeProductId; search again")
        raw = product.raw or {}
        item = {
            "productVariantId": product.product_id,
            "storeProductId": product.store_product_id,
            "quantity": quantity,
            "name": product.name,
            "price": round(product.price * 100),
            "mrp": round(product.mrp * 100) if product.mrp is not None else None,
            "packSize": raw.get("packSize"),
            "imageUrl": product.image_url,
        }
        return {k: v for k, v in item.items() if v is not None}

    async def add_many(self, picks: list[tuple[Product, int]], replace: bool = True) -> CartResult:
        """Several products in one update_cart call: [(milk, 2), (curd, 1)].
        replace=True (default) empties the agent's Zepto cart first. That cart lives in the MCP
        session only (your phone app doesn't show it), so replacing it never touches your own picks."""
        self._require_address()
        merged: dict[str, tuple[Product, int]] = {}
        for product, quantity in picks:
            prev = merged.get(product.product_id)
            merged[product.product_id] = (product, quantity + (prev[1] if prev else 0))
        await self.hub.call(SERVER, "update_cart", {
            "deviceId": self.device_id,
            "cartItems": [self._cart_item(p, q) for p, q in merged.values()],
            "replaceCart": replace,
        })
        return await self.get_cart()

    async def add_to_cart(self, product: Product, quantity: int = 1, replace: bool = False) -> CartResult:
        """replace=True empties the agent's Zepto cart first, so only this product is in it."""
        self._require_address()
        await self.hub.call(SERVER, "update_cart", {
            "deviceId": self.device_id,
            "cartItems": [self._cart_item(product, quantity)],
            "replaceCart": replace,
        })
        return await self.get_cart()

    async def get_cart(self) -> CartResult:
        return self._cart_result(await self.hub.call(SERVER, "view_cart", {}))

    # ---- ordering

    @staticmethod
    def _money(value: Any, item_total: Optional[float]) -> Optional[float]:
        """Zepto mostly uses paisa. A '₹280' string is rupees; a bare number far above the
        item total (e.g. 28000 for a ₹250 cart) is paisa."""
        if isinstance(value, str) and "₹" in value:
            return to_float(value)
        v = to_float(value)
        if v is None:
            return None
        if item_total and v > item_total * 5:
            return round(v / 100, 2)
        return v

    async def preview_order(self) -> CartResult:
        """create_online_payment_order with confirmOrder=false: Zepto's own bill preview
        (delivery + handling fees included). Places nothing."""
        self._require_address()
        cart = await self.get_cart()
        if not cart.lines:
            return cart.model_copy(update={"message": "Zepto cart is empty"})
        await self.hub.call_order(SERVER, "get_payment_methods", {})
        data = await self.hub.call_order(SERVER, "create_online_payment_order",
                                         {"confirmOrder": False, "userAddressId": self.address_id})
        total = self._money(first_value(data, _TOTAL_KEYS), cart.item_total)
        charges: dict[str, float] = {}
        for key, name in (("deliveryFee", "delivery"), ("delivery_fee", "delivery"),
                          ("handlingFee", "handling"), ("handling_fee", "handling"),
                          ("smallCartFee", "small_cart"), ("platformFee", "platform"),
                          ("lateNightFee", "late_night")):
            v = self._money(first_value(data, [key]), cart.item_total)
            if v is not None and name not in charges:
                charges[name] = v
        return cart.model_copy(update={
            "total": total,
            "charges": charges,
            "message": str(first_value(data, ["summary", "message"]) or ""),
            "raw": data if isinstance(data, dict) else {"value": data},
        })

    async def place_order(self, approval: OrderApproval) -> PaymentRequest:
        self._require_address()
        preview = await self.preview_order()
        if preview.total is not None and abs(preview.total - approval.amount) > 1:
            raise RuntimeError(f"Zepto bill changed from ₹{approval.amount:g} to ₹{preview.total:g}; "
                               "confirm the new amount")
        data = await self.hub.call_order(SERVER, "create_online_payment_order",
                                         {"confirmOrder": True, "userAddressId": self.address_id}, approval)
        bits = extract_payment_bits(data)
        order_id = first_value(data, ["orderId", "order_id"])
        return PaymentRequest(
            platform="zepto",
            order_id=str(order_id) if order_id is not None else None,
            amount=self._money(first_value(data, _TOTAL_KEYS + ["amount"]), preview.item_total) or preview.total,
            approved_amount=approval.amount,
            message=str(first_value(data, ["summary", "message"]) or ""),
            raw=data if isinstance(data, dict) else {"value": data},
            **bits,
        )

    async def payment_status(self, payment: PaymentRequest) -> PaymentState:
        if not payment.order_id:
            return "UNKNOWN"
        data = await self.hub.call_order(SERVER, "check_payment_status", {"orderId": payment.order_id, "poll": False})
        return payment_state(first_value(data, ["paymentStatus", "payment_status", "status", "state"]))