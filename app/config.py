"""Loads config.yaml, substitutes ${ENV_VAR} values from .env, validates with pydantic."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


class LLMConfig(BaseModel):
    provider: str = "velona"
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    structured_output_method: Literal["function_calling", "json_mode", "json_schema"] = "function_calling"


class McpServerConfig(BaseModel):
    url: str
    enabled: bool = True


class McpConfig(BaseModel):
    oauth_redirect_port: int = 3030
    token_dir: str = ".tokens"
    servers: dict[str, McpServerConfig]
    blocked_tool_patterns: list[str] = Field(default_factory=list)
    allowed_tools: dict[str, list[str]] = Field(default_factory=dict)


class AmazonNowConfig(BaseModel):
    profile_dir: str = ".browser/amazon_now"    # its own browser profile: you log in to Amazon here once
    headless: bool = False
    channel: str = "chrome"                     # 'brave', 'chrome', or 'chromium' (Playwright's own)
    executable_path: str = ""                   # only if Brave isn't found in its usual install folder
    base_url: str = "https://www.amazon.in"
    brand_id: str = "qqfsWw9RkO"                # Amazon Now's id on amazon.in (seen in every /tez/ call)
    search_pages: int = 2                       # 10 results per page


class GroceryScoring(BaseModel):
    w_kcal: float = 0.4
    w_sugar: float = 0.4
    w_protein: float = 0.2
    product_match_threshold: int = 90          # token_sort_ratio of cleaned names for 'same product'
    pack_tolerance: float = 0.05               # sizes may differ by 5% (e.g. 100 vs 104 ml)
    details_limit: int = 20                    # Zepto products whose nutrition label is read
    show: int = 15                             # products shown before you search with /word
    max_quantity: int = 10


class FoodScoring(BaseModel):
    w_rating: float = 0.5
    w_eta: float = 0.3
    w_dish: float = 0.2
    restaurant_name_threshold: int = 85        # token_set_ratio for 'same restaurant'
    same_restaurant_max_km_diff: float = 1.0   # the two apps' distances must agree within this
    menu_item_threshold: int = 90              # token_sort_ratio for 'same dish'
    top_restaurants: int = 10
    menu_show: int = 30                        # dishes shown before you search with /word
    max_quantity: int = 10


class LocationsConfig(BaseModel):
    places_file: str = "places.yaml"
    auto_detect: bool = True
    match_radius_m: float = 300          # GPS fix must be this close to a place to count
    max_gps_accuracy_m: float = 150      # fixes vaguer than this (e.g. IP-based) are ignored
    gps_timeout_s: float = 10
    group_threshold: float = 0.45        # address-text similarity needed to suggest 'same place'


class OrderingConfig(BaseModel):
    """Option 1: the agent places a UPI order only after you type 'confirm <total>'.
    Which tools can ever be used, and with which arguments, is hard-coded in
    app/mcp_client.py (ORDER_RULES); this section can only narrow it, never widen it."""
    enabled: bool = False
    max_order_total: float = 1500
    approval_valid_seconds: int = 300
    payment_poll_seconds: int = 10
    payment_timeout_seconds: int = 300
    tools: dict[str, list[str]] = Field(default_factory=dict)


class ScoringConfig(BaseModel):
    grocery: GroceryScoring = Field(default_factory=GroceryScoring)
    food: FoodScoring = Field(default_factory=FoodScoring)


class AppConfig(BaseModel):
    llm: LLMConfig
    mcp: McpConfig
    amazon_now: AmazonNowConfig = Field(default_factory=AmazonNowConfig)
    ordering: OrderingConfig = Field(default_factory=OrderingConfig)
    locations: LocationsConfig = Field(default_factory=LocationsConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    checkpoint_db: str = ".checkpoints/shop.sqlite"


def _substitute_env(value: Any) -> Any:
    """Recursively replace ${VAR} with os.environ['VAR'] ('' if unset)."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


def load_config(path: str | Path | None = None) -> AppConfig:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(_substitute_env(raw))


def resolve(path_str: str) -> Path:
    """Resolve a config path relative to the project root."""
    p = Path(path_str)
    return p if p.is_absolute() else ROOT / p