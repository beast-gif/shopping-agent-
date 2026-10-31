"""Pure helpers for the grocery workflow (no network): the search plan, cross-app matching,
the health score, ranking, tables and parsing your picks.

Matching (same product on Zepto and Amazon Now):
  1. clean both names: lower-case, drop sizes ('500 ml'), marketing phrases ('guilt free',
     'zero added sugar') and packaging words ('cup', 'tub', 'pack'); 'Get- A-Way' -> 'getaway'
  2. name score = rapidfuzz token_sort_ratio(clean_a, clean_b), 0-100
  3. sizes must agree: same unit and |a - b| / max(a, b) <= pack_tolerance (5%)
  4. a pair counts if score >= product_match_threshold (90) and sizes agree; then the
     highest-scoring pairs are taken first, each product used once (best-first).

Health score (only when the request has a health qualifier, e.g. 'guilt-free'):
  G = 0.4·(1 − kcal/kcal_max) + 0.4·(1 − sugar/sugar_max) + 0.2·(protein/protein_max)
  per 100 g/ml; the _max values are the highest among the products found. If a value is
  missing, the remaining terms are re-weighted (divided by the sum of the weights used).

Order shown: passes your limits (by G) -> nutrition unknown (pack claims first) -> fails.
Without a health qualifier ('milk'): cheapest per 100 g/ml first, then rating.
"""
from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from app.schemas import Nutrition, Product

APP_LABEL = {"zepto": "Zepto", "amazon_now": "Amazon Now"}

# ---------------------------------------------------------------- the plan (filled by the LLM)


class GroceryPlan(BaseModel):
    """How to search for one grocery request, and what its qualifier means in numbers."""
    item: str = Field(description="The product in plain words, e.g. 'ice cream', 'milk'")
    qualifier: Optional[str] = Field(None, description="Constraint words the user gave, e.g. 'guilt-free', else null")
    budget: Optional[int] = Field(None, description="Rupees, only if the user stated a budget ('under 300'), else null")
    search_terms: list[str] = Field(description="1-3 short app search queries, most direct first")
    category_words: list[str] = Field(default_factory=list, description=(
        "Lower-case words/phrases found in names of products of this kind, used to drop unrelated "
        "results. ice cream -> ['ice cream','kulfi','cone','pops','sorbet','gelato','frozen dessert','chocobar']"))
    exclude_words: list[str] = Field(default_factory=list, description=(
        "Lower-case words of OTHER products that merely mention this one, e.g. chocolate -> "
        "['ice cream','milkshake','cookies','biscuit','cake','spread','syrup','drink']"))
    claim_words: list[str] = Field(default_factory=list, description=(
        "Lower-case pack claims that signal the qualifier, e.g. guilt-free -> ['guilt free','sugar free',"
        "'no added sugar','zero added sugar','low calorie','high protein']. Empty without a qualifier."))
    max_kcal: Optional[float] = Field(None, description="Upper limit per 100 g/ml, only for health qualifiers")
    max_sugar_g: Optional[float] = Field(None, description="Upper limit per 100 g/ml, only for health qualifiers")
    min_protein_g: Optional[float] = Field(None, description="Lower limit per 100 g/ml, only for health qualifiers")
    notes: Optional[str] = Field(None, description="One short line on why these limits")

    @property
    def health_mode(self) -> bool:
        return any(v is not None for v in (self.max_kcal, self.max_sugar_g, self.min_protein_g)) \
            or bool(self.claim_words)


