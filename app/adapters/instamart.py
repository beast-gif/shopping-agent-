"""Swiggy Instamart via Swiggy's official MCP server (https://mcp.swiggy.com/im).

A full grocery app, like Zepto: search, the real cart and bill, and a UPI order after you
type 'confirm <total>'. Same Swiggy account as Swiggy Food, separate MCP server and login.

From Swiggy's reference docs (mcp.swiggy.com/builders/docs/reference/instamart/):
- every response is {success, data, message} (unwrapped by mcp_client.parse_result)
- search_products(query, addressId, offset) -> data.products[]: displayName, brand, productId,
  inStock, isPromoted, variations[]: spinId, skuId, displayName, quantityDescription ('500 ml'),
  price {mrp, offerPrice}, isInStockAndAvailable, imageUrl, rating {value, count}, maxQuantity
- update_cart(items[{spinId, skuId, quantity}], selectedAddressId) REPLACES the whole cart
- get_cart() -> items[]: itemName, quantity, mrp, discountedFinalPrice, spinId, skuId;
  billBreakdown {lineItems [{label, value}], toPay {label, value}}; availablePaymentMethods
- checkout(addressId, paymentMethod='UPI') -> orderId, paasId, upiIntentUrl/bridgeUrl,
  status PENDING_PAYMENT          (blocked for normal calls: only hub.call_order, with approval)
- check_payment_status(paasId, orderId) -> terminal, isTerminalSuccess, isTerminalFailure, confirmed
- confirm_order(orderId, paasId) ONLY after a successful payment that isn't already confirmed
  (unlike Swiggy Food: not on timeout; an unpaid Instamart order just lapses)

Not documented (checked by scripts/probe_instamart.py): whether prices are rupees (assumed,
like Swiggy Food) and the exact bill labels ('Item Total', 'Delivery Fee', 'Handling Fee' ...).
"""
from __future__ import annotations

import re
from typing import Any, Optional

from app.adapters.base import (
    GroceryAdapter,
    extract_payment_bits,
    find_pincode,
    first_value,
    parse_pack_size,
    strip_name_prefix,
    to_float,
)
from app.approval import OrderApproval
from app.debug import save_debug
from app.mcp_client import McpHub
from app.schemas import CartLine, CartResult, PaymentRequest, PaymentState, Product, SavedAddress

SERVER = "instamart"
MAX_RESULTS = 30          # variants kept per search (a product can have several pack sizes)


def _fee_name(label: str) -> str:
    """'Delivery Fee' -> 'delivery', 'Handling Fee' -> 'handling', 'Small Cart Fee' -> 'small cart'"""
    s = re.sub(r"\b(fee|fees|charge|charges)\b", "", label.lower()).strip(" :")
    return re.sub(r"\s+", " ", s) or label.lower()


def _amount(value: Any) -> Optional[float]:
    """'₹30', '30', 30, '-₹20', 'FREE' -> 30, 30, 30, -20, 0"""
    if isinstance(value, str) and value.strip().lower() in ("free", "₹0", "0"):
        return 0.0
    return to_float(value)


