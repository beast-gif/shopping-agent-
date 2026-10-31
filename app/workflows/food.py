"""Food workflow (LangGraph). Fixed steps; you choose at every ⏸:

  find_place ─(unknown)─> ask_place ⏸
      │
  search  (Swiggy ∥ Zomato, merge same restaurants, rank by R)
      │  'paradise' matches a restaurant name -> straight to its menu
  ask_restaurant ⏸ ── load_menu ── ask_dishes ⏸  ("3x2, 7", "/paneer", "back")
                                        │
                              quote  (real carts on both apps, best Swiggy coupon)
                                        │
                                  ask_app ⏸ ── ask_confirm ⏸ ("confirm 349")
                                                    │
                                              place_order  (UPI QR, wait, close out)

Every node returns {'route': <next node>} so the flow is explicit. State holds only plain
dicts/lists (pydantic models are dumped), which LangGraph's checkpointer can store safely.
Nodes that ask you (interrupt) do no network calls: LangGraph re-runs them on resume.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from rapidfuzz import fuzz

from app.adapters.base import FoodAdapter
from app.approval import ApprovalError, approve_order, confirm_phrase
from app.config import AppConfig
from app.debug import save_debug
from app.location import detect_place, remember_place
from app.mcp_client import describe_error
from app.payment import show_payment, wait_for_payment
from app.places import Place, load_places, match_place_name
from app.schemas import CartResult, MenuRow, Outlet
from app.workflows.food_logic import (
    APP_LABEL,
    best_quote,
    dish_cards,
    items_for_app,
    menu_table,
    menu_view,
    merge_menu,
    merge_outlets,
    outlets_named_like,
    outlets_table,
    parse_selection,
    quotes_table,
    rank_outlets,
    restaurant_cards,
    restaurant_name_matches,
    rs,
    same_restaurant_score,
)

SERVER_OF = {"swiggy": "swiggy_food", "zomato": "zomato"}
APP_ORDER = ["swiggy", "zomato"]
CANCEL = {"cancel", "q", "quit", "exit", "stop"}


class FoodState(TypedDict, total=False):
    request: str                    # 'biryani' or 'paradise'
    entry: str                      # 'auto' | 'dish' | 'restaurant'
    place_hint: Optional[str]       # 'college' if you said it
    place: Optional[dict]           # {'name', 'how', 'addresses': {app: address_id}}
    apps: list[str]                 # apps with a saved address at this place
    outlets: list[dict]             # ranked Outlet dumps
    outlet: Optional[dict]          # the one you picked
    menu: Optional[list[dict]]      # merged MenuRow dumps
    menu_filter: Optional[str]
    selection: Optional[list[dict]] # [{'row_id': '3', 'qty': 2}]
    quotes: Optional[list[dict]]    # [{'app', 'cart', 'missing', 'note'}]
    chosen: Optional[int]           # index into quotes
    coupon_code: Optional[str]      # 'SWIGGYIT' you typed at the bills
    typed_confirm: Optional[str]
    payment: Optional[dict]
    error: Optional[str]            # shown above the next question, then cleared
    done: Optional[str]             # final message
    route: str


def build_food_graph(cfg: AppConfig, adapters: dict[str, FoodAdapter],
                     log: Callable[[str], None] = print) -> StateGraph:
    fs = cfg.scoring.food

    def finish(msg: str) -> dict:
        return {"done": msg, "route": END}

    def with_error(state: FoodState, text: str) -> str:
        return (f"⚠ {state['error']}\n" if state.get("error") else "") + text

    def label(app: str) -> str:
        return APP_LABEL.get(app, app)

    # ------------------------------------------------------------ place

    def set_place(place: Place, how: str) -> dict:
        apps = [a for a in APP_ORDER if a in adapters and place.address_for(a)]
        remember_place(place)
        log(f"Delivering to {place.name} ({how}).")
        for a in APP_ORDER:
            if a in adapters and a not in apps:
                log(f"  {label(a)} has no saved address at {place.name}, so it's left out.")
        if not apps:
            return finish(f"Neither app has a saved address at {place.name}.")
        return {"place": {"name": place.name, "how": how, "addresses": dict(place.addresses)},
                "apps": apps, "error": None, "route": "search"}

    async def find_place(state: FoodState) -> dict:
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

    def ask_place(state: FoodState) -> dict:
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

    # ------------------------------------------------------------ restaurants

    async def search(state: FoodState) -> dict:
        request, apps = state["request"], state["apps"]
        addresses = state["place"]["addresses"]
        log(f"Searching {' and '.join(label(a) for a in apps)} for '{request}'...")
        results = await asyncio.gather(*(adapters[a].search(addresses[a], request) for a in apps),
                                       return_exceptions=True)
        listings, dish_restaurants, all_dishes = {}, set(), []
        for app, res in zip(apps, results):
            if isinstance(res, BaseException):
                log(f"  {label(app)} search failed: {describe_error(res)}")
                continue
            listings[app] = res.restaurants
            all_dishes.extend(res.dishes)
            dish_restaurants.update(f"{app}:{d.restaurant_id}" for d in res.dishes)
            log(f"  {label(app)}: {len(res.restaurants)} restaurants, {len(res.dishes)} matching dishes")

        outlets = merge_outlets(listings, fs.restaurant_name_threshold, fs.same_restaurant_max_km_diff)
        if not outlets:
            return finish(f"No restaurants found for '{request}' near {state['place']['name']}.")
        both = sum(1 for o in outlets if len(o.listings) > 1)
        log(f"  {len(outlets)} restaurants after merging ({both} are on both apps).")

        weights = dict(w_rating=fs.w_rating, w_eta=fs.w_eta, w_dish=fs.w_dish)
        entry = state.get("entry") or "auto"
        if entry in ("auto", "restaurant"):
            if entry == "restaurant":                      # you said it's a restaurant: trust that
                named = outlets_named_like(request, outlets)
            else:
                named, why = restaurant_name_matches(request, outlets, all_dishes)
                log(f"  '{request}' looks like a {'restaurant' if named else 'dish'}: {why}.")
            if named:
                ranked = rank_outlets(named, dish_restaurants, **weights)[:fs.top_restaurants]
                if not ranked:
                    return finish(f"{named[0].name} is closed right now on every app.")
                dumps = [o.model_dump(mode="json") for o in ranked]
                if len(dumps) == 1:
                    log(f"Found the restaurant: {ranked[0].name}.")
                    return {"outlets": dumps, "outlet": dumps[0], "entry": "restaurant", "route": "load_menu"}
                log(f"'{request}' is a restaurant with {len(dumps)} branches open near you; pick one.")
                return {"outlets": dumps, "outlet": None, "entry": "restaurant", "route": "ask_restaurant"}
            if entry == "restaurant":
                log(f"No restaurant named '{request}' nearby; showing places that serve it instead.")

        ranked = rank_outlets(outlets, dish_restaurants, **weights)[:fs.top_restaurants]
        if not ranked:
            return finish(f"Every restaurant for '{request}' is closed right now.")
        return {"outlets": [o.model_dump(mode="json") for o in ranked], "outlet": None,
                "entry": "dish", "route": "ask_restaurant"}

    def ask_restaurant(state: FoodState) -> dict:
        outlets = [Outlet.model_validate(d) for d in state["outlets"]]
        prompt = with_error(state, "Restaurants (R = 0.5·rating/5 + 0.3·speed + 0.2·has the dish):\n"
                            + outlets_table(outlets) + "\nPick a restaurant number, or cancel:")
        options = [{"label": f"{i}. {o.name}", "value": str(i)} for i, o in enumerate(outlets, 1)] + ["cancel"]
        answer = str(interrupt({"prompt": prompt, "options": options,
                                "cards": restaurant_cards(outlets)})).strip().lower()
        if answer in CANCEL:
            return finish("Cancelled.")
        index = None
        if answer.isdigit() and 1 <= int(answer) <= len(outlets):
            index = int(answer) - 1
        elif len(answer) >= 3:
            # you typed a name ('nandhana palace', 'nandhana'): take it if exactly one restaurant fits
            hits = [i for i, o in enumerate(outlets)
                    if fuzz.partial_ratio(answer, o.name.lower()) >= 85 or fuzz.token_set_ratio(answer, o.name.lower()) >= 85]
            if len(hits) == 1:
                index = hits[0]
            elif len(hits) > 1:
                return {"error": f"'{answer}' fits {len(hits)} restaurants; use the number.", "route": "ask_restaurant"}
        if index is None:
            return {"error": f"'{answer}' isn't a number from 1 to {len(outlets)} or a name in the list.",
                    "route": "ask_restaurant"}
        return {"outlet": state["outlets"][index], "menu": None, "menu_filter": None,
                "selection": None, "error": None, "route": "load_menu"}

    # ------------------------------------------------------------ menu + dishes

    async def find_on_other_apps(outlet: Outlet, addresses: dict[str, str], apps: list[str]) -> Outlet:
        """The restaurant you picked was only in one app's search results. Each app returns its
        own top list, so it may still be on the other app: search that app for its NAME and
        attach the listing if the same-restaurant rule matches (name >= 85 and distances
        within 1 km). One extra search, only for the restaurant you chose."""
        base = next(iter(outlet.listings.values()))
        for app in apps:
            if app in outlet.listings or app not in adapters:
                continue
            try:
                found = await adapters[app].search(addresses[app], outlet.name)
            except Exception as e:  # noqa: BLE001
                log(f"  Couldn't look for {outlet.name} on {label(app)}: {describe_error(e)}")
                continue
            scored = [(same_restaurant_score(base, cand, fs.restaurant_name_threshold,
                                             fs.same_restaurant_max_km_diff), cand)
                      for cand in found.restaurants]
            scored = [(sc, cand) for sc, cand in scored if sc is not None]
            if not scored:
                log(f"  {outlet.name} isn't on {label(app)} near you.")
                continue
            score, match = max(scored, key=lambda x: x[0])
            if not match.is_open:
                log(f"  {outlet.name} is on {label(app)} but closed there right now.")
                continue
            outlet.listings[app] = match
            log(f"  Also on {label(app)}: {match.name}" + (f" ({match.offer})" if match.offer else "")
                + (f", {match.distance_km:g} km" if match.distance_km is not None else ""))
        return outlet

    async def load_menu(state: FoodState) -> dict:
        outlet = Outlet.model_validate(state["outlet"])
        addresses = state["place"]["addresses"]
        if len(outlet.listings) < len([a for a in state["apps"] if a in adapters]):
            outlet = await find_on_other_apps(outlet, addresses, state["apps"])
        apps = [a for a in APP_ORDER if a in outlet.listings and a in state["apps"]]
        log(f"Loading {outlet.name}'s menu from {' and '.join(label(a) for a in apps)}...")
        results = await asyncio.gather(
            *(adapters[a].get_menu(addresses[a], outlet.listings[a].restaurant_id) for a in apps),
            return_exceptions=True)
        menus = {}
        for app, res in zip(apps, results):
            if isinstance(res, BaseException):
                log(f"  {label(app)} menu failed: {describe_error(res)}")
            else:
                menus[app] = res
        rows = merge_menu(menus, fs.menu_item_threshold)
        if not rows:
            return {"outlet": None, "error": f"{outlet.name}'s menu came back empty; pick another.",
                    "route": "ask_restaurant"}
        on_both = sum(1 for r in rows if len(r.items) > 1)
        log(f"  {len(rows)} dishes ({', '.join(f'{label(a)} {len(m)}' for a, m in menus.items())}; "
            f"{on_both} on both).")
        return {"outlet": outlet.model_dump(mode="json"),      # now with the other app's listing, if found
                "menu": [r.model_dump(mode="json") for r in rows], "menu_filter": None,
                "error": None, "route": "ask_dishes"}

    def ask_dishes(state: FoodState) -> dict:
        rows = [MenuRow.model_validate(d) for d in state["menu"]]
        outlet = Outlet.model_validate(state["outlet"])
        apps = [a for a in APP_ORDER if a in outlet.listings and a in state["apps"]]
        filt = state.get("menu_filter")
        view = menu_view(rows, state["request"] if state.get("entry") == "dish" else "", filt, fs.menu_show)
        head = (f"{outlet.name}: showing {len(view)} of {len(rows)} dishes"
                + (f" matching '{filt}'" if filt else "") + "\n")
        prompt = with_error(state, head + menu_table(view, apps)
                            + "\nType the dish numbers you want. Add x and a number for more than one:"
                            + "\n   3        -> 1 of dish 3"
                            + "\n   3x2, 7   -> 2 of dish 3 and 1 of dish 7"
                            + "\nTo search the menu type / and a word, e.g. /paneer   (just / shows everything again)"
                            + "\nback = other restaurants   cancel = stop")
        cards = dish_cards(rows, apps, state["request"] if state.get("entry") == "dish" else "",
                           fs.max_quantity, fs.menu_show)
        answer = str(interrupt({"prompt": prompt, "options": ["back", "cancel"], "cards": cards})).strip()
        low = answer.lower()
        if low in CANCEL:
            return finish("Cancelled.")
        if low == "back":
            return {"outlet": None, "menu": None, "selection": None, "error": None, "route": "ask_restaurant"}
        if answer.startswith("/"):
            word = answer[1:].strip().lstrip("=").strip()      # '/=paneer' works too
            return {"menu_filter": word or None, "error": None, "route": "ask_dishes"}
        try:
            picked = parse_selection(answer, rows, fs.max_quantity)
        except ValueError as e:
            return {"error": str(e), "route": "ask_dishes"}
        return {"selection": [{"row_id": r.row_id, "qty": q} for r, q in picked], "error": None, "route": "quote"}

    # ------------------------------------------------------------ carts

    async def quote(state: FoodState) -> dict:
        rows = {d["row_id"]: MenuRow.model_validate(d) for d in state["menu"]}
        selection = [(rows[s["row_id"]], s["qty"]) for s in state["selection"]]
        outlet = Outlet.model_validate(state["outlet"])
        addresses = state["place"]["addresses"]
        apps = [a for a in APP_ORDER if a in outlet.listings and a in state["apps"]]
        log("Building your cart on " + " and ".join(label(a) for a in apps) + " to get the real bills...")

        raw_carts: dict[str, Any] = {}

        async def one(app: str) -> dict[str, Any]:
            items, missing = items_for_app(selection, app)
            if not items:
                return {"app": app, "cart": None, "missing": missing, "note": "none of your dishes are here"}
            adapter = adapters[app]
            rid = outlet.listings[app].restaurant_id
            try:
                cart = await adapter.add_to_cart(addresses[app], rid, items, replace=True)
                note = ""
                if hasattr(adapter, "apply_best_coupon") and cart.total is not None:
                    cart, note = await adapter.apply_best_coupon(addresses[app], rid, cart)
            except Exception as e:  # noqa: BLE001
                return {"app": app, "cart": None, "missing": missing, "note": f"couldn't build cart: {describe_error(e)}"}
            raw_carts[app] = cart.raw
            return {"app": app, "cart": cart.model_dump(mode="json"), "missing": missing, "note": note}

        quotes = list(await asyncio.gather(*(one(a) for a in apps)))
        save_debug("last_quotes.json", raw_carts)     # the apps' raw bills, to check the fee parsing
        return {"quotes": quotes, "chosen": None, "error": None, "route": "ask_app"}

    def ask_app(state: FoodState) -> dict:
        quotes = state["quotes"]
        best = best_quote(quotes)
        tip = ""
        if best is not None:
            q = quotes[best]
            tip = f"\nCheapest{' complete' if not q.get('missing') else ''}: {best + 1}. {label(q['app'])} " \
                  f"at {rs(q['cart']['total'])}"
        prompt = with_error(state, "Your order, real bills from each app:\n" + quotes_table(quotes) + tip
                            + "\nPick an app (number or name), back = change dishes, cancel"
                            + ("\ncoupon CODE = try a Swiggy code from your app, e.g. coupon SWIGGYIT"
                               if any(q["app"] == "swiggy" and q.get("cart") for q in quotes) else "") + ":")
        options = [{"label": f"{k}. {label(q['app'])}" + (f" {rs(q['cart']['total'])}" if (q.get("cart") or {}).get("total") is not None else ""),
                    "value": str(k)} for k, q in enumerate(quotes, 1)] + ["back", "cancel"]
        raw = str(interrupt({"prompt": prompt, "options": options})).strip()
        answer = raw.lower()
        if answer in CANCEL:
            return finish("Cancelled. Nothing was ordered.")
        if answer == "back":
            return {"selection": None, "quotes": None, "error": None, "route": "ask_dishes"}
        if answer.startswith("coupon"):
            code = raw[6:].strip().upper()
            if not code:
                return {"error": "Type the code after the word coupon, e.g. coupon SWIGGYIT.", "route": "ask_app"}
            if not any(q["app"] == "swiggy" and q.get("cart") for q in quotes):
                return {"error": "There's no Swiggy cart to apply a coupon to.", "route": "ask_app"}
            return {"coupon_code": code, "error": None, "route": "apply_coupon"}
        index = None
        if answer.isdigit() and 1 <= int(answer) <= len(quotes):
            index = int(answer) - 1
        else:
            index = next((i for i, q in enumerate(quotes) if label(q["app"]).lower() == answer), None)
        if index is None:
            return {"error": f"'{answer}' isn't one of the options.", "route": "ask_app"}
        if not quotes[index].get("cart") or quotes[index]["cart"].get("total") is None:
            return {"error": f"{label(quotes[index]['app'])} has no bill for this order.", "route": "ask_app"}
        return {"chosen": index, "error": None, "route": "ask_confirm"}

    async def apply_coupon(state: FoodState) -> dict:
        """'coupon SWIGGYIT': applies it to the Swiggy cart and refreshes Swiggy's bill."""
        code = state["coupon_code"]
        quotes = [dict(q) for q in state["quotes"]]
        i = next(i for i, q in enumerate(quotes) if q["app"] == "swiggy")
        adapter = adapters["swiggy"]
        log(f"Trying coupon {code} on Swiggy...")
        try:
            cart, note = await adapter.apply_coupon(state["place"]["addresses"]["swiggy"], code)
        except Exception as e:  # noqa: BLE001
            return {"error": f"Couldn't apply {code}: {describe_error(e)}", "route": "ask_app"}
        log(f"  {note}")
        quotes[i] = {**quotes[i], "cart": cart.model_dump(mode="json"), "note": note}
        return {"quotes": quotes, "coupon_code": None, "error": None, "route": "ask_app"}

    # ------------------------------------------------------------ confirm + pay

    def ask_confirm(state: FoodState) -> dict:
        q = state["quotes"][state["chosen"]]
        total = q["cart"]["total"]
        if total > cfg.ordering.max_order_total:
            return {"error": f"{rs(total)} is above your cap of {rs(cfg.ordering.max_order_total)} "
                             "(config.yaml ordering.max_order_total).", "route": "ask_app"}
        missing = f"\n(⚠ not included: {', '.join(q['missing'])})" if q.get("missing") else ""
        prompt = with_error(state, f"{label(q['app'])}: TO PAY {rs(total)}, delivered to {state['place']['name']}."
                            + missing + f"\nType '{confirm_phrase(total)}' to place a UPI order, back, or cancel:")
        # no 'confirm' button on purpose: the exact phrase must be typed
        answer = str(interrupt({"prompt": prompt, "options": ["back", "cancel"], "typed": confirm_phrase(total)})).strip()
        low = answer.lower()
        if low in CANCEL:
            return finish("Cancelled. Nothing was ordered.")
        if low == "back":
            return {"error": None, "route": "ask_app"}
        try:
            approve_order(cfg, SERVER_OF[q["app"]], total, answer)     # checked here, issued again at payment
        except ApprovalError as e:
            return {"error": str(e), "route": "ask_confirm"}
        return {"typed_confirm": answer, "error": None, "route": "place_order"}

    async def place_order(state: FoodState) -> dict:
        q = state["quotes"][state["chosen"]]
        app = q["app"]
        adapter = adapters[app]
        cart = CartResult.model_validate(q["cart"])
        address = state["place"]["addresses"][app]
        try:
            approval = approve_order(cfg, SERVER_OF[app], cart.total, state["typed_confirm"])
            payment = await adapter.place_order(address, cart, approval)
        except Exception as e:  # noqa: BLE001
            return finish(f"Order not placed: {describe_error(e)}")

        if not show_payment(payment, log):
            await adapter.finalize(payment, "PENDING")
            return {**finish("Stopped: the app asked for more than you approved. Not paid; order closed."),
                    "payment": payment.model_dump(mode="json")}

        log(f"Waiting for your payment (checking every {cfg.ordering.payment_poll_seconds} s, "
            f"up to {cfg.ordering.payment_timeout_seconds // 60} min)...")
        final, timed_out = await wait_for_payment(adapter, payment, cfg, log)
        if final == "PAID":
            msg = f"Paid. {label(app)} order {payment.order_id} is placed."
        elif timed_out:
            msg = f"Not paid in time, so the {label(app)} order was closed. Nothing was charged."
        else:
            msg = f"Payment {final.lower()}. Nothing was charged unless your UPI app says otherwise."
        return {**finish(msg), "payment": payment.model_dump(mode="json")}

    # ------------------------------------------------------------ wiring

    g = StateGraph(FoodState)
    nodes = {
        "find_place": find_place, "ask_place": ask_place, "search": search,
        "ask_restaurant": ask_restaurant, "load_menu": load_menu, "ask_dishes": ask_dishes,
        "quote": quote, "ask_app": ask_app, "apply_coupon": apply_coupon,
        "ask_confirm": ask_confirm, "place_order": place_order,
    }
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.add_edge(START, "find_place")
    targets = list(nodes) + [END]
    for name in nodes:
        g.add_conditional_edges(name, lambda s: s.get("route", END), targets)
    return g