PLAN_PROMPT = """You plan a grocery search on Indian quick-commerce apps (Zepto, Amazon Now) in Bengaluru.
From the request, fill the fields:
- item, qualifier, budget (budget only if the user stated one).
- search_terms: 1-3 short queries as a shopper would type them. The most direct first
  ("guilt free ice cream"). Add a variant only if it finds different products
  ("sugar free ice cream", "protein ice cream"). A plain request ("milk") gets one term.
- category_words: words that name THIS product in a pack name, plus its synonyms and forms,
  to drop unrelated results. Never the wider family: masala kaju -> ["kaju", "cashew"]
  (not "nuts", "snacks", "masala"); ice cream -> ["ice cream", "kulfi", "cone", "pops",
  "sorbet", "gelato", "frozen dessert", "chocobar"]; chips -> ["chips", "crisps", "wafers"].
- exclude_words: words of OTHER products whose names merely mention this one, so they get
  dropped: chocolate -> ["ice cream", "milkshake", "cookies", "biscuit", "cake", "spread",
  "syrup", "drink"]; milk -> ["chocolate", "biscuit", "powder"]. Empty if nothing obvious.
- claim_words: pack claims that signal the qualifier, including "sugar free", "no added sugar",
  "zero added sugar", "guilt free", "low calorie", "high protein"; empty if no qualifier.
- max_kcal / max_sugar_g / min_protein_g: per 100 g or 100 ml as printed on Indian labels,
  ONLY when the qualifier is about health (guilt-free, low sugar, high protein, diet ...).
  Set only limits THIS category can realistically meet, and leave the others null:
    ice cream, guilt-free   -> max_kcal 150, max_sugar_g 8   (regular: ~200 kcal, ~20 g sugar)
    chocolate, guilt-free / sugar free -> max_sugar_g 5, NO kcal limit (every chocolate,
                               even sugar-free, is 450-600 kcal per 100 g)
    chips / namkeen, healthy -> max_kcal 480                  (regular: ~540 kcal)
    protein bar             -> min_protein_g 20
  Leave all null for non-health qualifiers (dark, family pack, cheap) or no qualifier.
- notes: one short line explaining the limits."""


def fallback_plan(request: str) -> GroceryPlan:
    """Used if the LLM call fails: search the request as typed, no limits."""
    return GroceryPlan(item=request, search_terms=[request])


# ---------------------------------------------------------------- name cleaning + matching

_PHRASES = ["zero added sugar", "no added sugar", "added sugar", "sugar free", "guilt free", "guiltfree",
            "high protein", "low calorie", "low fat"]
_PACKAGING = {"cup", "tub", "pack", "pouch", "bottle", "box", "carton", "jar", "can", "combo", "new",
              "with", "and", "the", "of", "a", "in"}
_SIZE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ml|l|ltr|litre|g|gm|gms|kg|pc|pcs|x)\b")


def clean_name(name: str) -> str:
    """'Go Zero Belgian Dark Chocolate Guilt Free Ice Cream Cup' -> 'go zero belgian dark chocolate ice cream'
    'Get- A-Way Mango Duet Guilt Free Ice Cream Stick 60 ml' -> 'getaway mango duet ice cream stick'"""
    s = name.lower()
    s = re.sub(r"\s*-\s*", "", s)                       # 'get- a-way' / 'guilt-free' -> 'getaway' / 'guiltfree'
    s = _SIZE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    for phrase in _PHRASES:
        s = re.sub(rf"\b{phrase}\b", " ", s)
    return " ".join(w for w in s.split() if w not in _PACKAGING)


def sizes_agree(a: Product, b: Product, tolerance: float) -> Optional[bool]:
    """True/False if both sizes are known; None if either is missing."""
    if not a.pack_size or not b.pack_size:
        return None
    if a.pack_unit != b.pack_unit:
        return False
    return abs(a.pack_size - b.pack_size) / max(a.pack_size, b.pack_size) <= tolerance


def name_score(a: Product, b: Product) -> float:
    return fuzz.token_sort_ratio(clean_name(a.name), clean_name(b.name))