class InstamartAdapter(GroceryAdapter):
    platform = "instamart"

    def __init__(self, hub: McpHub):
        super().__init__(hub)
        self.address_id: Optional[str] = None
        self._last_cart: Optional[dict[str, Any]] = None    # the cart update_cart returned

    # ---- addresses (the same Swiggy addresses as Swiggy Food)

    async def list_addresses(self) -> list[SavedAddress]:
        data = await self.hub.call(SERVER, "get_addresses", {})
        out = []
        for a in (data or {}).get("addresses", []) if isinstance(data, dict) else []:
            text = strip_name_prefix(a.get("addressLine") or a.get("address") or "")
            out.append(SavedAddress(platform="instamart", address_id=str(a.get("id")),
                                    label=a.get("addressTag") or a.get("addressCategory") or a.get("annotation"),
                                    text=text, pincode=find_pincode(text)))
        return out

    async def set_address(self, address_id: str) -> None:
        self.address_id = address_id

    def _require_address(self) -> None:
        if not self.address_id:
            raise RuntimeError("Instamart: call set_address() first (the address picks the store)")

    # ---- search

    def _variant(self, p: dict[str, Any], v: dict[str, Any]) -> Optional[Product]:
        price = v.get("price") or {}
        offer = to_float(price.get("offerPrice"))
        if offer is None:
            return None
        size_text = v.get("quantityDescription")
        size, unit = parse_pack_size(size_text)
        name = (v.get("displayName") or p.get("displayName") or "").strip()
        rating = v.get("rating") or {}
        return Product(
            platform="instamart",
            product_id=str(v.get("spinId")),
            store_product_id=str(v.get("skuId")) if v.get("skuId") else None,
            name=name,
            brand=v.get("brandName") or p.get("brand"),
            price=offer,
            mrp=to_float(price.get("mrp")) or offer,
            pack_size=size, pack_unit=unit,
            in_stock=bool(v.get("isInStockAndAvailable", True)) and p.get("inStock") is not False,
            is_ad=bool(p.get("isPromoted")),
            rating=to_float(rating.get("value")) if isinstance(rating, dict) else None,
            image_url=v.get("imageUrl"),
            raw={"spinId": v.get("spinId"), "skuId": v.get("skuId"), "quantityDescription": size_text,
                 "maxQuantity": v.get("maxQuantity")},
        )

    async def search(self, query: str) -> list[Product]:
        self._require_address()
        data = await self.hub.call(SERVER, "search_products", {"query": query, "addressId": self.address_id})
        out: list[Product] = []
        for p in (data or {}).get("products", []) if isinstance(data, dict) else []:
            for v in p.get("variations") or []:
                product = self._variant(p, v)
                if product and product.product_id != "None":
                    out.append(product)
        return out[:MAX_RESULTS]

    async def details(self, product: Product) -> Product:
        return product                      # no nutrition on Instamart; matched Zepto labels are used

    # ---- cart

    @staticmethod
    def _cart_item(product: Product, quantity: int) -> dict[str, Any]:
        raw = product.raw or {}
        spin = raw.get("spinId") or product.product_id
        sku = raw.get("skuId") or product.store_product_id
        if not spin or not sku:
            raise ValueError(f"Instamart product {product.name!r} has no spinId/skuId; search again")
        return {"spinId": str(spin), "skuId": str(sku), "quantity": int(quantity)}

    def _cart_result(self, data: Any, message: str = "") -> CartResult:
        d = data if isinstance(data, dict) else {}
        lines = []
        for it in d.get("items") or []:
            qty = int(to_float(it.get("quantity")) or 0)
            lines.append(CartLine(name=it.get("itemName") or it.get("spinId") or "item", quantity=qty,
                                  price=to_float(it.get("discountedFinalPrice"))))
        item_total, charges = None, {}
        for li in ((d.get("billBreakdown") or {}).get("lineItems") or []):
            label = str(li.get("label") or "")
            value = _amount(li.get("value"))
            if value is None:
                continue
            low = label.lower()
            if "item" in low and "total" in low:
                item_total = value
            elif "total" in low or "to pay" in low:
                continue
            else:
                name = _fee_name(label)
                if any(w in low for w in ("discount", "saving", "coupon", "offer")) and value > 0:
                    value = -value
                charges[name] = round(charges.get(name, 0.0) + value, 2)
        to_pay = (d.get("billBreakdown") or {}).get("toPay") or {}
        total = _amount(to_pay.get("value")) if isinstance(to_pay, dict) else None
        if total is None:
            total = _amount(d.get("cartTotalAmount"))
        if item_total is None:
            known = [ln.price for ln in lines if ln.price is not None]
            item_total = round(sum(known), 2) if known and len(known) == len(lines) else None
        warning = (d.get("cartWarning") or {}).get("message") if isinstance(d.get("cartWarning"), dict) else None
        return CartResult(platform="instamart", ok=bool(lines), lines=lines, item_total=item_total,
                          total=total, charges=charges, cart_id=d.get("cartId"),
                          message=message or warning or d.get("addressWarning") or "", raw=d)

    async def add_many(self, picks: list[tuple[Product, int]], replace: bool = True) -> CartResult:
        """Puts these products in the Instamart cart. update_cart always REPLACES the whole cart,
        so the cart becomes exactly your picks (replace=False keeps what's there and adds)."""
        self._require_address()
        items: dict[str, dict[str, Any]] = {}
        if not replace:
            current = await self.hub.call(SERVER, "get_cart", {})
            for it in (current or {}).get("items", []) if isinstance(current, dict) else []:
                if it.get("spinId") and it.get("skuId"):
                    items[str(it["spinId"])] = {"spinId": str(it["spinId"]), "skuId": str(it["skuId"]),
                                                "quantity": int(to_float(it.get("quantity")) or 1)}
        for product, quantity in picks:
            item = self._cart_item(product, quantity)
            if item["spinId"] in items and not replace:
                item["quantity"] += items[item["spinId"]]["quantity"]
            items[item["spinId"]] = item
        data = await self.hub.call(SERVER, "update_cart", {"items": list(items.values()),
                                                           "selectedAddressId": self.address_id})
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            self._last_cart = data
        save_debug("instamart_update_cart.json", data)
        notes = []
        for r in (data or {}).get("reducedQuantityItems") or [] if isinstance(data, dict) else []:
            notes.append(f"{r.get('itemName')}: only {r.get('cappedQuantity')} allowed (asked {r.get('requestedQuantity')})")
        for r in (data or {}).get("removedOutOfStockItems") or [] if isinstance(data, dict) else []:
            notes.append(f"{r.get('itemName')}: out of stock, removed")
        cart = await self.get_cart()
        return cart.model_copy(update={"message": "; ".join(notes) or cart.message, "ok": cart.ok and not notes})

    async def _cart_data(self) -> Any:
        """get_cart, or (if it fails, e.g. 'The store is unavailable at the moment') the cart that
        update_cart returned, which Swiggy documents as the same cart shape with the bill."""
        try:
            data = await self.hub.call(SERVER, "get_cart", {})
        except Exception:
            if self._last_cart is not None:
                return self._last_cart
            raise
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            self._last_cart = data
        return data

    async def add_to_cart(self, product: Product, quantity: int = 1, replace: bool = False) -> CartResult:
        return await self.add_many([(product, quantity)], replace=replace)

    async def get_cart(self) -> CartResult:
        data = await self._cart_data()
        save_debug("instamart_cart.json", data)
        return self._cart_result(data)

    # ---- ordering (UPI only, after your typed 'confirm <total>')

    async def preview_order(self) -> CartResult:
        """get_cart already has the real bill (fees, discounts, to pay). Places nothing."""
        self._require_address()
        return await self.get_cart()

    async def place_order(self, approval: OrderApproval) -> PaymentRequest:
        self._require_address()
        fresh = await self.get_cart()
        if not fresh.lines:
            raise RuntimeError("Instamart cart is empty; nothing to order")
        if fresh.total is None:
            raise RuntimeError("Instamart did not return a bill total; not placing the order")
        if abs(fresh.total - approval.amount) > 1:
            raise RuntimeError(f"Instamart bill changed from ₹{approval.amount:g} to ₹{fresh.total:g}; "
                               "confirm the new amount")
        methods = [str(m).upper() for m in (fresh.raw.get("availablePaymentMethods") or [])]
        if methods and not any("UPI" in m for m in methods):
            raise RuntimeError("Instamart says UPI isn't available for this cart right now "
                               "(cash on delivery and wallets are never used by this agent)")

        data = await self.hub.call_order(SERVER, "checkout", {"addressId": self.address_id,
                                                              "paymentMethod": "UPI"}, approval)
        save_debug("instamart_checkout.json", data)
        bits = extract_payment_bits(data)
        if not bits.get("payment_url"):
            bits["payment_url"] = first_value(data, ["bridgeUrl"])
        order_id = first_value(data, ["orderId", "order_id"])
        return PaymentRequest(
            platform="instamart",
            order_id=str(order_id) if order_id is not None else None,
            amount=to_float(first_value(data, ["cartTotal", "amount", "toPay"])) or fresh.total,
            approved_amount=approval.amount,
            message=str(first_value(data, ["message", "status"]) or ""),
            extra={k: v for k, v in {"paasId": first_value(data, ["paasId"]),
                                     "transactionId": first_value(data, ["transactionId"])}.items() if v},
            raw=data if isinstance(data, dict) else {"value": data},
            **bits,
        )

    async def payment_status(self, payment: PaymentRequest) -> PaymentState:
        paas_id = payment.extra.get("paasId")
        if not paas_id:
            return "UNKNOWN"
        args = {"paasId": str(paas_id)}
        if payment.order_id:
            args["orderId"] = payment.order_id
        data = await self.hub.call_order(SERVER, "check_payment_status", args)
        if isinstance(data, dict) and data.get("confirmed"):
            payment.extra["confirmed"] = True
        if isinstance(data, dict) and data.get("isTerminalSuccess"):
            return "PAID"
        if isinstance(data, dict) and data.get("isTerminalFailure"):
            return "FAILED"
        return "PENDING"

    async def finalize(self, payment: PaymentRequest, state: PaymentState) -> None:
        """Only after a successful payment that Swiggy hasn't confirmed by itself. Unpaid or
        failed: nothing (Swiggy's docs say not to confirm those; the order just lapses)."""
        if state != "PAID" or payment.extra.get("confirmed") or not payment.order_id:
            return
        args = {"orderId": payment.order_id, "paasId": str(payment.extra.get("paasId"))}
        if payment.extra.get("transactionId"):
            args["transactionId"] = str(payment.extra["transactionId"])
        await self.hub.call_order(SERVER, "confirm_order", args)