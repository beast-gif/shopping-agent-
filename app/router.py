"""Intent router: one structured LLM call, no loop.

food_restaurant vs food_dish is only a first guess here. The food workflow
verifies a restaurant guess by searching (every typed word must be in the restaurant's name)
and falls back to the dish flow if nothing matches.
"""
from __future__ import annotations

from app.config import AppConfig
from app.llm import structured
from app.schemas import RouterOutput
from app.state import ShopState

ROUTER_PROMPT = """You route requests for a personal shopping assistant in Bengaluru, India.

Pick exactly one intent:
- grocery: a packaged product from a quick-commerce store (Zepto, Amazon Now).
  Examples: "guilt-free ice cream", "2 litres of milk", "protein bars under 300",
  "ice cream, chips and coke under 400".
- food_dish: a cooked dish ordered from a restaurant (Swiggy, Zomato), no restaurant named.
  Examples: "biryani", "I want a masala dosa", "spicy chicken momos".
- food_restaurant: the user names a specific restaurant they want to order from.
  Examples: "Meghana Foods", "show me Truffles menu", "order from Empire".
- unknown: anything else.

Fields:
- item: the product or dish in plain words ("ice cream", "biryani"). For food_restaurant,
  the dish only if one was also named, else null. For grocery with SEVERAL products, list
  them all comma-separated, each with its own words ("guilt-free ice cream, dark chocolate,
  chips"), and leave qualifier null.
- qualifier: constraint words the user gave ("guilt-free", "veg", "spicy", "low sugar"), else null.
- budget: integer rupees only if the user stated a budget ("under 300", "budget 250"), else null.
- restaurant_name: the restaurant name for food_restaurant, else null.
- place: where to deliver, only if the user said it ("I'm at college" -> "college",
  "send it home" -> "home", "to my hostel" -> "hostel"), else null. Never guess it.
- confidence: below 0.7 when a name could be either a dish or a restaurant
  (e.g. "Paradise", "Empire").
"""


def make_router_node(cfg: AppConfig):
    llm = structured(cfg, RouterOutput)

    async def route_intent(state: ShopState) -> dict:
        out: RouterOutput = await llm.ainvoke(
            [("system", ROUTER_PROMPT), ("human", state["user_message"])]
        )
        entry = {"food_dish": "dish", "food_restaurant": "restaurant"}.get(out.intent)
        return {
            "intent": out.intent,
            "item": out.item,
            "qualifier": out.qualifier,
            "budget": out.budget,
            "restaurant_query": out.restaurant_name,
            "place_hint": out.place,
            "router_confidence": out.confidence,
            "entry": entry,
        }

    return route_intent


def route_after_router(state: ShopState) -> str:
    """Conditional edge: returns the name of the next node."""
    return {
        "grocery": "grocery",
        "food_dish": "food_dish",
        "food_restaurant": "food_restaurant",
    }.get(state.get("intent", "unknown"), "unknown")