def match_products(left: list[Product], right: list[Product], threshold: float,
                   tolerance: float) -> list[tuple[int, int, float]]:
    """Best-first one-to-one pairs (i_left, j_right, score). A pair needs score >= threshold and
    sizes that agree; with a size missing it needs score >= 97 (near-identical names)."""
    pairs = []
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            score = name_score(a, b)
            if score < threshold:
                continue
            size_ok = sizes_agree(a, b, tolerance)
            if size_ok is False or (size_ok is None and score < 97):
                continue
            pairs.append((score, i, j))
    pairs.sort(reverse=True)
    used_l, used_r, out = set(), set(), []
    for score, i, j in pairs:
        if i not in used_l and j not in used_r:
            used_l.add(i)
            used_r.add(j)
            out.append((i, j, score))
    return out


# ---------------------------------------------------------------- groups (one row per real product)

Verdict = Literal["pass", "fail", "unknown"]


class ProductGroup(BaseModel):
    """One real product, with its listing on each app that has it."""
    gid: str
    name: str
    pack_size: Optional[float] = None
    pack_unit: Optional[str] = None
    items: dict[str, Product] = Field(default_factory=dict)          # app -> listing
    keys: dict[str, dict[str, Any]] = Field(default_factory=dict)    # app -> ids the cart needs later
    match_score: Optional[float] = None
    nutrition: Optional[Nutrition] = None
    nutrition_from: Optional[str] = None      # 'zepto', or 'label of the 100 ml pack' if borrowed
    health: Optional[float] = None                                    # G
    verdict: Verdict = "unknown"
    claims: bool = False                                              # name has a pack claim
    rating: Optional[float] = None

    def price(self, app: str) -> Optional[float]:
        p = self.items.get(app)
        return p.price if p and p.in_stock else None

    def cheapest(self) -> Optional[tuple[str, float]]:
        prices = [(a, self.price(a)) for a in self.items if self.price(a) is not None]
        return min(prices, key=lambda x: x[1]) if prices else None

    def per_100(self) -> Optional[float]:
        best = self.cheapest()
        if not best or not self.pack_size or self.pack_unit not in ("g", "ml"):
            return None
        return round(best[1] / self.pack_size * 100, 2)


def relevant(products: list[Product], category_words: list[str], exclude_words: list[str] = (),
             item: str = "") -> list[Product]:
    """Products whose name has a category word ('kulfi', 'cone' ...) and no exclude word
    ('ice cream' when you asked for chocolate). An exclude word that is part of what you asked
    for ('chocolate ice cream') is ignored. If nothing is left, all are kept (better a noisy
    list than an empty one)."""
    def norm(s: str) -> str:
        return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.lower().replace("-", " "))).strip() + " "

    def has(name: str, w: str) -> bool:
        """Word(s) at a word start, so plurals count: 'cashew' finds 'Cashews', 'cone' finds
        'Cones', but 'cone' doesn't find 'scones'. 'ice cream' also finds 'icecream'."""
        return f" {w}" in name or (" " in w and f" {w.replace(' ', '')}" in name)

    words = [norm(w).strip() for w in category_words if w.strip()]
    asked = norm(item)
    excludes = [norm(w).strip() for w in exclude_words if w.strip() and not has(asked, norm(w).strip())]
    keep = []
    for p in products:
        name = norm(p.name)
        if words and not any(has(name, w) for w in words):
            continue
        if any(has(name, w) for w in excludes):
            continue
        keep.append(p)
    return keep or products


def _keys(p: Product) -> dict[str, Any]:
    """What adding to the cart needs later; Product.raw isn't kept in the workflow state."""
    raw = p.raw or {}
    if p.platform == "amazon_now":
        return {"offerId": raw.get("offerId"), "storeId": raw.get("storeId") or p.store_product_id}
    return {"packSize": raw.get("packSize")}


