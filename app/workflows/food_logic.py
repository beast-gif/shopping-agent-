"""Pure functions behind the food workflow: no network, no LLM, easy to check by hand.

1. Same restaurant on both apps
     name_score = token_set_ratio(name_a, name_b)          (0-100; a name contained in the
                                                           other scores 100)
     same  ⇔  name_score ≥ 85  and  |distance_a − distance_b| ≤ 1.0 km
   Both distances are measured from the same place, so a real match has nearly equal ones.

2. Restaurant ranking
     R = 0.5 · (rating / 5) + 0.3 · (1 − eta / eta_max) + 0.2 · dish_match
     rating = mean over apps; eta = fastest app; dish_match = 1 if a matching dish was found
     at that restaurant, else 0.5. Ads get no boost.

3. Same dish on both menus
     token_sort_ratio(name_a, name_b) ≥ 90 and veg flags don't contradict.
   (token_sort, not token_set: 'Chicken Biryani' must NOT match 'Special Chicken Biryani'.)

4. Your selection: '3x2, 7'  ->  row 3 × 2, row 7 × 1
"""
from __future__ import annotations

import re
from statistics import mean
from typing import Optional

from rapidfuzz import fuzz

from app.schemas import CartResult, MenuItem, MenuRow, Outlet, RestaurantListing

APP_LABEL = {"swiggy": "Swiggy", "zomato": "Zomato"}


