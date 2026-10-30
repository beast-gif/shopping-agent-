"""Main graph (Phase 6): one chat for food and groceries.

                 ┌──────────┐
  your message ─>│  router  │  one LLM call: grocery / food_dish / food_restaurant / unknown
                 └────┬─────┘  (+ item, qualifier, budget, restaurant, place if you said them)
          ┌───────────┼────────────┐
          ▼           ▼            ▼
      grocery       food        unknown
     (subgraph)   (subgraph)   (explains what it can do)
          └───────────┴────────────┘
                      ▼
                     END

grocery and food are the compiled workflow graphs, each run inside one node. When a
subgraph asks you something (interrupt), the question travels up through that node to the
chat. When you answer, the node runs again and the subgraph continues from its checkpoint
at the same question, so the node itself must do nothing else that would repeat: it only
maps the state in, calls the subgraph and maps the result out.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.adapters.base import FoodAdapter, GroceryAdapter
from app.config import AppConfig
from app.router import make_router_node
from app.workflows.food import build_food_graph
from app.workflows.grocery import build_grocery_graph

HELP = ("I can order food (Swiggy / Zomato) or groceries (Zepto / Amazon Now). Try:\n"
        "   biryani                          a dish: best restaurants on both apps\n"
        "   meghana foods                    a restaurant: straight to its menu\n"
        "   guilt free ice cream under 200   groceries, compared on both apps\n"
        "   ice cream, chips, curd           a grocery basket, one bill\n"
        "   chicken momos at college         'at college' / 'to home' picks the address")


class MainState(TypedDict, total=False):
    user_message: str
    # filled by the router
    intent: str
    item: Optional[str]
    qualifier: Optional[str]
    budget: Optional[int]
    restaurant_query: Optional[str]
    place_hint: Optional[str]
    router_confidence: float
    entry: Optional[str]
    # filled by the flow that ran
    result: Optional[str]


def build_main_graph(cfg: AppConfig,
                     food_adapters: dict[str, FoodAdapter],
                     grocery_adapters: dict[str, GroceryAdapter],
                     ensure_grocery: Optional[Callable[[], Awaitable[None]]] = None,
                     log: Callable[[str], None] = print) -> StateGraph:
    """ensure_grocery: called before every grocery run (e.g. opens the Amazon browser the first
    time). It runs again when you answer a grocery question, so it must be safe to repeat."""
    # No checkpointer here: subgraphs use the parent's, saved under their own namespace.
    food_graph = build_food_graph(cfg, food_adapters, log).compile()
    grocery_graph = build_grocery_graph(cfg, grocery_adapters, log).compile()

    async def grocery(state: MainState) -> dict:
        if ensure_grocery is not None:
            await ensure_grocery()
        if not grocery_adapters:
            return {"result": "No grocery app is connected (Zepto / Amazon Now)."}
        item = (state.get("item") or state["user_message"]).strip()
        qualifier = state.get("qualifier")
        # 'ice cream, chips' is a basket: each item already carries its own words
        request = item if ("," in item or not qualifier or qualifier.lower() in item.lower()) \
            else f"{qualifier} {item}"
        out = await grocery_graph.ainvoke({"request": request, "budget": state.get("budget"),
                                           "place_hint": state.get("place_hint")})
        return {"result": out.get("done") or "Done."}

    async def food(state: MainState) -> dict:
        if not food_adapters:
            return {"result": "No food app is connected (Swiggy / Zomato)."}
        # Trust 'restaurant' only when the router is sure; otherwise the food flow checks the
        # name against real restaurants and dishes itself ('auto').
        entry = "restaurant" if (state.get("intent") == "food_restaurant"
                                 and (state.get("router_confidence") or 0) >= 0.7) else "auto"
        request = (state.get("restaurant_query") or state.get("item") or state["user_message"]).strip()
        out = await food_graph.ainvoke({"request": request, "entry": entry,
                                        "place_hint": state.get("place_hint")})
        return {"result": out.get("done") or "Done."}

    def unknown(state: MainState) -> dict:
        return {"result": HELP}

    def after_router(state: MainState) -> str:
        intent = state.get("intent") or "unknown"
        if intent == "grocery":
            return "grocery"
        if intent in ("food_dish", "food_restaurant"):
            return "food"
        return "unknown"

    route = make_router_node(cfg)

    async def router(state: MainState) -> dict:
        try:
            return await route(state)
        except Exception as e:  # noqa: BLE001  LLM down -> explain instead of crashing the chat
            log(f"(Couldn't understand that: the LLM call failed: {type(e).__name__}: {str(e)[:120]})")
            return {"intent": "unknown"}

    g = StateGraph(MainState)
    g.add_node("router", router)
    g.add_node("grocery", grocery)
    g.add_node("food", food)
    g.add_node("unknown", unknown)
    g.add_edge(START, "router")
    g.add_conditional_edges("router", after_router, ["grocery", "food", "unknown"])
    for name in ("grocery", "food", "unknown"):
        g.add_edge(name, END)
    return g