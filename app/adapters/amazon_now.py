"""Amazon Now through the website's own JSON endpoints, called from inside the agent's
logged-in browser profile (so the cookies, CSRF token and browser are exactly the site's own).

Facts from the recon (probes/amazon/capture.json):
- every response is {"success": true, "statusCode": "200", "message": "OK", "data": {...}}
- prices are rupees: {"amount": 248.0, "code": "INR"}
- search    GET  /tez/browse/searchByKeyword?keyword=&brandId=&offset=N     (N = page, 10 per page)
- product   GET  /tez/browse/minidp?asin=&brandId=
- cart      POST /tez/order/getcart  {"brandId", "initiatePurchase": false, "withProductDetails": true, ...}
- add       POST /tez/order/atc      {"setQuantityFlag": false, asin, offerId, brandId, storeId, "quantity": 1, ...}
- set qty   POST /tez/order/atc      the same + "setQuantityFlag": true, "itemId", "quantity": N   (0 = remove)
- POSTs carry the header anti-csrftoken-a2z = GET /tez/browse/csrf -> data.CSRFToken
- the real bill (fees after Prime / thresholds) comes from createPurchase, which the CART PAGE calls
  itself when it opens. The agent only listens to that response; it never calls it.
- Amazon Now shows no nutrition table (only description / bullets), so nutrition is usually None.
- the cart is your REAL Amazon Now cart (the phone app shows the same one), tied to the delivery address.

Safety: only the endpoints in ALLOWED can ever be called. There is no checkout/payment endpoint in
it: you press "Proceed to pay" yourself.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlencode

from playwright.async_api import BrowserContext, Page

from app.adapters.base import GroceryAdapter, first_value, parse_nutrition_text, parse_pack_size, to_float
from app.approval import OrderApproval
from app.config import AppConfig
from app.debug import save_debug
from app.schemas import CartLine, CartResult, PaymentRequest, PaymentState, Product, SavedAddress

ALLOWED = {
    ("GET", "/tez/browse/csrf"),
    ("GET", "/tez/browse/searchByKeyword"),
    ("GET", "/tez/browse/minidp"),
    ("GET", "/tez/order/getLastOrder"),
    ("GET", "/portal-migration/hz/glow/get-location-label"),
    ("POST", "/tez/order/getcart"),
    ("POST", "/tez/order/atc"),
    ("POST", "/tez/order/validateLocation"),
}

_FETCH_JS = """async ({url, method, headers, body}) => {
    const r = await fetch(url, {method, headers, credentials: 'include',
                                body: body === null ? undefined : JSON.stringify(body)});
    return {status: r.status, text: await r.text()};
}"""

# cartSavings benefit -> fee name. If the benefit is unlocked the fee is waived (₹0).
_FEE_BENEFITS = {"FREE_SHIPPING": "delivery", "HANDLING_FEES": "handling"}


class AmazonNowError(RuntimeError):
    pass


def money(value: Any) -> Optional[float]:
    """{'amount': 248.0} -> 248.0;  {'currency': {'amount': 10.0}, 'displayString': '₹10.00'} -> 10.0"""
    if value is None:
        return None
    if isinstance(value, (int, float, str)):
        return to_float(value)
    if isinstance(value, dict):
        if value.get("amount") is not None and not isinstance(value["amount"], dict):
            return to_float(value["amount"])
        for key in ("currency", "amount", "unitPrice"):
            if isinstance(value.get(key), dict):
                return money(value[key])
        if value.get("displayString"):
            return to_float(value["displayString"])
    return None


def clean_text(text: Any) -> str:
    """Drops control characters / odd whitespace that can hide a line in the terminal."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f​-‏]", " ", str(text or ""))).strip()


def company_name(manufacturer: Optional[str]) -> Optional[str]:
    """'Hocco Industries Pvt Ltd Survey no 3311, ... Gujarat' -> 'Hocco Industries'
    'Hangyo, Hangyo Ice Creams Private Limited' -> 'Hangyo'"""
    if not manufacturer:
        return None
    name = clean_text(manufacturer).split(",")[0]
    name = re.split(r"\b(?:Pvt|Private|Ltd|Limited|LLP|Survey|Plot|No\.?\s*\d)\b", name, maxsplit=1, flags=re.I)[0]
    name = name.strip(" .-")
    return name or None