def dedupe(products: list[Product]) -> list[Product]:
    """One app listing the same product twice (two ids, same name/size/price): keep the first."""
    seen: set[tuple] = set()
    out = []
    for p in products:
        key = (clean_name(p.name), p.pack_size, p.pack_unit, round(p.price, 2))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def relax_limits(plan: GroceryPlan, labels: list[Nutrition], min_labels: int = 3) -> list[str]:
    """Drops a limit that NO labelled product meets (e.g. '<= 150 kcal' for chocolate, where
    every bar is 450-600 kcal): such a limit would just mark everything ✗. Needs at least
    min_labels labels to decide. Returns what was dropped, for the log."""
    dropped = []
    checks = [("max_kcal", "kcal", lambda v, lim: v <= lim, "≤ {:g} kcal"),
              ("max_sugar_g", "sugar_g", lambda v, lim: v <= lim, "≤ {:g} g sugar"),
              ("min_protein_g", "protein_g", lambda v, lim: v >= lim, "≥ {:g} g protein")]
    for field, attr, ok, text in checks:
        limit = getattr(plan, field)
        values = [getattr(n, attr) for n in labels if getattr(n, attr) is not None]
        if limit is None or len(values) < min_labels:
            continue
        if not any(ok(v, limit) for v in values):
            dropped.append(f"{text.format(limit)} (none of {len(values)} labels meet it; "
                           f"they range {min(values):g}-{max(values):g})")
            setattr(plan, field, None)
    return dropped


def judge(n: Optional[Nutrition], plan: GroceryPlan) -> Verdict:
    if n is None or (n.kcal is None and n.sugar_g is None and n.protein_g is None):
        return "unknown"
    checks = []
    if plan.max_kcal is not None and n.kcal is not None:
        checks.append(n.kcal <= plan.max_kcal)
    if plan.max_sugar_g is not None and n.sugar_g is not None:
        checks.append(n.sugar_g <= plan.max_sugar_g)
    if plan.min_protein_g is not None and n.protein_g is not None:
        checks.append(n.protein_g >= plan.min_protein_g)
    if not checks:
        return "unknown"
    return "pass" if all(checks) else "fail"


def health_scores(groups: list[ProductGroup], w_kcal: float, w_sugar: float, w_protein: float) -> None:
    """Fills g.health = G (see the module docstring)."""
    known = [g.nutrition for g in groups if g.nutrition]
    kmax = max((n.kcal for n in known if n.kcal), default=None)
    smax = max((n.sugar_g for n in known if n.sugar_g), default=None)
    pmax = max((n.protein_g for n in known if n.protein_g), default=None)
    for g in groups:
        n = g.nutrition
        if not n:
            continue
        terms = []
        if n.kcal is not None and kmax:
            terms.append((w_kcal, 1 - n.kcal / kmax))
        if n.sugar_g is not None and smax:
            terms.append((w_sugar, 1 - n.sugar_g / smax))
        if n.protein_g is not None and pmax:
            terms.append((w_protein, n.protein_g / pmax))
        if terms:
            g.health = round(sum(w * s for w, s in terms) / sum(w for w, _ in terms), 3)


