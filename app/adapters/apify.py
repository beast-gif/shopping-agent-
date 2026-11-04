"""Blinkit and Flipkart Minutes through Apify scraper Actors, over Apify's MCP server.

COMPARE-ONLY: the Actors read prices and stock; there is no cart and no order. The workflow
shows their prices next to Zepto / Amazon Now and, if you pick one of them, gives you the
product links to open in that app.

How one search runs (from probes/apify_*.json):
  1. call the Actor tool (s-r--blinkit-scraper / clearpath--flipkart-minutes-scraper) with
     waitSecs=45. It returns the RUN, not the products:
       {"runId": ..., "status": "RUNNING"|"SUCCEEDED"|..., "storages": {"datasets": {"default":
        {"id": "<datasetId>", "itemCount": 10}}}}
  2. while it's still running and has fewer rows than we want: get-actor-run (waitSecs=30)
  3. get-dataset-items(datasetId) -> the rows
Flipkart Minutes took ~11 s in the probe; Blinkit was still running at 30 s but already had
its 10 rows, so rows are read as soon as there are enough, even if the run isn't finished.

Rows:
  Blinkit: record_type/_type 'offer' (products) or 'fulfillment_location' (the dark store);
    product_id, title, brand, unit ('250 ml'), price, mrp, availability ('InStock'),
    inventory, image_url, url, rating, eta_minutes
  Flipkart Minutes: type 'product' or 'store'; id/listingId/itemId, title/name, brand,
    packSize, price, mrp, inStock, stockState, images[], url, sponsored, deliveryMinutes,
    serviceable (store rows), deliveryCharge
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from app.adapters.base import GroceryAdapter, parse_pack_size, to_float
from app.approval import OrderApproval
from app.config import AppConfig
from app.debug import save_debug
from app.mcp_client import McpHub
from app.schemas import CartResult, PaymentRequest, PaymentState, Product, SavedAddress

SERVER = "apify"
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"}


class CompareOnly(RuntimeError):
    """Raised by cart/order methods: these apps can only be compared, not ordered from."""


def _items(data: Any) -> list[dict[str, Any]]:
    """get-dataset-items may return a list, or a dict holding the list."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "data", "results", "rows"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    return []


class ApifyRunner:
    """Runs one Actor search and returns its dataset rows. Keeps a running tally of results,
    so the workflow can show roughly what the searches cost."""

    def __init__(self, hub: McpHub, cfg: AppConfig, log: Callable[[str], None] = print):
        self.hub = hub
        self.cfg = cfg.apify
        self.log = log
        self.results: dict[str, int] = {}          # app -> rows billed so far (this session)
        self.runs: dict[str, int] = {}

    async def run(self, app: str, tool: str, args: dict[str, Any], want: int) -> list[dict[str, Any]]:
        started = time.monotonic()
        res = await self.hub.call(SERVER, tool, {**args, "waitSecs": 45})
        run_id = (res or {}).get("runId")
        dataset = (((res or {}).get("storages") or {}).get("datasets") or {}).get("default") or {}
        dataset_id = dataset.get("id")
        status = str((res or {}).get("status") or "")
        count = int(dataset.get("itemCount") or 0)
        if not run_id or not dataset_id:
            save_debug(f"apify_{app}_start.json", res)
            raise RuntimeError(f"{app}: Apify returned no run/dataset id")

        while status not in TERMINAL and count < want and time.monotonic() - started < self.cfg.timeout_s:
            res = await self.hub.call(SERVER, "get-actor-run", {"runId": run_id, "waitSecs": 30})
            status = str((res or {}).get("status") or "")
            dataset = (((res or {}).get("storages") or {}).get("datasets") or {}).get("default") or {}
            count = int(dataset.get("itemCount") or count)
        if status not in TERMINAL and count == 0:
            try:                                   # nothing after the timeout: stop paying for it
                await self.hub.call(SERVER, "abort-actor-run", {"runId": run_id, "gracefully": True})
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"{app}: no results after {int(time.monotonic() - started)} s (status {status})")
        if status in {"FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"} and count == 0:
            raise RuntimeError(f"{app}: Apify run {status.lower()}")

        rows = _items(await self.hub.call(SERVER, "get-dataset-items",
                                          {"datasetId": dataset_id, "limit": want + 20, "clean": True}))
        save_debug(f"apify_{app}_items.json", rows)
        self.results[app] = self.results.get(app, 0) + len(rows)
        self.runs[app] = self.runs.get(app, 0) + 1
        self.log(f"  {app.replace('_', ' ').title()}: {len(rows)} rows in {time.monotonic() - started:.0f} s "
                 f"({status.lower() or 'running'})")
        return rows

    def cost_usd(self) -> float:
        """Rough cost of this session's Apify searches, from config prices."""
        return round(sum(n * self.cfg.usd_per_1000.get(app, 5.0) / 1000 for app, n in self.results.items())
                     + sum(self.runs.values()) * self.cfg.usd_per_run, 3)