# Bill lines always shown, even at ₹0 (so you see that Prime made them free).
_ALWAYS_SHOWN = ("delivery", "handling")


def _fee_name(line_type: str) -> str:
    """'HANDLING_FEE_TAX_INCLUSIVE' -> 'handling';  'SMALL_CART_FEE_TAX_INCLUSIVE' -> 'small cart'"""
    t = re.sub(r"_TAX_INCLUSIVE$", "", line_type.upper())
    t = re.sub(r"_(FEE|FEES|CHARGE|CHARGES)$", "", t)
    return t.lower().replace("_", " ") or "other"


def parse_bill(data: dict[str, Any]) -> dict[str, Any]:
    """createPurchase response -> {'item_total': 248, 'charges': {'handling': 0, 'delivery': 0}, 'total': 248}"""
    pr = (data.get("data") or {}).get("purchaseResponse") or {}
    summary = pr.get("billSummary") or {}
    lines = (summary.get("shortBillSummary") or {}).get("lineItems") or []
    item_total: Optional[float] = None
    total: Optional[float] = None
    charges: dict[str, float] = {}
    for li in lines:
        t = (li.get("type") or "").upper()
        amount = money(li.get("ourPrice"))
        if amount is None:
            continue
        if t.startswith("ITEM_TOTAL"):
            item_total = amount
        elif "TOTAL" in t or "TO_PAY" in t or "PAYABLE" in t:
            total = amount
        else:
            if any(w in t for w in ("DISCOUNT", "PROMOTION", "SAVING", "CASHBACK", "COUPON")) and amount > 0:
                amount = -amount
            name = _fee_name(t)
            if amount == 0 and name not in _ALWAYS_SHOWN:
                continue                   # 'promotions applied ₹0', 'gift card balance ₹0' ...
            charges[name] = round(charges.get(name, 0.0) + amount, 2)
    if total is None:
        total = money(first_value(summary, ["totalAmount", "grandTotal", "amountToPay", "orderTotal", "total"]))
    if total is None and item_total is not None:
        total = round(item_total + sum(charges.values()), 2)
    return {"item_total": item_total, "charges": charges, "total": total, "purchase_id": pr.get("purchaseId")}