def build_groups(by_app: dict[str, list[Product]], plan: GroceryPlan, threshold: float, tolerance: float,
                 weights: dict[str, float]) -> list[ProductGroup]:
    """Matches the apps' products into groups, scores and sorts them."""
    by_app = {app: dedupe(products) for app, products in by_app.items()}
    apps = list(by_app)
    groups: list[ProductGroup] = []

    def new_group(listings: dict[str, Product], score: Optional[float] = None) -> ProductGroup:
        first = next(iter(listings.values()))
        nutrition_app = next((a for a, p in listings.items() if p.nutrition), None)
        names = sorted((p.name for p in listings.values()), key=len)
        ratings = [p.rating for p in listings.values() if p.rating is not None]
        return ProductGroup(
            gid=str(len(groups) + 1), name=names[0],
            pack_size=first.pack_size, pack_unit=first.pack_unit,
            items=listings, keys={a: _keys(p) for a, p in listings.items()}, match_score=score,
            nutrition=listings[nutrition_app].nutrition if nutrition_app else None,
            nutrition_from=nutrition_app,
            rating=max(ratings) if ratings else None,
            claims=any(c.lower() in clean_text_for_claims(p.name) for p in listings.values() for c in plan.claim_words),
        )

    if len(apps) == 2:
        a, b = apps
        pairs = match_products(by_app[a], by_app[b], threshold, tolerance)
        used_a = {i for i, _, _ in pairs}
        used_b = {j for _, j, _ in pairs}
        for i, j, score in pairs:
            groups.append(new_group({a: by_app[a][i], b: by_app[b][j]}, score))
        for i, p in enumerate(by_app[a]):
            if i not in used_a:
                groups.append(new_group({a: p}))
        for j, p in enumerate(by_app[b]):
            if j not in used_b:
                groups.append(new_group({b: p}))
    else:
        for app in apps:
            for p in by_app[app]:
                groups.append(new_group({app: p}))

    groups = [g for g in groups if g.cheapest()]                      # drop out-of-stock everywhere
    borrow_labels(groups)
    for g in groups:
        g.verdict = judge(g.nutrition, plan)
    health_scores(groups, **weights)

    if plan.health_mode:
        rank = {"pass": 0, "unknown": 1, "fail": 2}
        groups.sort(key=lambda g: (rank[g.verdict],
                                   -(g.health or 0) if g.verdict != "unknown" else (0 if g.claims else 1),
                                   g.per_100() or 1e9))
    else:
        groups.sort(key=lambda g: (g.per_100() or 1e9, -(g.rating or 0)))
    for k, g in enumerate(groups, 1):
        g.gid = str(k)
    return groups


def borrow_labels(groups: list[ProductGroup], min_score: float = 97) -> None:
    """A product without a nutrition label takes the label of another SIZE of the same product
    (labels are per 100 g/ml, so size doesn't change them). Names must score >= 97, i.e. only the
    size differs: 'cone' vs 'tub' of one flavour (~93) doesn't count, the wafer changes the numbers."""
    labelled = [g for g in groups if g.nutrition]
    for g in groups:
        if g.nutrition:
            continue
        best, best_score = None, 0.0
        for h in labelled:
            if h.pack_unit and g.pack_unit and h.pack_unit != g.pack_unit:
                continue
            score = max(fuzz.token_sort_ratio(clean_name(a.name), clean_name(b.name))
                        for a in g.items.values() for b in h.items.values())
            if score >= min_score and score > best_score:
                best, best_score = h, score
        if best:
            g.nutrition = best.nutrition
            size = f"{best.pack_size:g} {best.pack_unit}" if best.pack_size else "another size"
            g.nutrition_from = f"label of the {size} pack"


