"""Recon v2 for Amazon Now: records what the page SENDS (request body + headers) for each
cart action, not just what Amazon answers. Phase 3b needs this to add/remove items itself.

Nothing is ordered. Your cookies are never written to the file.

Run from the project root:
    python scripts/amazon_capture.py

Writes probes/amazon/capture.json   <- send me this one file
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.browser import amazon_browser, signed_in_name  # noqa: E402
from app.config import load_config  # noqa: E402

OUT = ROOT / "probes" / "amazon"

STEPS = [
    ("search", "In Amazon Now, search for: milk. Then press Enter here."),
    ("add", "On the results page, press the yellow + (Add) on ONE product. Then press Enter."),
    ("plus", "Press + on that same product again (quantity 2). Then press Enter."),
    ("minus", "Press - twice on it (back to 0, removed). Then press Enter."),
    ("detail_add", "Click a product's picture to open its panel, press 'Add to Cart' there. Then press Enter."),
    ("cart", "Open the cart. Remove that item with -. DON'T press 'Proceed to pay'. Then press Enter."),
    ("address", "Optional (type skip if you like): click the address at the top of Amazon Now, pick\n"
                "another saved address (e.g. college), then switch back to home. Then press Enter."),
]

# Only Amazon Now's own calls and the address switcher.
WANTED = ("/tez/", "/glow/", "address")
# Huge page-layout responses we don't need the body of.
NO_BODY = ("widgets", "storefront", "recs/", "productRecommendations", "layout", "getOverlayWidgets",
           "suggestions")
# Headers never written to the file.
SECRET_HEADERS = ("cookie", "authorization", "x-amz-access-token")
MAX_BODY = 400_000


def endpoint(url: str) -> str:
    return urlparse(url).path


async def main() -> None:
    cfg = load_config()
    calls: list[dict[str, Any]] = []
    current = {"step": "open"}

    async def on_response(response) -> None:
        url = response.url
        if "amazon." not in url or not any(w in url for w in WANTED):
            return
        req = response.request
        if req.resource_type not in ("xhr", "fetch"):
            return
        try:
            headers = await req.all_headers()
        except Exception:  # noqa: BLE001
            headers = dict(req.headers or {})
        headers = {k: v for k, v in headers.items() if k.lower() not in SECRET_HEADERS}
        try:
            body = await response.text()
        except Exception:  # noqa: BLE001
            body = ""
        keep_body = not any(n in url for n in NO_BODY) and len(body) <= MAX_BODY
        calls.append({
            "n": len(calls) + 1, "step": current["step"], "method": req.method,
            "endpoint": endpoint(url), "url": url[:600], "status": response.status,
            "request_headers": headers, "request_body": (req.post_data or "")[:20_000],
            "response_bytes": len(body),
            "response": body if keep_body else body[:1500],
        })

    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "capture.json"
    info: dict[str, Any] = {"direct_url": "", "brand": "", "direct_tests": {}}

    def save() -> None:
        """Written after every step, so a crash later never loses what was already captured."""
        out.write_text(json.dumps({**info, "calls": calls}, indent=2, ensure_ascii=False), encoding="utf-8")

    async with amazon_browser(cfg, headless=False) as context:
        context.on("response", on_response)
        name = await signed_in_name(context, cfg.amazon_now.base_url)
        if not name:
            print("Not signed in. Run scripts/amazon_login.py first.")
            return
        page = context.pages[0]

        # Does /tez/browse open Amazon Now directly (so the agent can go there by itself)?
        await page.goto(cfg.amazon_now.base_url.rstrip("/") + "/tez/browse", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        info["direct_url"] = page.url
        print(f"Signed in as {name}. Opened {page.url[:100]}")
        print("If that isn't the Amazon Now store, click into Amazon Now yourself before step 1.\n")

        for step, instruction in STEPS:
            current["step"] = step
            print(f"\n[{step}] {instruction}")
            answer = (await asyncio.to_thread(input, "> (Enter = done, skip, stop) ")).strip().lower()
            save()
            if answer == "stop":
                break

        # Read-only checks: can the agent call search/product directly from the page?
        current["step"] = "direct_tests"
        await asyncio.sleep(2)                       # let any reload (e.g. after an address switch) finish
        brand = ""
        for u in [p.url for p in context.pages] + [c["url"] for c in calls]:
            q = parse_qs(urlparse(u).query)
            brand = (q.get("qcbrand") or q.get("brandId") or [""])[0]
            if brand:
                break
        asin = ""
        for c in calls:
            if c["endpoint"].endswith("searchByKeyword") and '"asin":"' in c["response"]:
                asin = c["response"].split('"asin":"', 1)[1].split('"', 1)[0]
                break
        info["brand"] = brand
        js = """async (u) => { try { const r = await fetch(u, {credentials: 'include',
                   headers: {'accept': 'application/json'}});
                   const t = await r.text(); return {status: r.status, bytes: t.length, head: t.slice(0, 300)}; }
                 catch (e) { return {error: String(e)}; } }"""

        async def probe(url: str) -> dict[str, Any]:
            for attempt in range(3):
                page = context.pages[-1]
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    return await page.evaluate(js, url)
                except Exception as e:  # noqa: BLE001  page navigated mid-call -> wait and retry
                    err = f"{type(e).__name__}: {str(e)[:150]}"
                    await asyncio.sleep(2)
            return {"error": err}

        tests = info["direct_tests"]
        if brand:
            tests["search"] = await probe(f"/tez/browse/searchByKeyword?keyword=curd&brandId={brand}&offset=0")
            if asin:
                tests["minidp"] = await probe(f"/tez/browse/minidp?asin={asin}&brandId={brand}")
        tests["csrf"] = await probe("/tez/browse/csrf")
        await asyncio.sleep(1)
        save()

    save()
    by_ep: dict[str, int] = {}
    for c in calls:
        if c["method"] == "POST":
            by_ep[c["endpoint"]] = by_ep.get(c["endpoint"], 0) + 1
    print(f"\nRecorded {len(calls)} calls. POSTs: " + ", ".join(f"{k.split('/')[-1]} x{v}" for k, v in by_ep.items()))
    print(f"Direct tests: " + ", ".join(f"{k}={v.get('status', v.get('error'))}" for k, v in tests.items()))
    print(f"Send me {out}")


if __name__ == "__main__":
    asyncio.run(main())