class AmazonNowAdapter(GroceryAdapter):
    platform = "amazon_now"

    def __init__(self, context: BrowserContext, cfg: AppConfig):
        super().__init__(None)  # type: ignore[arg-type]   no MCP hub: Amazon Now goes through the browser
        self.context = context
        self.amz = cfg.amazon_now
        self.base = self.amz.base_url.rstrip("/")
        self.brand = self.amz.brand_id
        self.cart_url = f"{self.base}/tez/browse/cart?qcbrand={self.brand}"
        self._page: Optional[Page] = None
        self._csrf: Optional[str] = None
        self.address: Optional[SavedAddress] = None

    # ---- transport

    async def _api_page(self) -> Page:
        """A background tab on a tiny same-origin JSON page: fetch() from it carries your Amazon
        cookies, and nothing heavy has to render (so it works even if the store page is slow)."""
        if self._page and not self._page.is_closed():
            return self._page
        page = await self.context.new_page()
        await page.goto(f"{self.base}/tez/browse/csrf", wait_until="domcontentloaded")
        self._page = page
        return page

    async def _csrf_token(self) -> str:
        if not self._csrf:
            data = await self._call("GET", "/tez/browse/csrf")
            self._csrf = (data or {}).get("CSRFToken")
            if not self._csrf:
                raise AmazonNowError("Amazon Now gave no CSRF token (signed out? run scripts/amazon_login.py)")
        return self._csrf

    async def _call(self, method: str, path: str, params: Optional[dict[str, Any]] = None,
                    body: Optional[dict[str, Any]] = None, _retry: bool = True) -> Any:
        if (method, path) not in ALLOWED:
            raise AmazonNowError(f"blocked: {method} {path} is not on the Amazon Now allowlist")
        url = path + ("?" + urlencode(params) if params else "")
        headers = {"accept": "*/*", "tez-brand-id": self.brand, "tez-group-id": ""}
        if method == "POST":
            headers["content-type"] = "application/json"
            headers["anti-csrftoken-a2z"] = await self._csrf_token()
        page = await self._api_page()
        try:
            res = await page.evaluate(_FETCH_JS, {"url": url, "method": method, "headers": headers, "body": body})
        except Exception as e:  # noqa: BLE001  tab navigated or closed -> open a fresh one once
            if _retry:
                self._page = None
                return await self._call(method, path, params, body, _retry=False)
            raise AmazonNowError(f"{path}: browser call failed ({type(e).__name__}: {str(e)[:120]})") from e

        status, text = res["status"], res["text"]
        if status in (401, 403) and _retry:          # stale CSRF token -> fetch a new one, retry once
            self._csrf = None
            return await self._call(method, path, params, body, _retry=False)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise AmazonNowError(f"{path}: HTTP {status}, not JSON (signed out? run scripts/amazon_login.py)")
        if status >= 400:
            raise AmazonNowError(f"{path}: HTTP {status}: {text[:200]}")
        if not path.startswith("/tez/"):
            return data
        if isinstance(data, dict) and data.get("success") is False:
            raise AmazonNowError(f"{path}: {data.get('message') or data.get('statusCode')}")
        return data.get("data", data) if isinstance(data, dict) else data

    # ---- addresses

    async def current_address(self) -> SavedAddress:
        """The address Amazon Now is delivering to right now (the one at the top of the store)."""
        data = await self._call("GET", "/portal-migration/hz/glow/get-location-label",
                                {"storeContext": "amazontez", "pageType": "amazontez", "actionSource": "tez-glow"})
        ci = (data or {}).get("customerIntent") or {}
        parts = [ci.get("addressLine1"), ci.get("addressLine2"), ci.get("city"), ci.get("zipCode")]
        return SavedAddress(
            platform="amazon_now",
            address_id=str(ci.get("addressId") or ""),
            label=re.sub(r"\s+", " ", ci.get("addressTag") or "").strip() or None,
            text=", ".join(p.strip() for p in parts if p and p.strip()),
            pincode=ci.get("zipCode"),
        )

    async def list_addresses(self) -> list[SavedAddress]:
        """Only the current one for now: switching Amazon addresses wasn't captured yet."""
        return [await self.current_address()]

    async def set_address(self, address_id: str) -> None:
        current = await self.current_address()
        if address_id and address_id != current.address_id:
            raise AmazonNowError(f"Amazon Now is delivering to '{current.label}' ({current.text}). "
                                 "Change it at the top of Amazon Now (app or site); the agent can't switch "
                                 "Amazon addresses yet.")
        data = await self._call("POST", "/tez/order/validateLocation", {"brandId": self.brand},
                                {"addressId": current.address_id, "brandId": self.brand})
        if isinstance(data, dict) and data.get("isServiceable") is False:
            raise AmazonNowError(f"Amazon Now doesn't deliver to '{current.label}' ({current.text}) right now")
        self.address = current

    # ---- search

    def _product(self, p: dict[str, Any]) -> Product:
        facts = {(e.get("id") or ""): e.get("value") for e in p.get("extraProductDetails") or [] if e.get("value")}
        size, unit = parse_pack_size(p.get("packSize") or facts.get("net_quantity"))
        price = money(p.get("buyingPrice")) or money(p.get("price"))
        availability = (p.get("availability") or {}).get("type")
        hero = p.get("heroImage") or {}
        return Product(
            platform="amazon_now",
            product_id=p["asin"],
            store_product_id=p.get("storeId"),
            name=clean_text(p.get("title") or p.get("shortTitle")) or p["asin"],
            price=price or 0.0,
            mrp=money(p.get("listPrice")) or price,
            pack_size=size,
            pack_unit=unit,
            in_stock=price is not None and availability in (None, "IN_STOCK") and p.get("canAddToCart") is not False,
            is_ad=p.get("productType") == "SPONSORED" or bool(p.get("sponsoredProductInfo")),
            rating=to_float(((p.get("customerReviewSummary") or {}).get("rating") or {}).get("value")),
            url=(p.get("shareMessageData") or {}).get("url"),
            image_url=hero.get("highResImageUrl") or hero.get("lowResImageUrl"),
            raw=p,
        )

    async def search(self, query: str, pages: Optional[int] = None) -> list[Product]:
        """pages: how many result pages to read (default amazon_now.search_pages)."""
        out: list[Product] = []
        seen: set[str] = set()
        for page_no in range(max(1, pages or self.amz.search_pages)):
            data = await self._call("GET", "/tez/browse/searchByKeyword", {
                "keyword": query, "brandId": self.brand, "offset": page_no,
                "spellCorrectionDisabled": "false", "searchSource": "search"})
            sr = (data or {}).get("searchResponse") or {}
            for p in sr.get("products") or []:
                if p.get("asin") and p["asin"] not in seen:
                    seen.add(p["asin"])
                    out.append(self._product(p))
            if not (sr.get("metadata") or {}).get("hasMoreResults"):
                break
        return out

    async def details(self, product: Product) -> Product:
        data = await self._call("GET", "/tez/browse/minidp", {"asin": product.product_id, "brandId": self.brand})
        items = (data or {}).get("miniDpPageResponse") or []
        d = next((x for x in items if x.get("asin") == product.product_id), items[0] if items else None)
        if not d:
            return product
        facts = {(e.get("label") or "").lower(): e.get("value") for e in d.get("extraProductDetails") or []
                 if e.get("value")}
        text = " ".join(str(x) for x in [d.get("description"), " ".join(d.get("featureBullets") or []),
                                          d.get("ingredients") if isinstance(d.get("ingredients"), str) else None,
                                          d.get("shortDescription")] if x)
        has_numbers = "kcal" in text.lower() or "energy" in text.lower()
        unit = product.pack_unit if product.pack_unit in ("g", "ml") else "g"
        fresh = self._product(d)
        raw = {**product.raw, "details": d}
        raw.setdefault("offerId", d.get("offerId"))       # keep the search offerId (the one the site adds with)
        return product.model_copy(update={
            "brand": clean_text(facts.get("brand")) or company_name(facts.get("manufacturer")) or product.brand,
            "price": fresh.price or product.price,
            "mrp": fresh.mrp or product.mrp,
            "in_stock": fresh.in_stock,
            "rating": fresh.rating if fresh.rating is not None else product.rating,
            "url": fresh.url or product.url,
            "store_product_id": product.store_product_id or d.get("storeId"),
            "nutrition": parse_nutrition_text(text, basis=unit) if has_numbers else product.nutrition,
            "raw": raw,
        })

    # ---- cart

    async def _cart_raw(self) -> dict[str, Any]:
        data = await self._call("POST", "/tez/order/getcart", {"brandId": self.brand}, {
            "brandId": self.brand, "initiatePurchase": False,
            "withProductDetails": True, "withWalletDetails": False})
        return (data or {}).get("cartResponse") or {}

    @staticmethod
    def _estimate_fees(cr: dict[str, Any]) -> dict[str, float]:
        """Fees from the cart alone (the real bill comes from preview_order). A fee whose benefit is
        unlocked (e.g. Prime free delivery above ₹99) is ₹0; a locked one costs what it would save."""
        charges: dict[str, float] = {}
        extra = cr.get("additionalCharges")
        if isinstance(extra, list):
            for c in extra:
                if isinstance(c, dict):
                    amount = money(c.get("amount") or c.get("charge") or c.get("price"))
                    if amount is not None:
                        name = _fee_name(str(c.get("type") or c.get("chargeType") or "other"))
                        charges[name] = round(charges.get(name, 0.0) + amount, 2)
            if charges:
                return charges
        for s in (cr.get("cartSavings") or {}).get("subSavingsList") or []:
            name = _FEE_BENEFITS.get(s.get("benefitType"))
            if name:
                charges[name] = 0.0 if s.get("unlocked") else (money(s.get("amountSaved")) or 0.0)
        return charges

    def _cart_result(self, cr: dict[str, Any], message: str = "") -> CartResult:
        lines = [CartLine(name=clean_text(it.get("title") or it.get("shortTitle")) or it.get("asin", "item"),
                          quantity=int(it.get("quantity") or 0),
                          price=money(it.get("totalOfferPrice")))
                 for it in cr.get("cartItems") or []]
        item_total = money(cr.get("totalOfferPrice"))
        charges = self._estimate_fees(cr)
        total = round(item_total + sum(charges.values()), 2) if item_total is not None else None
        return CartResult(platform="amazon_now", ok=bool(lines), lines=lines, item_total=item_total,
                          total=total, charges=charges, cart_url=self.cart_url,
                          message=message or ("(fees estimated from the cart)" if lines else "cart is empty"),
                          raw=cr)

    async def get_cart(self) -> CartResult:
        return self._cart_result(await self._cart_raw())

    async def _set_quantity(self, item: dict[str, Any], quantity: int) -> dict[str, Any]:
        data = await self._call("POST", "/tez/order/atc", {"brandId": self.brand}, {
            "setQuantityFlag": True, "asin": item["asin"], "offerId": item["offerId"], "brandId": self.brand,
            "storeId": item["storeId"], "quantity": quantity, "initiatePurchase": True,
            "paymentEntity": None, "itemId": item["itemId"]})
        return (data or {}).get("cartResponse") or {}

    async def other_items(self, keep_asins: list[str]) -> list[CartLine]:
        """What's in your Amazon Now cart besides these products (e.g. things you added on your phone)."""
        cr = await self._cart_raw()
        return [CartLine(name=clean_text(it.get("title")) or it.get("asin", "item"), quantity=int(it.get("quantity") or 0),
                         price=money(it.get("totalOfferPrice")))
                for it in cr.get("cartItems") or [] if it.get("asin") not in keep_asins]

    async def remove_items(self, asins: Optional[list[str]] = None, keep_asins: Optional[list[str]] = None) -> CartResult:
        """remove_items(['B0..']) removes those; remove_items(keep_asins=['B0..']) removes everything else."""
        cr = await self._cart_raw()
        for it in cr.get("cartItems") or []:
            asin = it.get("asin")
            if (asins is not None and asin in asins) or (keep_asins is not None and asin not in keep_asins):
                await self._set_quantity(it, 0)
        return await self.get_cart()

    async def _put(self, product: Product, quantity: int) -> Optional[str]:
        """Sets one product's quantity. Returns a note for you if it couldn't do exactly that."""
        items = (await self._cart_raw()).get("cartItems") or []
        existing = next((it for it in items if it.get("asin") == product.product_id), None)

        if existing is None:
            if not product.in_stock:
                return f"{product.name}: out of stock, skipped"
            offer = product.raw.get("offerId")
            store = product.store_product_id or product.raw.get("storeId")
            if not offer or not store:
                return f"{product.name}: no offerId/storeId (search again), skipped"
            cr = (await self._call("POST", "/tez/order/atc", {"brandId": self.brand}, {
                "setQuantityFlag": False, "asin": product.product_id, "offerId": offer, "brandId": self.brand,
                "storeId": store, "quantity": 1, "initiatePurchase": True, "paymentEntity": None}) or {}
                  ).get("cartResponse") or {}
            existing = next((it for it in cr.get("cartItems") or [] if it.get("asin") == product.product_id), None)
            if existing is None:             # the reply may not list the whole cart: read it again
                items = (await self._cart_raw()).get("cartItems") or []
                existing = next((it for it in items if it.get("asin") == product.product_id), None)
            if existing is None:
                return f"{product.name}: Amazon Now didn't add it"

        note = None
        limit = existing.get("maxAllowedQuantity")
        if limit and quantity > int(limit):
            note = f"{product.name}: Amazon Now allows at most {limit}, so {limit} added (you asked {quantity})"
            quantity = int(limit)
        if int(existing.get("quantity") or 0) != quantity:
            await self._set_quantity(existing, quantity)
        return note

    async def other_items_raw(self, keep_asins: list[str]) -> list[dict[str, Any]]:
        """Like other_items, but with what's needed to put them back later (put_back)."""
        out = []
        for it in (await self._cart_raw()).get("cartItems") or []:
            if it.get("asin") in keep_asins:
                continue
            out.append({"asin": it.get("asin"), "offerId": it.get("offerId"), "storeId": it.get("storeId"),
                        "quantity": int(it.get("quantity") or 0),
                        "title": clean_text(it.get("title")) or it.get("asin"),
                        "price": money(it.get("totalOfferPrice"))})
        return out

    async def _add_one(self, asin: str, offer_id: str, store_id: str) -> Optional[dict[str, Any]]:
        """Adds 1 of a product (like pressing Add). Returns its cart line, or None."""
        cr = (await self._call("POST", "/tez/order/atc", {"brandId": self.brand}, {
            "setQuantityFlag": False, "asin": asin, "offerId": offer_id, "brandId": self.brand,
            "storeId": store_id, "quantity": 1, "initiatePurchase": True, "paymentEntity": None}) or {}
              ).get("cartResponse") or {}
        found = next((it for it in cr.get("cartItems") or [] if it.get("asin") == asin), None)
        if found is None:                    # the reply may not list the whole cart: read it again
            found = next((it for it in (await self._cart_raw()).get("cartItems") or []
                          if it.get("asin") == asin), None)
        return found

    async def put_back(self, items: list[dict[str, Any]]) -> list[str]:
        """Re-adds items set aside with other_items_raw (same quantities).
        Returns the names it couldn't put back (e.g. now out of stock)."""
        failed: list[str] = []
        current = {it.get("asin"): it for it in (await self._cart_raw()).get("cartItems") or []}
        for x in items:
            try:
                line = current.get(x["asin"])
                if line is None and x.get("offerId") and x.get("storeId"):
                    line = await self._add_one(x["asin"], x["offerId"], x["storeId"])
                if line is None:
                    failed.append(x.get("title") or x["asin"])
                    continue
                if int(line.get("quantity") or 0) != x["quantity"]:
                    await self._set_quantity(line, x["quantity"])
            except AmazonNowError:
                failed.append(x.get("title") or x["asin"])
        return failed

    async def quantities(self, asins: list[str]) -> dict[str, int]:
        """How many of each product your Amazon Now cart has right now (0 = not in it)."""
        items = (await self._cart_raw()).get("cartItems") or []
        have = {it.get("asin"): int(it.get("quantity") or 0) for it in items}
        return {a: have.get(a, 0) for a in asins}

    async def set_quantities(self, wanted: dict[str, int]) -> CartResult:
        """Puts products back to these quantities (0 removes). Used to undo the agent's picks
        when you choose another app or cancel: {asin: quantity before the agent touched it}."""
        for it in (await self._cart_raw()).get("cartItems") or []:
            asin = it.get("asin")
            if asin in wanted and int(it.get("quantity") or 0) != wanted[asin]:
                await self._set_quantity(it, wanted[asin])
        return await self.get_cart()

    async def add_many(self, picks: list[tuple[Product, int]], replace: bool = False) -> CartResult:
        """Puts several products in your Amazon Now cart: [(milk, 2), (curd, 1)].
        The same product picked twice is added up. Each product is set to exactly its quantity
        (re-running doesn't double it). replace=True first removes everything else in the cart
        (it's your real cart, so the workflow asks you before using it).
        One product failing (out of stock, over the limit) doesn't stop the others; the reasons
        are in CartResult.message and ok is False."""
        wanted: dict[str, tuple[Product, int]] = {}
        for product, quantity in picks:
            if quantity < 1:
                raise ValueError(f"quantity for {product.name!r} must be at least 1")
            prev = wanted.get(product.product_id)
            wanted[product.product_id] = (product, quantity + (prev[1] if prev else 0))
        if not wanted:
            raise ValueError("pick at least one product")

        if replace:
            await self.remove_items(keep_asins=list(wanted))
        notes = [n for product, quantity in wanted.values() if (n := await self._put(product, quantity))]

        cart = await self.get_cart()
        in_cart = {it.get("asin") for it in cart.raw.get("cartItems") or []}
        added = sum(1 for asin in wanted if asin in in_cart)
        summary = f"{added} of {len(wanted)} products in the cart"
        return cart.model_copy(update={
            "ok": added == len(wanted) and not any("skipped" in n or "didn't" in n for n in notes),
            "message": "; ".join([summary] + notes + [cart.message]),
        })

    async def add_to_cart(self, product: Product, quantity: int = 1, replace: bool = False) -> CartResult:
        """One product: same as add_many([(product, quantity)])."""
        return await self.add_many([(product, quantity)], replace=replace)

    # ---- bill and payment (you pay yourself)

    async def preview_order(self) -> CartResult:
        """Opens the cart page in a background tab and reads the bill that page fetches for itself
        (createPurchase: item total, handling, delivery after Prime). Places nothing."""
        cart = await self.get_cart()
        if not cart.lines:
            return cart.model_copy(update={"message": "Amazon Now cart is empty"})
        page = await self.context.new_page()
        try:
            async with page.expect_response(lambda r: "/tez/order/createPurchase" in r.url, timeout=30_000) as info:
                await page.goto(self.cart_url, wait_until="domcontentloaded")
            data = await (await info.value).json()
        except Exception as e:  # noqa: BLE001  page didn't load -> keep the cart-based estimate
            return cart.model_copy(update={"message": f"(fees estimated; the cart page didn't load: {type(e).__name__})"})
        finally:
            await page.close()
        save_debug("amazon_bill.json", data)
        bill = parse_bill(data if isinstance(data, dict) else {})
        if bill["total"] is None:
            return cart.model_copy(update={"message": "(fees estimated; couldn't read the bill)"})
        return cart.model_copy(update={
            "item_total": bill["item_total"] if bill["item_total"] is not None else cart.item_total,
            "charges": bill["charges"], "total": bill["total"], "cart_id": bill["purchase_id"],
            "message": "", "raw": {**cart.raw, "bill": data}})

    async def _last_order_id(self) -> Optional[str]:
        data = await self._call("GET", "/tez/order/getLastOrder", {"brandId": self.brand})
        save_debug("amazon_last_order.json", data)
        value = first_value(data, ["orderId", "purchaseId", "order_id", "id"])
        return str(value) if value is not None else None

    async def place_order(self, approval: OrderApproval) -> PaymentRequest:
        """The agent never checks out on Amazon. It checks the bill is the one you approved, then
        opens the cart page in the browser window: you press 'Proceed to pay' (or pay from the
        Amazon app: it's the same cart)."""
        preview = await self.preview_order()
        if preview.total is not None and abs(preview.total - approval.amount) > 1:
            raise AmazonNowError(f"Amazon Now bill changed from ₹{approval.amount:g} to ₹{preview.total:g}; "
                                 "confirm the new amount")
        before = await self._last_order_id()
        page = await self.context.new_page()
        await page.goto(self.cart_url, wait_until="domcontentloaded")
        await page.bring_to_front()
        return PaymentRequest(
            platform="amazon_now", amount=preview.total, approved_amount=approval.amount,
            payment_url=self.cart_url,
            message="Press 'Proceed to pay' in the Amazon window (or pay from the Amazon app; same cart).",
            extra={"last_order_before": before})

    async def payment_status(self, payment: PaymentRequest) -> PaymentState:
        """PAID once Amazon reports a newer last order than before you were sent to pay."""
        latest = await self._last_order_id()
        if latest and latest != payment.extra.get("last_order_before"):
            return "PAID"
        return "PENDING"