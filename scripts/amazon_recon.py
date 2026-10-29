"""Recon for Amazon Now: YOU click through Amazon Now once, and this records what each page
looks like and which background (JSON) requests the site makes. The Phase 3b adapter is
then written against that, the same way the MCP adapters were written against the probes.

Nothing is ordered: stop at the cart.

Run from the project root (after scripts/amazon_login.py):
    python scripts/amazon_recon.py

Writes probes/amazon/:
    recon.json        page digests + background requests   <- send this
    <step>.png        a screenshot of each step              <- send these
    <step>.html       full page HTML (only if I ask for it)
    net/NNN.json      full background responses (only if I ask for it)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.browser import amazon_browser, signed_in_name  # noqa: E402
from app.config import load_config  # noqa: E402

OUT = ROOT / "probes" / "amazon"
NET = OUT / "net"

STEPS = [
    ("now_store", "In the Chrome window, open Amazon Now (the 'Now' tab/banner near the top). If you\n"
                  "don't see it, set your delivery address to home first. Then press Enter here."),
    ("now_search", "Search for: ice cream  (inside Amazon Now, not regular Amazon). Then press Enter."),
    ("now_product", "Open one product from those results. Then press Enter."),
    ("now_cart", "Add it to the cart and open the cart. DON'T place the order. Then press Enter."),
]

# Background requests we never care about (ads, metrics, images).
NOISE = ("fls-", "unagi", "/uedata", "csm", "metrics", "rd/uedata", "images-amazon", "m.media-amazon",
         "/1/events", "adsystem", "doubleclick", "/ah/ajax/counter", "aax-", "/beacon")

DIGEST_JS = r"""
() => {
  const txt = el => ((el && el.innerText) || '').trim().replace(/\s+/g, ' ').slice(0, 160);
  const cls = el => (typeof el.className === 'string' ? el.className : (el.getAttribute('class') || '')).slice(0, 120);
  const label = el => txt(el) || el.value || el.getAttribute('aria-label') || '';
  const all = sel => Array.from(document.querySelectorAll(sel));
  return {
    asins: all('[data-asin]').filter(e => e.getAttribute('data-asin')).slice(0, 40).map(e => ({
      tag: e.tagName, asin: e.getAttribute('data-asin'), cls: cls(e),
      component: e.getAttribute('data-component-type'), text: txt(e)})),
    prices: all('.a-price, [class*="price" i]').slice(0, 40).map(e => ({tag: e.tagName, cls: cls(e), text: txt(e)})),
    buttons: all('button, input[type=submit], input[type=button], [role=button], a[role=button]')
      .filter(e => /add|cart|buy|now|checkout|remove|\+|−|-/i.test(label(e))).slice(0, 40)
      .map(e => ({tag: e.tagName, id: e.id, name: e.getAttribute('name'), cls: cls(e),
                  aria: e.getAttribute('aria-label'), text: label(e).slice(0, 80)})),
    inputs: all('input[type=text], input[type=search], input:not([type])').slice(0, 15)
      .map(e => ({id: e.id, name: e.getAttribute('name'), cls: cls(e), placeholder: e.placeholder})),
    forms: all('form').slice(0, 10).map(f => ({action: f.action, method: f.method, id: f.id})),
    links: all('a[href]').map(a => ({href: a.href, text: txt(a)}))
      .filter(l => /tez|now|alm|fresh|quick|minute/i.test(l.href + ' ' + l.text)).slice(0, 60),
    markers: Array.from(new Set(all('[data-testid],[data-csa-c-type],[data-component-type]').map(e =>
      e.getAttribute('data-testid') || e.getAttribute('data-csa-c-type') || e.getAttribute('data-component-type')))).slice(0, 80),
    text_sample: ((document.body && document.body.innerText) || '').slice(0, 4000),
  };
}
"""


async def capture(context, step: str) -> dict[str, Any]:
    page = context.pages[-1]                  # the tab you're looking at (newest)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(2)                    # let late content render
    OUT.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(OUT / f"{step}.png"))
    (OUT / f"{step}.html").write_text(await page.content(), encoding="utf-8")
    digest = await page.evaluate(DIGEST_JS)
    info = {"step": step, "url": page.url, "title": await page.title(), **digest}
    print(f"  captured {step}: {info['title'][:60]}  ({page.url[:90]})")
    print(f"     {len(digest['asins'])} product blocks, {len(digest['prices'])} price elements, "
          f"{len(digest['buttons'])} cart/add buttons")
    return info


async def main() -> None:
    cfg = load_config()
    NET.mkdir(parents=True, exist_ok=True)
    network: list[dict[str, Any]] = []
    current = {"step": "home"}

    async def on_response(response) -> None:
        url = response.url
        if "amazon." not in url or any(n in url for n in NOISE):
            return
        ctype = (response.headers or {}).get("content-type", "")
        if "json" not in ctype:
            return
        try:
            body = await response.text()
        except Exception:  # noqa: BLE001  redirects / closed pages
            return
        n = len(network) + 1
        if len(body) < 2_000_000:
            (NET / f"{n:03}.json").write_text(body, encoding="utf-8")
        network.append({"n": n, "step": current["step"], "method": response.request.method,
                        "status": response.status, "url": url[:400], "bytes": len(body),
                        "preview": body[:1500]})

    async with amazon_browser(cfg, headless=False) as context:
        context.on("response", on_response)
        name = await signed_in_name(context, cfg.amazon_now.base_url)
        if not name:
            print("Not signed in. Run scripts/amazon_login.py first.")
            return
        print(f"Signed in as {name}. Recording...\n")

        pages = [await capture(context, "home")]
        for step, instruction in STEPS:
            current["step"] = step
            print(f"\n{instruction}")
            answer = await asyncio.to_thread(input, "> (Enter = capture, skip = skip this step, stop = finish) ")
            if answer.strip().lower() == "stop":
                break
            if answer.strip().lower() == "skip":
                continue
            pages.append(await capture(context, step))

        (OUT / "recon.json").write_text(
            json.dumps({"pages": pages, "network": network}, indent=2, ensure_ascii=False), encoding="utf-8")

    by_step: dict[str, int] = {}
    for r in network:
        by_step[r["step"]] = by_step.get(r["step"], 0) + 1
    print(f"\nSaved {len(pages)} pages and {len(network)} background JSON responses "
          f"({', '.join(f'{k}: {v}' for k, v in by_step.items()) or 'none'}).")
    print(f"Send me {OUT / 'recon.json'} and the .png screenshots in {OUT}.")


if __name__ == "__main__":
    asyncio.run(main())