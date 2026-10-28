"""Places: one real-world location (home, college ...) mapped to the saved address of that
location in each app.

Only Zepto gives coordinates; Swiggy and Zomato give address text only. So the 'same place'
suggestion compares address texts, and you confirm it once in scripts/setup_places.py.

Text similarity (theory):
  1. tokens = lower-cased words, minus filler ('bengaluru', 'road', 'flat' ...); '07' -> '7'
  2. weight(t) = ln((N + 1) / (df(t) + 0.5)), where N = number of addresses and df = how
     many of them contain t. Rare words ('sadan', 'allen') weigh more than common ones
     ('sai', 'colony'), so two flats in the same colony don't look identical.
  3. Only 'shareable' words count: ones in 2+ addresses, or ≥ 85% alike to a word elsewhere
     ('shanti' ~ 'shanthi'). A word in just one address ('vidyapeetam') can't match anything,
     so it would only dilute the score.
  4. cover(A→B) = weight of A's shareable words found in B / weight of A's shareable words
     sim(A, B) = (cover(A→B) + cover(B→A)) / 2       (both directions, so ties break)
  5. different pincodes -> sim × 0.6

Grouping: Zepto addresses seed the places (they have coordinates). Then, one app at a time,
the highest-scoring (address, place) pairs are assigned first, one address per app per place.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from app.config import AppConfig, resolve
from app.schemas import SavedAddress

APP_ORDER = ["zepto", "swiggy", "zomato", "amazon_now"]

_FILLER = {
    "bengaluru", "bangalore", "karnataka", "india", "road", "rd", "main", "cross", "flat", "no",
    "floor", "block", "sg", "near", "behind", "opp", "opposite", "phase", "stage", "sector",
    "the", "and", "of", "st", "street", "th", "nd", "rd", "1st", "2nd", "3rd",
}


class Place(BaseModel):
    name: str                                            # 'home', 'college' ...
    aliases: list[str] = Field(default_factory=list)     # other words you might use: 'hostel', 'clg'
    wifi: list[str] = Field(default_factory=list)        # Wi-Fi names (SSIDs) seen only here
    lat: Optional[float] = None                          # from the Zepto address, if there is one
    lng: Optional[float] = None
    addresses: dict[str, str] = Field(default_factory=dict)   # app -> address_id
    texts: dict[str, str] = Field(default_factory=dict)       # app -> address text (for display)

    def address_for(self, app: str) -> Optional[str]:
        return self.addresses.get(app)

    def names(self) -> list[str]:
        return [self.name, *self.aliases]


class PlaceBook(BaseModel):
    places: list[Place] = Field(default_factory=list)

    def get(self, name: str) -> Optional[Place]:
        for p in self.places:
            if name.lower() in (n.lower() for n in p.names()):
                return p
        return None


# ---------------------------------------------------------------- load / save


def places_path(cfg: AppConfig) -> Path:
    return resolve(cfg.locations.places_file)


def load_places(cfg: AppConfig) -> PlaceBook:
    path = places_path(cfg)
    if not path.exists():
        return PlaceBook()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return PlaceBook.model_validate(data)


def save_places(cfg: AppConfig, book: PlaceBook) -> Path:
    path = places_path(cfg)
    path.write_text(
        yaml.safe_dump(book.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------- text similarity


def tokens(text: str) -> list[str]:
    """'07 , Shanti sudha , 2Q34+644, Sai Colony' -> ['7', 'shanti', 'sudha', '2q34', '644', 'sai', 'colony']"""
    out = []
    for t in re.split(r"[^a-z0-9]+", text.lower()):
        if not t or t in _FILLER:
            continue
        if t.isdigit():
            t = str(int(t))                  # '07' -> '7'
            if len(t) == 6 and t.startswith("5"):
                continue                     # pincode: compared separately
        elif len(t) < 2:
            continue
        out.append(t)
    return list(dict.fromkeys(out))          # unique, order kept


def _same_word(a: str, b: str) -> bool:
    return a == b or (len(a) >= 4 and len(b) >= 4 and fuzz.ratio(a, b) >= 85)


def make_similarity(addresses: list[SavedAddress]):
    """Returns sim(a, b) in [0, 1] with word weights learned from all your addresses."""
    toks = {id(a): tokens(a.text) for a in addresses}
    n = len(addresses)
    df = Counter(t for ts in toks.values() for t in set(ts))
    weight = {t: math.log((n + 1) / (df[t] + 0.5)) for t in df}
    vocab = list(df)
    shareable = {t for t in vocab
                 if df[t] >= 2 or any(u != t and _same_word(t, u) for u in vocab)}

    def cover(src: list[str], dst: list[str]) -> Optional[float]:
        mine = [t for t in src if t in shareable]
        total = sum(weight[t] for t in mine)
        if total <= 0:
            return None
        found = sum(weight[t] for t in mine if any(_same_word(t, u) for u in dst))
        return found / total

    def sim(a: SavedAddress, b: SavedAddress) -> float:
        ta, tb = toks.get(id(a), []), toks.get(id(b), [])
        ab, ba = cover(ta, tb), cover(tb, ta)
        if ab is None or ba is None:
            return 0.0
        score = (ab + ba) / 2
        if a.pincode and b.pincode and a.pincode != b.pincode:
            score *= 0.6
        return round(min(score, 1.0), 3)

    return sim


def suggest_groups(addresses: list[SavedAddress], threshold: float = 0.45) -> list[list[SavedAddress]]:
    """Zepto addresses seed the groups. Then for each other app: score every (address, group)
    pair as the mean sim to the group's members, assign the best pairs first (one address
    per app per group), and give leftovers a group of their own."""
    sim = make_similarity(addresses)
    by_app: dict[str, list[SavedAddress]] = {}
    for a in addresses:
        by_app.setdefault(a.platform, []).append(a)
    apps = sorted(by_app, key=lambda p: APP_ORDER.index(p) if p in APP_ORDER else 99)

    groups: list[list[SavedAddress]] = []
    for app in apps:
        pairs = []
        for a in by_app[app]:
            for gi, g in enumerate(groups):
                if any(m.platform == app for m in g):
                    continue
                pairs.append((sum(sim(a, m) for m in g) / len(g), id(a), gi, a))
        pairs.sort(key=lambda p: p[0], reverse=True)

        placed: set[int] = set()
        filled: set[int] = set()
        for score, aid, gi, a in pairs:
            if score < threshold:
                break
            if aid in placed or gi in filled:
                continue
            groups[gi].append(a)
            placed.add(aid)
            filled.add(gi)
        for a in by_app[app]:
            if id(a) not in placed:
                groups.append([a])
    return groups


def default_name(group: list[SavedAddress], taken: set[str]) -> str:
    """Swiggy's tag ('Home', 'Clg', 'Work', 'Family') is the most reliable label you set;
    Zepto's labels are often 'Other'."""
    by_app = {a.platform: a for a in group}
    for app in ("swiggy", "zepto", "zomato"):
        label = (by_app.get(app).label if by_app.get(app) else None) or ""
        label = label.strip().lower()
        if label and label != "other" and label not in taken:
            return label
    i = 1
    while f"place{i}" in taken:
        i += 1
    return f"place{i}"