def clean_text_for_claims(name: str) -> str:
    """'Guilt-Free' / 'guilt  free' -> 'guilt free' so claim phrases are found."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("-", " ")))


def within_budget(groups: list[ProductGroup], budget: Optional[int]) -> tuple[list[ProductGroup], int]:
    """Groups whose cheapest price fits the budget, and how many were hidden."""
    if not budget:
        return groups, 0
    keep = [g for g in groups if (g.cheapest() or ("", 1e9))[1] <= budget]
    return keep, len(groups) - len(keep)


# ---------------------------------------------------------------- tables


def rs(v: Optional[float]) -> str:
    return "—" if v is None else f"₹{v:g}"


def _size(g: ProductGroup) -> str:
    return f"{g.pack_size:g} {g.pack_unit}" if g.pack_size else "?"


def _num(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.0f}" if v >= 10 else f"{v:.1f}"


def groups_view(groups: list[ProductGroup], word: Optional[str], show: int) -> list[ProductGroup]:
    if word == "*":
        return groups
    if word:
        w = word.lower()
        return [g for g in groups if w in g.name.lower()
                or any(w in p.name.lower() for p in g.items.values())]
    return groups[:show]


def groups_table(groups: list[ProductGroup], apps: list[str], health_mode: bool) -> str:
    head = f"{'#':>3}  {'product':<46} {'size':>8} " + " ".join(f"{APP_LABEL[a]:>10}" for a in apps)
    head += f" {'₹/100':>6}"
    if health_mode:
        head += f" {'kcal':>5} {'sugar':>5} {'prot':>5} {'G':>5}  "
    rows = [head]
    for g in groups:
        prices = {a: g.price(a) for a in apps if g.price(a) is not None}
        low = min(prices.values()) if prices else None
        strictly = len(prices) > 1 and list(prices.values()).count(low) == 1     # no star on a tie
        cells = []
        for a in apps:
            price = g.price(a)
            mark = "*" if strictly and price == low else " "
            cells.append(f"{rs(price) + mark:>10}")
        line = f"{g.gid:>3}  {g.name[:46]:<46} {_size(g):>8} " + " ".join(cells) + f" {_num(g.per_100()):>6}"
        if health_mode:
            n = g.nutrition
            flag = {"pass": "✓", "fail": "✗", "unknown": "?"}[g.verdict]
            line += (f" {_num(n.kcal if n else None):>5} {_num(n.sugar_g if n else None):>5}"
                     f" {_num(n.protein_g if n else None):>5} "
                     f"{(f'{g.health:.2f}' if g.health is not None else '—'):>5} {flag}")
            if g.nutrition_from and g.nutrition_from.startswith("label of"):
                line += " ~"
            if g.claims and g.verdict == "unknown":
                line += " claim"
        rows.append(line)
    return "\n".join(rows)


# ---------------------------------------------------------------- picks


def parse_picks(text: str, count: int, max_qty: int = 10) -> list[tuple[int, int]]:
    """Which listed products you want, and how many of each.

    '6x3, 9'   -> [(6, 3), (9, 1)]      numbers are as shown in the list (1-based)
    '6 9'      -> [(6, 1), (9, 1)]      spaces work too
    '6*2; 6'   -> [(6, 3)]              the same number twice is added up
    Also accepted for 'x': × * :
    Raises ValueError with a readable reason."""
    picked: dict[int, int] = {}
    cleaned = re.sub(r"\s*([x×*:])\s*", r"\1", text.strip().lower())     # '6 x 3' -> '6x3'
    for part in re.split(r"[,;\s]+", cleaned):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:[x×*:](\d+))?", part)
        if not m:
            raise ValueError(f"'{part}' isn't like 6 or 6x3")
        number, qty = int(m.group(1)), int(m.group(2) or 1)
        if not 1 <= number <= count:
            raise ValueError(f"there's no #{number} (the list has 1-{count})")
        if not 1 <= qty <= max_qty:
            raise ValueError(f"quantity for #{number} must be 1-{max_qty}")
        picked[number] = picked.get(number, 0) + qty
    if not picked:
        raise ValueError("pick at least one product")
    return [(n, min(q, max_qty)) for n, q in picked.items()]


def selection_table(picks: list[tuple[ProductGroup, int]], apps: list[str],
                    budget: Optional[int]) -> str:
    """Your picks with each app's item total (listed prices, before fees)."""
    lines = [f"{'#':>3} {'qty':>4}  {'product':<46} {'size':>8} " + " ".join(f"{APP_LABEL[a]:>10}" for a in apps)]
    totals = {a: 0.0 for a in apps}
    missing = {a: [] for a in apps}
    for k, (g, qty) in enumerate(picks, 1):
        cells = []
        for a in apps:
            price = g.price(a)
            if price is None:
                missing[a].append(g.name)
                cells.append(f"{'—':>10}")
            else:
                totals[a] += price * qty
                cells.append(f"{rs(round(price * qty, 2)):>10}")
        lines.append(f"{k:>3} {qty:>4}  {g.name[:46]:<46} {_size(g):>8} " + " ".join(cells))
    lines.append(f"{'':>3} {'':>4}  {'items total (before fees)':<46} {'':>8} "
                 + " ".join(f"{(rs(round(totals[a], 2)) if len(missing[a]) < len(picks) else '—'):>10}"
                            for a in apps))
    for a in apps:
        if missing[a]:
            lines.append(f"   {APP_LABEL[a]} doesn't have: {', '.join(missing[a])}")
    if budget:
        for a in apps:
            if not missing[a] and totals[a] > budget:
                lines.append(f"   ⚠ {APP_LABEL[a]} is ₹{totals[a] - budget:g} over your ₹{budget} budget")
    return "\n".join(lines)