class _CompareOnlyAdapter(GroceryAdapter):
    """Shared by Blinkit and Flipkart Minutes: location by coordinates, search only."""
    platform: str
    cart_supported = False

    def __init__(self, runner: ApifyRunner, cfg: AppConfig):
        super().__init__(runner.hub)
        self.runner = runner
        self.cfg = cfg
        self.lat: Optional[float] = None
        self.lng: Optional[float] = None
        self.label = ""

    def set_location(self, lat: float, lng: float, label: str = "") -> None:
        self.lat, self.lng, self.label = lat, lng, label

    def _require_location(self) -> None:
        if self.lat is None or self.lng is None:
            raise RuntimeError(f"{self.platform}: no coordinates for this place (its Zepto address has them)")

    # ---- the GroceryAdapter interface (addresses / cart / orders don't exist here)

    async def list_addresses(self) -> list[SavedAddress]:
        return []

    async def set_address(self, address_id: str) -> None:
        raise CompareOnly(f"{self.platform} uses coordinates: call set_location(lat, lng)")

    async def details(self, product: Product) -> Product:
        return product                             # no nutrition here; matched Zepto labels are used

    async def add_to_cart(self, product: Product, quantity: int = 1, replace: bool = False) -> CartResult:
        raise CompareOnly(f"{self.platform} is compare-only: open the product in the app")

    async def get_cart(self) -> CartResult:
        raise CompareOnly(f"{self.platform} is compare-only")

    async def preview_order(self) -> CartResult:
        raise CompareOnly(f"{self.platform} is compare-only")

    async def place_order(self, approval: OrderApproval) -> PaymentRequest:
        raise CompareOnly(f"{self.platform} is compare-only")

    async def payment_status(self, payment: PaymentRequest) -> PaymentState:
        return "UNKNOWN"


class BlinkitAdapter(_CompareOnlyAdapter):
    platform = "blinkit"

    async def search(self, query: str) -> list[Product]:
        self._require_location()
        want = self.cfg.apify.max_items
        rows = await self.runner.run("blinkit", self.cfg.apify.blinkit_tool, {
            "queries": [query], "locations": [{"lat": self.lat, "lon": self.lng}],
            "max_items": want, "include_out_of_stock": False, "include_stores": True}, want)
        out = []
        for r in rows:
            kind = str(r.get("record_type") or r.get("_type") or "offer")
            if kind != "offer" or not r.get("title"):
                continue
            price = to_float(r.get("price"))
            if price is None:
                continue
            size, unit = parse_pack_size(r.get("unit"))
            available = str(r.get("availability") or "").lower()
            out.append(Product(
                platform="blinkit",
                product_id=str(r.get("product_id") or r.get("row_id") or r["title"]),
                name=str(r["title"]).strip(),
                brand=r.get("brand"),
                price=price,
                mrp=to_float(r.get("mrp")) or price,
                pack_size=size, pack_unit=unit,
                in_stock=("out" not in available) and (to_float(r.get("inventory")) or 1) > 0,
                rating=to_float(r.get("rating")),
                url=r.get("url"),
                image_url=r.get("image_url"),
                raw={"url": r.get("url"), "unit": r.get("unit"), "eta_minutes": r.get("eta_minutes")},
            ))
        return out[:want]


class FlipkartMinutesAdapter(_CompareOnlyAdapter):
    platform = "flipkart_minutes"

    async def search(self, query: str) -> list[Product]:
        self._require_location()
        want = self.cfg.apify.max_items
        rows = await self.runner.run("flipkart_minutes", self.cfg.apify.flipkart_tool, {
            "customLocations": [f"{self.lat},{self.lng},{self.label or 'here'}"],
            "searchTerms": [query], "maxItems": want, "onlyInStock": True, "includeDetails": False}, want + 1)
        stores = [r for r in rows if r.get("type") == "store"]
        if stores and all(s.get("serviceable") is False for s in stores):
            raise RuntimeError("Flipkart Minutes doesn't deliver to this place")
        out = []
        for r in rows:
            if r.get("type") not in (None, "product"):
                continue
            title = r.get("title") or r.get("name")
            price = to_float(r.get("price"))
            if not title or price is None:
                continue
            size, unit = parse_pack_size(r.get("packSize") or title)
            images = r.get("images") or []
            out.append(Product(
                platform="flipkart_minutes",
                product_id=str(r.get("listingId") or r.get("id") or r.get("itemId") or title),
                name=str(title).strip(),
                brand=r.get("brand"),
                price=price,
                mrp=to_float(r.get("mrp")) or price,
                pack_size=size, pack_unit=unit,
                in_stock=r.get("inStock") is not False,
                is_ad=bool(r.get("sponsored")),
                url=r.get("url"),
                image_url=images[0] if images and isinstance(images[0], str) else None,
                raw={"url": r.get("url"), "packSize": r.get("packSize"),
                     "deliveryMinutes": r.get("deliveryMinutes"), "deliveryCharge": r.get("deliveryCharge")},
            ))
        return out[:want]