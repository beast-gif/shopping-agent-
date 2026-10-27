"""One adapter per platform. Every adapter returns the shared schemas in app/schemas.py,
so the workflows never see platform-specific JSON."""
from app.adapters.base import FoodAdapter, GroceryAdapter
from app.adapters.swiggy import SwiggyAdapter
from app.adapters.zepto import ZeptoAdapter
from app.adapters.zomato import ZomatoAdapter

__all__ = ["FoodAdapter", "GroceryAdapter", "SwiggyAdapter", "ZeptoAdapter", "ZomatoAdapter"]