# ---------------------------------------------------------------- basket (several items, one bill)


def split_request(text: str) -> list[str]:
    """'ice cream, chocolates; chips' -> ['ice cream', 'chocolates', 'chips'].
    Only , ; + split ('bread and butter' stays one item)."""
    return [part.strip() for part in re.split(r"[,;+]", text or "") if part.strip()]


def basket_key(g: ProductGroup) -> str:
    """Same listings = same basket line, even if found by two different searches."""
    return "|".join(sorted(f"{a}:{p.product_id}" for a, p in g.items.items()))


def add_to_basket(basket: list[dict[str, Any]], picks: list[tuple[ProductGroup, int]],
                  request: str, max_qty: int) -> list[dict[str, Any]]:
    """Basket lines are {'group': ProductGroup dump, 'qty': n, 'request': 'ice cream'}.
    Picking something already in the basket adds to its quantity (capped at max_qty)."""
    out = [dict(line) for line in basket]
    index = {basket_key(ProductGroup.model_validate(line["group"])): i for i, line in enumerate(out)}
    for g, qty in picks:
        k = basket_key(g)
        if k in index:
            line = out[index[k]]
            line["qty"] = min(max_qty, line["qty"] + qty)
        else:
            index[k] = len(out)
            out.append({"group": g.model_dump(mode="json"), "qty": qty, "request": request})
    return out


def basket_picks(basket: list[dict[str, Any]]) -> list[tuple[ProductGroup, int]]:
    return [(ProductGroup.model_validate(line["group"]), line["qty"]) for line in basket or []]


def basket_spent(basket: list[dict[str, Any]]) -> float:
    """Basket at each product's cheapest listed price (before fees)."""
    total = 0.0
    for g, qty in basket_picks(basket):
        best = g.cheapest()
        if best:
            total += best[1] * qty
    return round(total, 2)


# ---------------------------------------------------------------- carts and bills (Phase 4b)


def picks_for_app(picks: list[tuple[ProductGroup, int]], app: str) -> tuple[list[tuple[Product, int]], list[str]]:
    """What can be put in `app`'s cart, and which picks it doesn't have (or has out of stock).
    The products get back the ids their cart needs (ProductGroup.keys), since Product.raw
    isn't kept in the workflow state."""
    items, missing = [], []
    for g, qty in picks:
        p = g.items.get(app)
        if p is None or not p.in_stock:
            missing.append(g.name)
            continue
        items.append((p.model_copy(update={"raw": dict(g.keys.get(app) or {})}), qty))
    return items, missing


def _fees(cart: dict[str, Any]) -> str:
    """'delivery ₹30, other ₹5': 'other' is whatever the app's total has beyond the items and
    the fees it named (total − items − named fees), so the line always adds up."""
    charges = dict(cart.get("charges") or {})
    total, items = cart.get("total"), cart.get("item_total")
    if total is not None and items is not None:
        other = round(total - items - sum(charges.values()), 2)
        if abs(other) >= 0.5:
            charges["other"] = other
    if not charges:
        return "—"
    return ", ".join(f"{k} {rs(round(v, 2))}" for k, v in charges.items())


def compare_total(q: dict[str, Any]) -> Optional[float]:
    """The bill for YOUR picks only: Amazon's bill minus other items you kept in its cart."""
    cart = q.get("cart") or {}
    total = cart.get("total")
    if total is None:
        return None
    return round(total - (q.get("others_value") or 0.0), 2)


def best_quote(quotes: list[dict[str, Any]]) -> Optional[int]:
    """Cheapest app that has ALL your picks; if none has all, the cheapest with a bill."""
    priced = [(i, compare_total(q)) for i, q in enumerate(quotes) if compare_total(q) is not None]
    complete = [(i, t) for i, t in priced if not quotes[i].get("missing")]
    pool = complete or priced
    return min(pool, key=lambda x: x[1])[0] if pool else None