def place_from_group(name: str, group: list[SavedAddress]) -> Place:
    zepto = next((a for a in group if a.lat is not None and a.lng is not None), None)
    return Place(
        name=name,
        lat=zepto.lat if zepto else None,
        lng=zepto.lng if zepto else None,
        addresses={a.platform: a.address_id for a in group},
        texts={a.platform: a.text for a in group},
    )


# Everyday words that mean the same place, so 'college' finds a place you named 'clg'.
SYNONYMS = [
    {"college", "clg", "campus", "coll", "university", "uni"},
    {"home", "house", "ghar"},
    {"work", "office", "workplace"},
    {"hostel", "pg", "room"},
]


def _is_abbreviation(short: str, long: str) -> bool:
    """'clg' abbreviates 'college': same first letter, and its letters appear in order."""
    if len(short) < 3 or len(short) >= len(long) or short[0] != long[0]:
        return False
    it = iter(long)
    return all(ch in it for ch in short)


def _same_place_word(a: str, b: str) -> bool:
    if a == b:
        return True
    if any(a in group and b in group for group in SYNONYMS):
        return True
    return _is_abbreviation(a, b) or _is_abbreviation(b, a) or fuzz.ratio(a, b) >= 80


def match_place_name(book: PlaceBook, hint: Optional[str]) -> Optional[Place]:
    """'I'm at college' -> hint 'college' -> the place whose name or alias is 'college',
    a synonym ('clg', 'campus'), an abbreviation of it, or ≥ 80% alike ('colege')."""
    if not hint:
        return None
    word = hint.strip().lower()
    exact = book.get(word)
    if exact:
        return exact
    for p in book.places:
        if any(_same_place_word(word, n.lower()) for n in p.names()):
            return p
    return None