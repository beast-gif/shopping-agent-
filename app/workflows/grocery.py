"""Grocery workflow (LangGraph): find, compare, pick, put in the carts, pay. You choose at every ⏸:

  find_place ─(unknown)─> ask_place ⏸
      │
  next_item   ('ice cream, chocolates' -> 'ice cream' now, 'chocolates' next)  <────────────┐
      │                                                                                    │
  plan     (LLM: 'guilt free ice cream' -> search terms + limits per 100 g/ml)             │
      │
  ask_budget ⏸   (skipped if you gave one: --budget 300 or 'under 300')
      │
  search   (Zepto ∥ Amazon Now, drop unrelated results, read Zepto nutrition labels,
            match the same product across apps, health score G, rank)
      │
  ask_products ⏸   ("1x2, 3", "/mango", "/*", skip, cancel)  -> picks go into the BASKET
      │  (more items queued -> next_item) ─────────────────────────────────────────────────┤
  ask_more ⏸   (basket so far; type another item -> next_item ┘, 'remove 2', 'bill')
      │ bill
  prepare  (notes what's already in your real Amazon Now cart)
      │  ─(other items there)─> ask_leftovers ⏸  ("set those aside while comparing? Y/n";
      │                                             they're put back afterwards)
  quote    (your picks in BOTH carts, real bills with fees)
      │
  ask_app ⏸ ── ask_confirm ⏸ ── place_order
                  Zepto:  'confirm 133' -> UPI QR -> wait for payment
                  Amazon: 'open' -> cart opens in Brave -> you press Proceed to pay -> wait
  back (-> ask_more) / cancel / choosing Zepto -> cleanup: your picks go back out of the Amazon Now cart

Same rules as the food workflow: every node returns {'route': next}; state holds only plain
dicts; nodes that ask you (interrupt) make no network calls, because LangGraph re-runs them
when you answer.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.adapters.amazon_now import money
from app.adapters.base import GroceryAdapter, find_pincode
from app.approval import ApprovalError, OrderApproval, approve_order, confirm_phrase
from app.config import AppConfig
from app.debug import save_debug
from app.llm import structured
from app.location import detect_place, remember_place
from app.mcp_client import describe_error
from app.payment import show_payment, wait_for_payment
from app.places import Place, load_places, match_place_name
from app.schemas import Product
from app.workflows.grocery_logic import (
    APP_LABEL,
    PLAN_PROMPT,
    GroceryPlan,
    ProductGroup,
    add_to_basket,
    basket_picks,
    basket_spent,
    best_quote,
    build_groups,
    clean_text_for_claims,
    compare_total,
    fallback_plan,
    groups_table,
    groups_view,
    parse_picks,
    picks_for_app,
    quotes_table,
    relax_limits,
    relevant,
    selection_table,
    split_request,
    within_budget,
)

APP_ORDER = ["zepto", "amazon_now"]
CANCEL = {"cancel", "q", "quit", "exit", "stop"}


class GroceryState(TypedDict, total=False):
    request: str                     # what you typed: 'ice cream, chocolates' or the current item
    queue: Optional[list[str]]       # items still to search: ['chocolates', 'chips']
    basket: list[dict]               # [{'group': ProductGroup dump, 'qty': 2, 'request': 'ice cream'}]
    place_hint: Optional[str]
    place: Optional[dict]            # {'name', 'how', 'addresses': {app: id}, 'texts': {app: text}}
    apps: list[str]                  # apps used for this request
    plan: Optional[dict]             # GroceryPlan dump
    budget: Optional[int]            # None = not asked yet, 0 = no limit
    groups: Optional[list[dict]]     # ProductGroup dumps, ranked, numbered 1..n
    view: Optional[str]              # '/word' filter, '*' = all
    amazon_before: Optional[dict]    # {asin: qty in your Amazon cart before the agent}, to undo
    amazon_aside: list[dict]         # other items already in your Amazon cart (to put back later)
    amazon_replace: bool             # you said set them aside while comparing
    quotes: Optional[list[dict]]     # [{'app', 'cart', 'missing', 'note', 'others_value'}]
    chosen: Optional[int]
    typed_confirm: Optional[str]
    after: Optional[str]             # where cleanup goes: 'ask_more' | 'cancel' | 'end'
    payment: Optional[dict]
    error: Optional[str]
    done: Optional[str]
    route: str


def build_grocery_graph(cfg: AppConfig, adapters: dict[str, GroceryAdapter],
                        log: Callable[[str], None] = print) -> StateGraph:
    gs = cfg.scoring.grocery

    def finish(msg: str) -> dict:
        return {"done": msg, "route": END}

    def with_error(state: GroceryState, text: str) -> str:
        return (f"⚠ {state['error']}\n" if state.get("error") else "") + text

    def label(app: str) -> str:
        return APP_LABEL.get(app, app)

    # ------------------------------------------------------------ place

    def set_place(place: Place, how: str) -> dict:
        apps = [a for a in APP_ORDER if a in adapters and (a == "amazon_now" or place.address_for(a))]
        remember_place(place)
        log(f"Delivering to {place.name} ({how}).")
        if "zepto" in adapters and "zepto" not in apps:
            log(f"  Zepto has no saved address at {place.name}, so it's left out.")
        if not apps:
            return finish(f"No grocery app can deliver to {place.name}.")
        return {"place": {"name": place.name, "how": how, "addresses": dict(place.addresses),
                          "texts": dict(place.texts)},
                "apps": apps, "error": None, "route": "next_item"}

    async def find_place(state: GroceryState) -> dict:
        book = load_places(cfg)
        if not book.places:
            return finish("No places set up yet. Run: python scripts/setup_places.py")
        if cfg.locations.always_ask and not state.get("place_hint"):
            return {"place": None, "route": "ask_place"}
        found = await detect_place(book, cfg, state.get("place_hint"))
        if found.place is None:
            log(f"Couldn't work out where you are: {found.how}")
            return {"place": None, "route": "ask_place"}
        return set_place(found.place, found.how)

    def ask_place(state: GroceryState) -> dict:
        book = load_places(cfg)
        names = ", ".join(p.name for p in book.places)
        answer = str(interrupt({"prompt": with_error(state, f"Where should it be delivered? ({names}, or cancel)"),
                                "options": [p.name for p in book.places] + ["cancel"]})).strip()
        if answer.lower() in CANCEL:
            return finish("Cancelled.")
        place = match_place_name(book, answer)
        if not place:
            return {"error": f"No place called '{answer}'.", "route": "ask_place"}
        return set_place(place, f"you said '{answer}'")

    # ------------------------------------------------------------ basket: one item at a time

    def next_item(state: GroceryState) -> dict:
        """Takes the next item from the queue ('ice cream, chocolates' -> 'ice cream' now,
        'chocolates' next). When the queue is empty, you're asked what else to add."""
        queue = state.get("queue")
        if queue is None:                                    # first time: split what you typed
            queue = split_request(state["request"])
        if not queue:
            return {"queue": [], "route": "ask_more" if state.get("basket") else END}
        item, rest = queue[0], queue[1:]
        if state.get("basket") or rest or len(split_request(state["request"])) > 1:
            log(f"\n── {item} ──" + (f"   (then: {', '.join(rest)})" if rest else ""))
        return {"request": item, "queue": rest, "plan": None, "groups": None, "view": None,
                "error": None, "route": "plan"}

    def after_item(state: GroceryState, extra: Optional[dict] = None) -> dict:
        """Where to go once an item is picked or skipped: the next queued item, or ask_more."""
        return {**(extra or {}), "error": None, "route": "next_item" if state.get("queue") else "ask_more"}

    def ask_more(state: GroceryState) -> dict:
        basket = state.get("basket") or []
        budget = state.get("budget") or None
        if basket:
            head = ("Your basket (listed prices, before fees):\n"
                    + selection_table(basket_picks(basket), state["apps"], budget))
            if budget:
                head += f"\nBudget ₹{budget}: about ₹{max(0.0, budget - basket_spent(basket)):g} left (cheapest prices)"
        else:
            head = "Your basket is empty."
        prompt = with_error(state, head + "\nAdd more: type an item (chocolates, or several: chips, curd)"
                            + "\nbill = put the basket in the carts and show real bills"
                            + "\nremove 2 = drop line 2   cancel = stop")
        options = (["bill"] if basket else []) + ["cancel"]
        answer = str(interrupt({"prompt": prompt, "options": options})).strip()
        low = answer.lower()
        if low in CANCEL:
            return finish("Cancelled. Nothing was added to any cart.")
        if low in ("bill", "done", "checkout", "pay"):
            if not basket:
                return {"error": "The basket is empty; add an item first.", "route": "ask_more"}
            return {"quotes": None, "error": None, "route": "prepare"}
        m = re.fullmatch(r"(?:remove|rm|del|delete|-)\s*(\d+)", low)
        if m:
            k = int(m.group(1))
            if not 1 <= k <= len(basket):
                return {"error": f"There's no line {k} in the basket.", "route": "ask_more"}
            return {"basket": basket[:k - 1] + basket[k:], "error": None, "route": "ask_more"}
        items = split_request(answer)
        if not items:
            return {"error": "Type an item, bill, remove N, or cancel.", "route": "ask_more"}
        return {"queue": items, "error": None, "route": "next_item"}

    # ------------------------------------------------------------ plan + budget

    async def plan(state: GroceryState) -> dict:
        request = state["request"]
        try:
            p: GroceryPlan = await structured(cfg, GroceryPlan).ainvoke(
                [("system", PLAN_PROMPT), ("human", request)])
        except Exception as e:  # noqa: BLE001
            log(f"(Couldn't plan with the LLM: {describe_error(e)}. Searching '{request}' as typed.)")
            p = fallback_plan(request)
        p.search_terms = [t.strip() for t in p.search_terms if t and t.strip()][:3] or [request]

        log(f"Searching for: {', '.join(repr(t) for t in p.search_terms)}")
        limits = []
        if p.max_kcal is not None:
            limits.append(f"≤ {p.max_kcal:g} kcal")
        if p.max_sugar_g is not None:
            limits.append(f"≤ {p.max_sugar_g:g} g sugar")
        if p.min_protein_g is not None:
            limits.append(f"≥ {p.min_protein_g:g} g protein")
        if limits:
            log(f"'{p.qualifier}' means, per 100 g/ml: {', '.join(limits)}" + (f"  ({p.notes})" if p.notes else ""))

        budget = state.get("budget")
        if budget is None and p.budget:                     # 'under 300' in the first request
            budget = p.budget
            log(f"Budget from your request: ₹{budget}")
        return {"plan": p.model_dump(mode="json"), "budget": budget, "error": None,
                "route": "search" if budget is not None else "ask_budget"}

    def ask_budget(state: GroceryState) -> dict:
        answer = str(interrupt({"prompt": with_error(state, "Budget in ₹? (e.g. 300; Enter = no limit; cancel)"),
                                "options": ["no limit", "100", "200", "300", "500", "cancel"]})).strip().lower()
        if answer in CANCEL:
            return finish("Cancelled.")
        if answer in ("", "no", "none", "any", "no limit", "0"):
            return {"budget": 0, "error": None, "route": "search"}
        m = re.fullmatch(r"(?:₹|rs\.?|inr)?\s*(\d+)\s*(?:₹|rs|rupees)?", answer)
        if not m or int(m.group(1)) <= 0:
            return {"error": f"'{answer}' isn't an amount like 300.", "route": "ask_budget"}
        return {"budget": int(m.group(1)), "error": None, "route": "search"}

    # ------------------------------------------------------------ search, nutrition, matching

    async def amazon_ready(place: dict) -> bool:
        """Amazon Now can't switch addresses yet, so it's used only if its current address has the
        same pincode as this place."""
        amazon = adapters["amazon_now"]
        try:
            current = await amazon.current_address()
            pins = {find_pincode(t) for t in (place.get("texts") or {}).values()} - {None}
            if pins and current.pincode not in pins:
                log(f"  Amazon Now is set to {current.text} ({current.pincode}), not {place['name']} "
                    f"({', '.join(sorted(pins))}). Change it at the top of Amazon Now to include it; left out.")
                return False
            await amazon.set_address(current.address_id)
            log(f"  Amazon Now delivers to {current.label} ({current.pincode}).")
            return True
        except Exception as e:  # noqa: BLE001
            log(f"  Amazon Now left out: {describe_error(e)}")
            return False

    async def search_app(app: str, terms: list[str]) -> list[Product]:
        found: dict[str, Product] = {}
        if app == "zepto":
            sections = await adapters["zepto"].search_many(terms)
            ordered = [sections.get(t, []) for t in terms] + [ps for t, ps in sections.items() if t not in terms]
            for products in ordered:                             # keep the order of your terms
                for p in products:
                    found.setdefault(p.product_id, p)
        else:
            for k, term in enumerate(terms):                     # 1st term: all pages; others: 1 page
                for p in await adapters[app].search(term, pages=None if k == 0 else 1):
                    found.setdefault(p.product_id, p)
        return list(found.values())

    async def read_labels(products: list[Product], plan_: GroceryPlan) -> list[Product]:
        """Zepto nutrition labels for up to details_limit products, pack claims first."""
        def has_claim(p: Product) -> bool:
            return any(c.lower() in clean_text_for_claims(p.name) for c in plan_.claim_words)
        order = sorted(range(len(products)), key=lambda i: (not has_claim(products[i]), i))
        chosen = order[:gs.details_limit]
        sem = asyncio.Semaphore(5)

        async def one(i: int) -> tuple[int, Optional[Product]]:
            async with sem:
                try:
                    return i, await adapters["zepto"].details(products[i])
                except Exception:  # noqa: BLE001  one bad product doesn't stop the rest
                    return i, None

        out = list(products)
        for i, detailed in await asyncio.gather(*(one(i) for i in chosen)):
            if detailed is not None:
                out[i] = detailed
        labelled = sum(1 for i in chosen if out[i].nutrition)
        log(f"  Zepto nutrition labels: read {len(chosen)}, {labelled} had numbers.")
        return out

    async def search(state: GroceryState) -> dict:
        plan_ = GroceryPlan.model_validate(state["plan"])
        place = state["place"]
        budget = state.get("budget") or 0
        left = round(budget - basket_spent(state.get("basket") or []), 2) if budget else 0

        def nothing(msg: str) -> dict:
            """In a basket, one item finding nothing just moves on."""
            if state.get("basket") or state.get("queue"):
                log(f"  {msg} Skipped.")
                return after_item(state)
            return finish(msg)

        ready = []
        for app in state["apps"]:
            if app == "zepto":
                try:
                    await adapters["zepto"].set_address(place["addresses"]["zepto"])
                    ready.append(app)
                except Exception as e:  # noqa: BLE001
                    log(f"  Zepto left out: {describe_error(e)}")
            elif app == "amazon_now" and await amazon_ready(place):
                ready.append(app)
        if not ready:
            return finish("Neither Zepto nor Amazon Now can be used here right now.")

        log(f"Searching {' and '.join(label(a) for a in ready)}...")
        results = await asyncio.gather(*(search_app(a, plan_.search_terms) for a in ready), return_exceptions=True)
        by_app: dict[str, list[Product]] = {}
        for app, res in zip(ready, results):
            if isinstance(res, BaseException):
                log(f"  {label(app)} search failed: {describe_error(res)}")
                continue
            kept = relevant(res, plan_.category_words, plan_.exclude_words, plan_.item)
            log(f"  {label(app)}: {len(res)} products, {len(kept)} look like {plan_.item}")
            if kept:
                by_app[app] = kept
        if not by_app:
            return nothing(f"Nothing found for '{state['request']}'.")

        if plan_.health_mode and "zepto" in by_app:
            by_app["zepto"] = await read_labels(by_app["zepto"], plan_)
            labels = [p.nutrition for p in by_app["zepto"] if p.nutrition]
            for note in relax_limits(plan_, labels):
                log(f"  Dropped the limit {note}.")

        groups = build_groups(by_app, plan_, gs.product_match_threshold, gs.pack_tolerance,
                              dict(w_kcal=gs.w_kcal, w_sugar=gs.w_sugar, w_protein=gs.w_protein))
        if budget and left <= 0:
            log(f"  (Your ₹{budget} budget is already used by the basket; showing everything.)")
        groups, hidden = within_budget(groups, left if budget and left > 0 else 0)
        for k, g in enumerate(groups, 1):
            g.gid = str(k)
        both = sum(1 for g in groups if len(g.items) > 1)
        log(f"  {len(groups)} products, {both} on both apps"
            + (f" ({hidden} over the ₹{left:g} left in your budget hidden)" if hidden else "") + ".")
        if not groups:
            return nothing(f"Nothing for '{state['request']}' within ₹{left:g}.")
        return {"apps": ready, "plan": plan_.model_dump(mode="json"),     # limits may have been relaxed
                "groups": [g.model_dump(mode="json") for g in groups],
                "view": None, "error": None, "route": "ask_products"}

    # ------------------------------------------------------------ pick

    def ask_products(state: GroceryState) -> dict:
        groups = [ProductGroup.model_validate(d) for d in state["groups"]]
        plan_ = GroceryPlan.model_validate(state["plan"])
        apps = state["apps"]
        view = groups_view(groups, state.get("view"), gs.show)
        filt = state.get("view")
        head = (f"{state['request']}: showing {len(view)} of {len(groups)}"
                + (f" matching '{filt}'" if filt and filt != "*" else ""))
        if plan_.health_mode:
            limits = ", ".join(x for x in [
                f"≤{plan_.max_kcal:g} kcal" if plan_.max_kcal is not None else "",
                f"≤{plan_.max_sugar_g:g} g sugar" if plan_.max_sugar_g is not None else "",
                f"≥{plan_.min_protein_g:g} g protein" if plan_.min_protein_g is not None else ""] if x)
            head += (f"  ·  per 100 g/ml{': ' + limits if limits else ''}"
                     "\n✓ within limits  ✗ outside  ? no label (claim = pack says it)"
                     "  ~ = label of another size of the same product"
                     "\nG = 0.4·(1−kcal/max) + 0.4·(1−sugar/max) + 0.2·(protein/max)")
        head += "\n* = cheaper app for that product.  ₹/100 = cheapest price per 100 g/ml"
        prompt = with_error(state, head + "\n" + groups_table(view, apps, plan_.health_mode)
                            + "\nType the numbers you want, x for more than one:   6x3, 9"
                            + "\n/word = search this list (/mango)   / = top again   /* = everything"
                            + "\nskip = nothing from this list   cancel = stop")
        answer = str(interrupt({"prompt": prompt, "options": ["skip", "cancel"]})).strip()
        low = answer.lower()
        if low in CANCEL:
            return finish("Cancelled. Nothing was added to any cart.")
        if low == "skip":
            return after_item(state)
        if answer.startswith("/"):
            word = answer[1:].strip()
            return {"view": word or None, "error": None, "route": "ask_products"}
        try:
            picks = parse_picks(answer, len(groups), gs.max_quantity)
        except ValueError as e:
            return {"error": str(e), "route": "ask_products"}
        by_gid = {g.gid: g for g in groups}
        chosen = [(by_gid[str(n)], q) for n, q in picks]
        basket = add_to_basket(state.get("basket") or [], chosen, state["request"], gs.max_quantity)
        log("Added to basket: " + ", ".join(f"{q} x {g.name}" for g, q in chosen))
        return after_item(state, {"basket": basket, "quotes": None})

    # ------------------------------------------------------------ carts + bills

    def picked(state: GroceryState) -> list[tuple[ProductGroup, int]]:
        return basket_picks(state.get("basket") or [])

    async def prepare(state: GroceryState) -> dict:
        """Amazon's cart is your real one: note what's in it before the agent adds anything,
        so your picks can be taken back out later, and your other items can be set aside while
        comparing and put back afterwards."""
        log("Your basket (listed prices, before fees):\n"
            + selection_table(picked(state), state["apps"], state.get("budget") or None))
        if "amazon_now" not in state["apps"] or "amazon_now" not in adapters:
            return {"amazon_before": None, "amazon_aside": [], "amazon_replace": False, "route": "quote"}
        items, _ = picks_for_app(picked(state), "amazon_now")
        if not items:
            return {"amazon_before": None, "amazon_aside": [], "amazon_replace": False, "route": "quote"}
        amazon = adapters["amazon_now"]
        asins = [p.product_id for p, _ in items]
        try:
            before = await amazon.quantities(asins)
            aside = await amazon.other_items_raw(asins)
        except Exception as e:  # noqa: BLE001
            log(f"  Couldn't read your Amazon Now cart: {describe_error(e)}")
            return {"amazon_before": None, "amazon_aside": [], "amazon_replace": False, "route": "quote"}
        return {"amazon_before": before, "amazon_aside": aside, "amazon_replace": False,
                "route": "ask_leftovers" if aside else "quote"}

    def ask_leftovers(state: GroceryState) -> dict:
        lines = [f"   {x['quantity']} x {x['title']}" + (f"  (₹{x['price']:g})" if x.get("price") else "")
                 for x in state["amazon_aside"]]
        prompt = with_error(state, "Your Amazon Now cart already has:\n" + "\n".join(lines)
                            + "\nSet those aside while comparing, so the bill shows only your basket?"
                            + "\nThey go back into your cart afterwards, whatever you choose. (Y/n, cancel)")
        options = [{"label": "Yes, set aside", "value": "y"}, {"label": "No, keep them in", "value": "n"}, "cancel"]
        answer = str(interrupt({"prompt": prompt, "options": options})).strip().lower()
        if answer in CANCEL:
            return finish("Cancelled. Nothing was added to any cart.")
        return {"amazon_replace": answer not in ("n", "no"), "error": None, "route": "quote"}

    async def quote(state: GroceryState) -> dict:
        picks = picked(state)
        apps = [a for a in state["apps"] if a in adapters]   # after a restart Amazon may be missing
        if "zepto" in apps:
            # a resumed chat has a fresh Zepto session with no store chosen yet (same address = no-op)
            try:
                await adapters["zepto"].set_address(state["place"]["addresses"]["zepto"])
            except Exception as e:  # noqa: BLE001
                log(f"  Zepto left out: {describe_error(e)}")
                apps.remove("zepto")
        if not apps:
            return {"quotes": [], "after": "end", "route": "cleanup"}
        log("Putting your picks in " + " and ".join(label(a) for a in apps) + " carts to get the real bills...")
        raw: dict[str, Any] = {}

        async def one(app: str) -> dict[str, Any]:
            items, missing = picks_for_app(picks, app)
            if not items:
                return {"app": app, "cart": None, "missing": missing, "note": "none of your picks are here"}
            adapter = adapters[app]
            try:
                if app == "amazon_now":
                    added = await adapter.add_many(items, replace=bool(state.get("amazon_replace")))
                else:
                    added = await adapter.add_many(items, replace=True)
                bill = await adapter.preview_order()
            except Exception as e:  # noqa: BLE001
                return {"app": app, "cart": None, "missing": missing, "note": f"couldn't build cart: {describe_error(e)}"}
            raw[app] = bill.raw
            note = added.message if not added.ok else ""
            if bill.message and not bill.message.startswith("("):
                note = "; ".join(x for x in [note, bill.message] if x)
            elif bill.message:
                note = "; ".join(x for x in [note, bill.message.strip("()")] if x)
            others_value = 0.0
            if app == "amazon_now" and not state.get("amazon_replace"):
                mine = {p.product_id for p, _ in items}
                others_value = round(sum(money(it.get("totalOfferPrice")) or 0.0
                                         for it in (bill.raw.get("cartItems") or [])
                                         if it.get("asin") not in mine), 2)
            return {"app": app, "cart": bill.model_dump(mode="json"), "missing": missing,
                    "note": note, "others_value": others_value}

        quotes = list(await asyncio.gather(*(one(a) for a in apps)))
        save_debug("grocery_quotes.json", raw)       # the apps' raw bills, to check the fee parsing
        if not any(q.get("cart") and q["cart"].get("total") is not None for q in quotes):
            for q in quotes:
                log(f"  {label(q['app'])}: {q.get('note')}")
            return {"quotes": quotes, "after": "end", "route": "cleanup"}
        return {"quotes": quotes, "chosen": None, "error": None, "route": "ask_app"}

    def ask_app(state: GroceryState) -> dict:
        quotes = state["quotes"]
        best = best_quote(quotes)
        tip = ""
        if best is not None:
            q = quotes[best]
            tip = (f"\nCheapest{' with everything' if not q.get('missing') else ''}: "
                   f"{best + 1}. {label(q['app'])} at ₹{compare_total(q):g}")
        prompt = with_error(state, "Real bills from each app:\n"
                            + quotes_table(quotes, state.get("budget") or None) + tip
                            + "\nPick an app (number or name), back = change the basket, cancel:")
        options = [{"label": f"{k}. {label(q['app'])}" + (f" ₹{q['cart']['total']:g}" if (q.get("cart") or {}).get("total") is not None else ""),
                    "value": str(k)} for k, q in enumerate(quotes, 1)] + ["back", "cancel"]
        answer = str(interrupt({"prompt": prompt, "options": options})).strip().lower()
        if answer in CANCEL:
            return {"after": "cancel", "route": "cleanup"}
        if answer == "back":
            return {"after": "ask_more", "route": "cleanup"}
        index = None
        if answer.isdigit() and 1 <= int(answer) <= len(quotes):
            index = int(answer) - 1
        else:
            index = next((i for i, q in enumerate(quotes)
                          if answer in (label(q["app"]).lower(), q["app"], label(q["app"]).lower().split()[0])), None)
        if index is None:
            return {"error": f"'{answer}' isn't one of the options.", "route": "ask_app"}
        cart = quotes[index].get("cart") or {}
        if cart.get("total") is None:
            return {"error": f"{label(quotes[index]['app'])} has no bill for this order.", "route": "ask_app"}
        if cart["total"] > cfg.ordering.max_order_total:
            return {"error": f"₹{cart['total']:g} is above your cap of ₹{cfg.ordering.max_order_total:g} "
                             "(config.yaml ordering.max_order_total).", "route": "ask_app"}
        return {"chosen": index, "error": None, "route": "ask_confirm"}

    def ask_confirm(state: GroceryState) -> dict:
        q = state["quotes"][state["chosen"]]
        app, total = q["app"], q["cart"]["total"]
        missing = f"\n(⚠ not included: {', '.join(q['missing'])})" if q.get("missing") else ""
        if app == "amazon_now":
            text = (f"Amazon Now: TO PAY ₹{total:g}, delivered to {state['place']['name']}.{missing}"
                    "\nType 'open' to open your Amazon Now cart in the Brave window; you press "
                    "'Proceed to pay' there yourself. Or back / cancel:")
        else:
            text = (f"{label(app)}: TO PAY ₹{total:g}, delivered to {state['place']['name']}.{missing}"
                    f"\nType '{confirm_phrase(total)}' to place a UPI order, back, or cancel:")
        # Zepto: no 'confirm' button on purpose, the exact phrase must be typed
        options = ["open", "back", "cancel"] if app == "amazon_now" else ["back", "cancel"]
        question = {"prompt": with_error(state, text), "options": options}
        if app != "amazon_now":
            question["typed"] = confirm_phrase(total)
        answer = str(interrupt(question)).strip()
        low = answer.lower()
        if low in CANCEL:
            return {"after": "cancel", "route": "cleanup"}
        if low == "back":
            return {"error": None, "route": "ask_app"}
        if app == "amazon_now":
            if low not in ("open", "y", "yes"):
                return {"error": "Type open, back or cancel.", "route": "ask_confirm"}
        else:
            try:
                approve_order(cfg, app, total, answer)        # checked here, issued again when placing
            except ApprovalError as e:
                return {"error": str(e), "route": "ask_confirm"}
        return {"typed_confirm": answer, "error": None, "route": "place_order"}

    async def put_aside_back(state: GroceryState) -> None:
        """Re-adds the items you let the agent set aside while comparing."""
        aside = state.get("amazon_aside") or []
        if not (state.get("amazon_replace") and aside):
            return
        try:
            failed = await adapters["amazon_now"].put_back(aside)
            back = len(aside) - len(failed)
            if back:
                log(f"  Put your {back} set-aside item(s) back in the Amazon Now cart.")
            if failed:
                log(f"  Couldn't put back: {', '.join(failed)} (add them again in the app).")
        except Exception as e:  # noqa: BLE001
            log(f"  Couldn't put your set-aside items back ({describe_error(e)}): "
                + ", ".join(f"{x['quantity']} x {x['title']}" for x in aside))

    async def undo_amazon(state: GroceryState) -> None:
        """Puts your Amazon Now cart back to how it was before the agent: picks out (or back to
        their old quantities), set-aside items back in."""
        before = state.get("amazon_before")
        quoted = any(q["app"] == "amazon_now" and q.get("cart") for q in state.get("quotes") or [])
        if before and quoted:
            try:
                await adapters["amazon_now"].set_quantities(before)
                log("  Took your picks back out of the Amazon Now cart.")
            except Exception as e:  # noqa: BLE001
                log(f"  Couldn't undo the Amazon Now cart ({describe_error(e)}); check it in the app.")
        await put_aside_back(state)

    async def cleanup(state: GroceryState) -> dict:
        await undo_amazon(state)
        after = state.get("after")
        if after == "ask_more":
            return {"quotes": None, "chosen": None, "amazon_before": None, "amazon_aside": [],
                    "amazon_replace": False, "error": None, "route": "ask_more"}
        if after == "cancel":
            return finish("Cancelled. Nothing was ordered.")
        return finish("No app could make a bill for these picks. Nothing was ordered.")

    async def place_order(state: GroceryState) -> dict:
        q = state["quotes"][state["chosen"]]
        app, total = q["app"], q["cart"]["total"]
        adapter = adapters[app]
        if app != "amazon_now":
            await undo_amazon(state)                 # you chose Zepto: Amazon's cart goes back as it was

        try:
            if app == "amazon_now":
                approval = OrderApproval(server="amazon_now", amount=total)   # nothing is charged by the agent
            else:
                approval = approve_order(cfg, app, total, state["typed_confirm"])
            payment = await adapter.place_order(approval)
        except Exception as e:  # noqa: BLE001
            return finish(f"Order not placed: {describe_error(e)}")

        if app == "amazon_now":
            log(f"Opened your Amazon Now cart (₹{payment.amount or total:g}) in the Brave window. "
                "Press 'Proceed to pay' there and pay as usual.")
        elif not show_payment(payment, log):
            await adapter.finalize(payment, "PENDING")
            return {**finish("Stopped: the app asked for more than you approved. Not paid."),
                    "payment": payment.model_dump(mode="json")}

        log(f"Waiting for your payment (checking every {cfg.ordering.payment_poll_seconds} s, "
            f"up to {cfg.ordering.payment_timeout_seconds // 60} min)...")
        final, timed_out = await wait_for_payment(adapter, payment, cfg, log)
        if app == "amazon_now":
            await put_aside_back(state)              # your other items go back, paid or not
        if final == "PAID":
            msg = f"Paid. {label(app)} order is placed."
        elif app == "amazon_now":
            msg = ("No new Amazon Now order seen yet. Your picks stay in your Amazon Now cart "
                   "(with any items that were set aside); pay from the app or site whenever you like.")
        elif timed_out:
            msg = f"Not paid in time, so the {label(app)} order lapsed. Nothing was charged."
        else:
            msg = f"Payment {final.lower()}. Nothing was charged unless your UPI app says otherwise."
        return {**finish(msg), "payment": payment.model_dump(mode="json")}

    # ------------------------------------------------------------ wiring

    g = StateGraph(GroceryState)
    nodes: dict[str, Any] = {
        "find_place": find_place, "ask_place": ask_place, "next_item": next_item, "ask_more": ask_more,
        "plan": plan, "ask_budget": ask_budget,
        "search": search, "ask_products": ask_products, "prepare": prepare, "ask_leftovers": ask_leftovers,
        "quote": quote, "ask_app": ask_app, "ask_confirm": ask_confirm, "place_order": place_order,
        "cleanup": cleanup,
    }
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.add_edge(START, "find_place")
    targets = list(nodes) + [END]
    for name in nodes:
        g.add_conditional_edges(name, lambda s: s.get("route", END), targets)
    return g