"""Shared LangGraph state.

Fields filled by parallel branches use operator.add so each platform's results are
appended instead of overwriting each other. Use a fresh thread_id per request so
these lists start empty.
"""
from __future__ import annotations

from operator import add
from typing import Annotated, Literal, Optional, TypedDict

from app.schemas import (
    CartResult,
    FilterSpec,
    Intent,
    MenuItem,
    MenuRow,
    Outlet,
    PriceQuote,
    Product,
    RestaurantListing,
    ScoredProduct,
)


class ShopState(TypedDict, total=False):
    # ---- input + router
    user_message: str
    intent: Intent
    item: Optional[str]
    qualifier: Optional[str]
    budget: Optional[int]
    restaurant_query: Optional[str]
    router_confidence: float
    entry: Optional[Literal["dish", "restaurant"]]

    # ---- location
    place_hint: Optional[str]      # what you said ('college'), if anything
    place_name: Optional[str]      # the place used for this request
    place_how: Optional[str]       # how it was decided: "Wi-Fi 'Home_5G'", "you said 'college'" ...

    # ---- grocery workflow
    filter_spec: Optional[FilterSpec]
    candidates: Annotated[list[Product], add]          # appended by Zepto ∥ Amazon Now
    ranked: list[ScoredProduct]
    chosen_product: Optional[Product]

    # ---- food workflow
    listings: Annotated[list[RestaurantListing], add]  # appended by Swiggy ∥ Zomato
    outlets: list[Outlet]
    chosen_outlet: Optional[Outlet]
    menu_items: Annotated[list[MenuItem], add]         # appended by Swiggy ∥ Zomato
    merged_menu: list[MenuRow]
    selection: list[MenuRow]
    quotes: list[PriceQuote]
    chosen_platform: Optional[str]

    # ---- output
    cart: Optional[CartResult]
    errors: Annotated[list[str], add]