def quotes_table(quotes: list[dict[str, Any]], budget: Optional[int]) -> str:
    lines = [f"{'#':>2}  {'app':<11} {'items':>8}  {'fees':<34} {'TO PAY':>8}"]
    notes = []
    for k, q in enumerate(quotes, 1):
        name = APP_LABEL.get(q["app"], q["app"])
        cart = q.get("cart")
        if not cart or cart.get("total") is None:
            lines.append(f"{k:>2}  {name:<11} {'':>8}  {'no bill: ' + (q.get('note') or '?'):<34}")
            continue
        lines.append(f"{k:>2}  {name:<11} {rs(cart.get('item_total')):>8}  {_fees(cart)[:34]:<34}"
                     f" {rs(cart.get('total')):>8}")
        for ln in cart.get("lines") or []:
            lines.append(f"      {ln['quantity']} x {ln['name'][:50]:<50} {rs(ln.get('price')):>8}")
        if q.get("missing"):
            notes.append(f"   {name} doesn't have: {', '.join(q['missing'])}")
        if q.get("others_value"):
            notes.append(f"   {name}'s bill includes your other items already in its cart "
                         f"({rs(q['others_value'])}); your picks alone ≈ {rs(compare_total(q))}")
        if q.get("note"):
            notes.append(f"   {name}: {q['note']}")
        t = compare_total(q)
        if budget and t is not None and t > budget:
            notes.append(f"   ⚠ {name} is {rs(round(t - budget, 2))} over your ₹{budget} budget")
    return "\n".join(lines + notes)


# ---------------------------------------------------------------- cards for the web page


def product_cards(groups: list[ProductGroup], apps: list[str], plan: GroceryPlan,
                  max_qty: int, show: int) -> dict:
    """The ranked products as data for the page's cards (photo, both prices, label, G, +/-).
    Your taps come back as the same text the terminal uses: '4x2, 7'."""
    items = []
    for g in groups:
        prices = {a: g.price(a) for a in apps if g.price(a) is not None}
        low = min(prices.values()) if prices else None
        cheaper = None
        if len(prices) > 1 and list(prices.values()).count(low) == 1:
            cheaper = next(a for a, v in prices.items() if v == low)
        mrp = {a: p.mrp for a, p in g.items.items() if p.mrp and p.price and p.mrp > p.price}
        n = g.nutrition
        items.append({
            "value": g.gid,
            "title": g.name,
            "size": _size(g),
            "image": next((p.image_url for p in g.items.values() if p.image_url), None),
            "rating": g.rating,
            "prices": prices, "mrp": mrp, "cheaper": cheaper,
            "per100": g.per_100(),
            "ad": any(p.is_ad for p in g.items.values()),
            "verdict": g.verdict if plan.health_mode else None,
            "G": g.health if plan.health_mode else None,
            "kcal": n.kcal if n else None, "sugar": n.sugar_g if n else None,
            "protein": n.protein_g if n else None, "basis": n.basis if n else None,
            "borrowed": bool(g.nutrition_from and g.nutrition_from.startswith("label of")),
            "claim": g.claims,
        })
    limits = ", ".join(x for x in [
        f"≤{plan.max_kcal:g} kcal" if plan.max_kcal is not None else "",
        f"≤{plan.max_sugar_g:g} g sugar" if plan.max_sugar_g is not None else "",
        f"≥{plan.min_protein_g:g} g protein" if plan.min_protein_g is not None else ""] if x)
    note = (f"Limits per 100 g/ml: {limits}" if limits else "") if plan.health_mode else \
        "Cheapest per 100 g/ml first"
    return {"kind": "products", "multi": True, "max_qty": max_qty, "show": show,
            "health": plan.health_mode, "note": note,
            "apps": [{"id": a, "label": APP_LABEL.get(a, a)} for a in apps], "items": items}