def _clean(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", name.lower()).strip()


def rs(v: Optional[float]) -> str:
    return "—" if v is None else f"₹{v:,.0f}" if float(v).is_integer() else f"₹{v:,.2f}"


# ---------------------------------------------------------------- 1. outlets


def same_restaurant_score(a: RestaurantListing, b: RestaurantListing,
                          name_threshold: float, max_km_diff: float) -> Optional[float]:
    score = fuzz.token_set_ratio(_clean(a.name), _clean(b.name))
    if score < name_threshold:
        return None
    if a.distance_km is not None and b.distance_km is not None:
        if abs(a.distance_km - b.distance_km) > max_km_diff:
            return None
    elif score < 95:                     # no distances to cross-check: demand a near-identical name
        return None
    return score


def merge_outlets(listings: dict[str, list[RestaurantListing]],
                  name_threshold: float = 85, max_km_diff: float = 1.0) -> list[Outlet]:
    """{'swiggy': [...], 'zomato': [...]} -> one Outlet per physical restaurant.
    Best-scoring pairs are matched first, one listing per app per outlet."""
    outlets: list[Outlet] = []
    for app, items in listings.items():
        pairs = []
        for j, listing in enumerate(items):
            for i, outlet in enumerate(outlets):
                if app in outlet.listings:
                    continue
                scores = [same_restaurant_score(listing, other, name_threshold, max_km_diff)
                          for other in outlet.listings.values()]
                if all(s is not None for s in scores):
                    pairs.append((min(scores), i, j))
        pairs.sort(reverse=True)
        used_outlets, used_items = set(), set()
        for _, i, j in pairs:
            if i in used_outlets or j in used_items:
                continue
            outlets[i].listings[app] = items[j]
            used_outlets.add(i)
            used_items.add(j)
        for j, listing in enumerate(items):
            if j not in used_items:
                outlets.append(Outlet(outlet_id=f"{app}:{listing.restaurant_id}", name=listing.name,
                                      listings={app: listing}))
    for o in outlets:
        o.outlet_id = "+".join(f"{app}:{l.restaurant_id}" for app, l in sorted(o.listings.items()))
        o.name = min((l.name for l in o.listings.values()), key=len)   # shortest = least decorated
    return outlets


# ---------------------------------------------------------------- 2. ranking


def outlet_rating(o: Outlet) -> Optional[float]:
    ratings = [l.rating for l in o.listings.values() if l.rating]
    return round(mean(ratings), 2) if ratings else None


def outlet_eta(o: Outlet) -> Optional[float]:
    etas = [l.eta_min for l in o.listings.values() if l.eta_min]
    return min(etas) if etas else None


def rank_outlets(outlets: list[Outlet], dish_restaurants: set[str], default_rating: float = 3.5,
                 w_rating: float = 0.5, w_eta: float = 0.3, w_dish: float = 0.2) -> list[Outlet]:
    """dish_restaurants = {'swiggy:692511', 'zomato:20627764', ...} that had a matching dish."""
    open_outlets = [o for o in outlets if any(l.is_open for l in o.listings.values())]
    etas = [e for e in (outlet_eta(o) for o in open_outlets) if e]
    eta_max = max(etas) if etas else None
    for o in open_outlets:
        rating = outlet_rating(o) or default_rating
        eta = outlet_eta(o)
        eta_part = (1 - eta / eta_max) if (eta and eta_max) else 0.5
        has_dish = any(f"{app}:{l.restaurant_id}" in dish_restaurants for app, l in o.listings.items())
        o.score = round(w_rating * rating / 5 + w_eta * eta_part + w_dish * (1.0 if has_dish else 0.5), 3)
    return sorted(open_outlets, key=lambda o: o.score or 0, reverse=True)


def _contains_words(words: list[str], text: str) -> bool:
    """Every typed word appears in `text` (exactly, or ≥ 85% alike for 4+ letter words)."""
    have = _clean(text).split()
    return all(any(w == n or (len(w) >= 4 and fuzz.ratio(w, n) >= 85) for n in have) for w in words)


def outlets_named_like(request: str, outlets: list[Outlet]) -> list[Outlet]:
    """Outlets whose name (on any app) contains every word you typed."""
    words = _clean(request).split()
    if not words:
        return []
    return [o for o in outlets if any(_contains_words(words, l.name) for l in o.listings.values())]


def restaurant_name_matches(request: str, outlets: list[Outlet],
                            dishes: list[MenuItem]) -> tuple[list[Outlet], str]:
    """Is what you typed a restaurant name or a dish? Returns (matching outlets, reason).

    Restaurant  ⇔  (1) some restaurant names contain the word, and
                   (2) dishes containing the word come from at most 1 restaurant whose
                       name DOESN'T contain it.
      'biryani'  -> biryani dishes at Nandhana Palace, Andhra Gunpowder, ITC ...  -> dish
      'nandhana' -> 'Nandhana Special …' dishes only at Nandhana Palace           -> restaurant
    """
    words = _clean(request).split()
    if not words or len(" ".join(words)) < 3:
        return [], "too short to be a name"
    hits = outlets_named_like(request, outlets)
    if not hits:
        return [], "no restaurant name contains it"

    elsewhere = {
        (d.platform, d.restaurant_id) for d in dishes
        if _contains_words(words, d.name) and not _contains_words(words, d.restaurant_name or "")
    }
    if len(elsewhere) >= 2:
        return [], (f"{len(hits)} names contain it, but dishes with it are sold at "
                    f"{len(elsewhere)} other restaurants, so it's a dish")
    return hits, f"{len(hits)} restaurant(s) are named like it and it isn't a dish elsewhere"


def outlets_table(outlets: list[Outlet]) -> str:
    lines = [f"  {'#':>2}  {'Restaurant':34} {'★':>4} {'ETA':>7} {'km':>5}  {'Apps / offers'}"]
    name_count: dict[str, int] = {}
    for o in outlets:
        name_count[_clean(o.name)] = name_count.get(_clean(o.name), 0) + 1
    for i, o in enumerate(outlets, 1):
        name = o.name
        if name_count[_clean(o.name)] > 1:          # chain branches: say which one ('Kadugodi')
            area = next((l.area for l in o.listings.values() if l.area), None)
            if area:
                name = f"{o.name} ({area})"
        eta = outlet_eta(o)
        dist = min((l.distance_km for l in o.listings.values() if l.distance_km is not None), default=None)
        apps = []
        for app, l in sorted(o.listings.items()):
            tag = APP_LABEL.get(app, app) + (" (ad)" if l.is_ad else "")
            apps.append(f"{tag}: {l.offer}" if l.offer else tag)
        lines.append(f"  {i:>2}  {name[:34]:34} {outlet_rating(o) or '—':>4} "
                     f"{(f'{eta:.0f} min' if eta else '—'):>7} {(f'{dist:.1f}' if dist is not None else '—'):>5}  "
                     f"{' | '.join(apps)}   [R={o.score}]")
    return "\n".join(lines)


# ---------------------------------------------------------------- 3. menu


def _veg_ok(a: MenuItem, b: MenuItem) -> bool:
    return a.is_veg is None or b.is_veg is None or a.is_veg == b.is_veg


def merge_menu(menus: dict[str, list[MenuItem]], threshold: float = 90) -> list[MenuRow]:
    """{'swiggy': [...], 'zomato': [...]} -> rows with each app's version of the dish."""
    rows: list[MenuRow] = []
    for app, items in menus.items():
        pairs = []
        for j, item in enumerate(items):
            for i, row in enumerate(rows):
                if app in row.items:
                    continue
                other = next(iter(row.items.values()))
                score = fuzz.token_sort_ratio(_clean(item.name), _clean(other.name))
                if score >= threshold and _veg_ok(item, other):
                    pairs.append((score, i, j))
        pairs.sort(reverse=True)
        used_rows, used_items = set(), set()
        for _, i, j in pairs:
            if i in used_rows or j in used_items:
                continue
            rows[i].items[app] = items[j]
            used_rows.add(i)
            used_items.add(j)
        for j, item in enumerate(items):
            if j not in used_items:
                rows.append(MenuRow(row_id="", name=item.name, items={app: item}))
    for n, row in enumerate(rows, 1):
        row.row_id = str(n)
    return rows


def row_orderable(row: MenuRow) -> bool:
    """Can go in a cart on at least one app (in stock, no size choice needed)."""
    return any(it.in_stock and not it.has_variants for it in row.items.values())


def menu_view(rows: list[MenuRow], request: str, filter_text: Optional[str], limit: int) -> list[MenuRow]:
    """With a /filter: rows whose name contains it. Otherwise rows matching what you asked for
    first, then the rest, up to `limit`."""
    f = _clean(filter_text or "")
    if f:                                   # '=' or other symbols alone clean to '' -> no filter
        return [r for r in rows if f in _clean(r.name)
                or fuzz.partial_ratio(f, _clean(r.name)) >= 85][:max(limit, 60)]
    req = _clean(request)
    matching = [r for r in rows if req and fuzz.partial_ratio(req, _clean(r.name)) >= 85]
    matched_ids = {r.row_id for r in matching}
    rest = [r for r in rows if r.row_id not in matched_ids]
    return (matching + rest)[:limit]


def menu_table(view: list[MenuRow], apps: list[str]) -> str:
    head = f"  {'#':>3}  {'Dish':44}" + "".join(f"{APP_LABEL.get(a, a):>9}" for a in apps) + "   veg  kcal"
    lines = [head]
    for r in view:
        prices = ""
        for a in apps:
            it = r.items.get(a)
            if not it:
                cell = "—"
            elif not it.in_stock:
                cell = "out"
            elif it.has_variants:
                cell = "size?"
            else:
                cell = rs(it.unit_price)
            prices += f"{cell:>9}"
        any_item = next(iter(r.items.values()))
        veg = "V" if any_item.is_veg else ("NV" if any_item.is_veg is False else "")
        kcal = next((f"{it.kcal:.0f}" for it in r.items.values() if it.kcal), "")
        lines.append(f"  {r.row_id:>3}  {r.name[:44]:44}{prices}   {veg:3}  {kcal}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 4. selection


def parse_selection(text: str, rows: list[MenuRow], max_qty: int = 10) -> list[tuple[MenuRow, int]]:
    """'3x2, 7' -> [(row 3, 2), (row 7, 1)]. Raises ValueError with a readable reason."""
    by_id = {r.row_id: r for r in rows}
    picked: dict[str, int] = {}
    for part in re.split(r"[,;]+", text):
        part = part.strip().lower()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*(?:[x×*:]\s*(\d+))?", part)
        if not m:
            raise ValueError(f"'{part}' isn't like 3 or 3x2")
        row_id, qty = m.group(1), int(m.group(2) or 1)
        row = by_id.get(row_id)
        if row is None:
            raise ValueError(f"there's no dish #{row_id}")
        if not row_orderable(row):
            raise ValueError(f"#{row_id} {row.name} needs a size choice or is out of stock (not supported yet)")
        if not 1 <= qty <= max_qty:
            raise ValueError(f"quantity for #{row_id} must be 1-{max_qty}")
        picked[row_id] = picked.get(row_id, 0) + qty
    if not picked:
        raise ValueError("pick at least one dish")
    return [(by_id[r], min(q, max_qty)) for r, q in picked.items()]


def items_for_app(selection: list[tuple[MenuRow, int]], app: str) -> tuple[list[tuple[MenuItem, int]], list[str]]:
    """What can be ordered on `app`, and which picked dishes it doesn't have."""
    items, missing = [], []
    for row, qty in selection:
        it = row.items.get(app)
        if it and it.in_stock and not it.has_variants:
            items.append((it, qty))
        else:
            missing.append(row.name)
    return items, missing


def charge_label(key: str) -> str:
    """'charge_id_delivery_charge' -> 'delivery', 'taxes_and_charges' -> 'taxes and charges'"""
    k = key.lower().replace("charge_id_", "")
    k = re.sub(r"(?<!and)_charges?(?=_discount$|$)", "", k)    # keep 'taxes_and_charges'
    return k.replace("_", " ").strip() or key


def bill_lines(cart: CartResult) -> list[tuple[str, float]]:
    """The app's charges, plus an 'other charges' line for whatever the app adds that we
    can't name, so the lines always add up:  items + Σ charges + other = TO PAY."""
    parts = [(charge_label(k), v) for k, v in cart.charges.items() if v]
    if cart.total is not None and cart.item_total is not None:
        other = round(cart.total - cart.item_total - sum(v for _, v in parts), 2)
        if abs(other) >= 0.5:
            parts.append(("other charges" if other > 0 else "other discount", other))
    return parts


def quotes_table(quotes: list[dict]) -> str:
    """quotes: [{'app', 'cart' (CartResult dict), 'missing': [...], 'note': str}]"""
    lines = []
    for n, q in enumerate(quotes, 1):
        app = APP_LABEL.get(q["app"], q["app"])
        cart = CartResult.model_validate(q["cart"]) if q.get("cart") else None
        if not cart or cart.total is None:
            lines.append(f"  {n}. {app:7} not available: {q.get('note') or 'no cart'}")
            continue
        lines.append(f"  {n}. {app:7} TO PAY {rs(cart.total)}")
        for ln in cart.lines:
            lines.append(f"       {ln.quantity} x {ln.name[:46]:46} {rs(ln.price):>9}")
        lines.append(f"       {'items':50} {rs(cart.item_total):>9}")
        for name, value in bill_lines(cart):
            sign = "−" if value < 0 else "+"
            lines.append(f"       {sign} {name[:48]:48} {rs(abs(value)):>9}")
        if q.get("missing"):
            lines.append(f"       ⚠ missing here: {', '.join(q['missing'])}")
        if q.get("note"):
            lines.append(f"       {q['note']}")
    return "\n".join(lines)


def best_quote(quotes: list[dict]) -> Optional[int]:
    """Index of the cheapest complete quote (all dishes available), else cheapest of any."""
    def total(q):
        return (q.get("cart") or {}).get("total")
    priced = [(i, q) for i, q in enumerate(quotes) if total(q) is not None]
    complete = [(i, q) for i, q in priced if not q.get("missing")]
    pool = complete or priced
    return min(pool, key=lambda p: total(p[1]))[0] if pool else None