"""Adapter interfaces + parsing helpers shared by all platforms."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Iterable, Optional

from app.approval import OrderApproval
from app.mcp_client import McpHub
from app.schemas import (
    CartResult,
    FoodSearch,
    MenuItem,
    Nutrition,
    PaymentRequest,
    PaymentState,
    Product,
    SavedAddress,
)

# ---------------------------------------------------------------- interfaces


class GroceryAdapter(ABC):
    platform: str

    def __init__(self, hub: McpHub):
        self.hub = hub

    @abstractmethod
    async def list_addresses(self) -> list[SavedAddress]: ...

    @abstractmethod
    async def set_address(self, address_id: str) -> None:
        """Must be called before search: the address decides which dark store serves you."""

    @abstractmethod
    async def search(self, query: str) -> list[Product]: ...

    @abstractmethod
    async def details(self, product: Product) -> Product:
        """Same product with brand / nutrition / rating filled in."""

    @abstractmethod
    async def add_to_cart(self, product: Product, quantity: int = 1, replace: bool = False) -> CartResult: ...

    @abstractmethod
    async def get_cart(self) -> CartResult: ...

    # ---- ordering (option 1: UPI after typed confirmation)

    @abstractmethod
    async def preview_order(self) -> CartResult:
        """The cart with the app's real fees and final total. Places nothing."""

    @abstractmethod
    async def place_order(self, approval: OrderApproval) -> PaymentRequest:
        """Creates a pending-payment UPI order. Needs approval for exactly this bill."""

    @abstractmethod
    async def payment_status(self, payment: PaymentRequest) -> PaymentState: ...

    async def finalize(self, payment: PaymentRequest, state: PaymentState) -> None:
        """Called once the payment is terminal or timed out. Most apps need nothing."""
        return None


class FoodAdapter(ABC):
    platform: str

    def __init__(self, hub: McpHub):
        self.hub = hub

    @abstractmethod
    async def list_addresses(self) -> list[SavedAddress]: ...

    @abstractmethod
    async def search(self, address_id: str, query: str) -> FoodSearch:
        """Restaurants and dishes matching a dish name or restaurant name."""

    @abstractmethod
    async def get_menu(self, address_id: str, restaurant_id: str) -> list[MenuItem]: ...

    @abstractmethod
    async def add_to_cart(self, address_id: str, restaurant_id: str,
                          items: list[tuple[MenuItem, int]], replace: bool = False) -> CartResult:
        """All items must be from one restaurant. replace=True empties the agent's cart first.
        Returns the cart as the app reports it."""

    @abstractmethod
    async def get_cart(self, address_id: str) -> CartResult: ...

    # ---- ordering (option 1: UPI after typed confirmation)

    @abstractmethod
    async def place_order(self, address_id: str, cart: CartResult, approval: OrderApproval) -> PaymentRequest:
        """Creates a pending-payment UPI order for `cart`. Needs approval for exactly this bill."""

    @abstractmethod
    async def payment_status(self, payment: PaymentRequest) -> PaymentState: ...

    async def finalize(self, payment: PaymentRequest, state: PaymentState) -> None:
        """Called once the payment is terminal or timed out. Most apps need nothing."""
        return None


# ---------------------------------------------------------------- payments

_PAID_WORDS = ("SUCCESS", "PAID", "COMPLETED", "CAPTURED", "CHARGED", "PLACED")
_FAILED_WORDS = ("FAIL", "DECLINED", "CANCEL", "EXPIRED", "TIMEOUT", "TIMED_OUT", "REJECT")


def payment_state(value: Any) -> PaymentState:
    """'SUCCESS' -> PAID, 'PENDING_PAYMENT' / 'UNPAID' -> PENDING, 'FAILED' -> FAILED ..."""
    s = str(value or "").upper()
    if not s:
        return "UNKNOWN"
    if "REFUND" in s:
        return "REFUND"
    if "UNPAID" in s or "PENDING" in s:
        return "PENDING"
    if any(w in s for w in _FAILED_WORDS):
        return "FAILED"
    if any(w in s for w in _PAID_WORDS):
        return "PAID"
    return "PENDING"


def extract_payment_bits(obj: Any) -> dict[str, Optional[str]]:
    """Finds the UPI link, QR image and payment page anywhere in a response.
    {'upi_link': 'upi://pay?...', 'qr_png_base64': 'iVBOR...', 'payment_url': 'https://...'}"""
    found: dict[str, Optional[str]] = {"upi_link": None, "qr_png_base64": None, "payment_url": None}

    def walk(o: Any, key: str = "") -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, str(k).lower())
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str):
            s = o.strip()
            if s.lower().startswith("upi://") and not found["upi_link"]:
                found["upi_link"] = s
            elif s.startswith("data:image") and not found["qr_png_base64"]:
                found["qr_png_base64"] = s.split(",", 1)[-1]
            elif "qr" in key and len(s) > 200 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", s) and not found["qr_png_base64"]:
                found["qr_png_base64"] = s
            elif s.startswith("http") and any(w in key for w in ("pay", "link", "url", "qr")) \
                    and not found["payment_url"] and "image" not in key and "img" not in key:
                found["payment_url"] = s

    walk(obj)
    return found


# ---------------------------------------------------------------- numbers


def to_float(value: Any) -> Optional[float]:
    """'4.4' -> 4.4, 4 -> 4.0, '' / None / 'NA' -> None"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group()) if m else None


def in_stock_flag(value: Any) -> bool:
    """None -> True (not reported), True/False as is, 1/0 or '1'/'0' -> True/False"""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return (to_float(value) or 0) > 0


def paise_to_rupees(value: Any) -> Optional[float]:
    v = to_float(value)
    return round(v / 100, 2) if v is not None else None


def eta_minutes(value: Any) -> Optional[float]:
    """'40–45 min' -> 42.5, '5-15 MINS' -> 10, 12 -> 12"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not value:
        return None
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", str(value))]
    if not nums:
        return None
    return round(sum(nums[:2]) / len(nums[:2]), 1)


# ---------------------------------------------------------------- pack size

_UNIT = r"(kg|kgs|g|gm|gms|gram|grams|ml|l|ltr|litre|litres|liter|liters)"
_UNIT_MAP = {
    "kg": ("g", 1000), "kgs": ("g", 1000),
    "g": ("g", 1), "gm": ("g", 1), "gms": ("g", 1), "gram": ("g", 1), "grams": ("g", 1),
    "ml": ("ml", 1),
    "l": ("ml", 1000), "ltr": ("ml", 1000), "litre": ("ml", 1000), "litres": ("ml", 1000),
    "liter": ("ml", 1000), "liters": ("ml", 1000),
}


def parse_pack_size(text: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """'1 pc (120 ml)' -> (120, 'ml');  '2 x 500 ml' -> (1000, 'ml');  '1kg' -> (1000, 'g');
    '6 pcs' -> (6, 'pcs');  None -> (None, None)"""
    if not text:
        return None, None
    s = text.lower().replace("×", "x")

    multi = re.search(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*" + _UNIT + r"\b", s)
    if multi:
        unit, factor = _UNIT_MAP[multi.group(3)]
        return round(int(multi.group(1)) * float(multi.group(2)) * factor, 2), unit

    amounts = re.findall(r"(\d+(?:\.\d+)?)\s*" + _UNIT + r"\b", s)
    if amounts:
        number, unit_word = amounts[-1]          # '1 pc (120 ml)': the bracketed amount is the real size
        unit, factor = _UNIT_MAP[unit_word]
        return round(float(number) * factor, 2), unit

    pieces = re.search(r"(\d+)\s*(pc|pcs|piece|pieces|pack|packs|unit|units)\b", s)
    if pieces:
        return float(pieces.group(1)), "pcs"
    return None, None


# ---------------------------------------------------------------- nutrition text

_NUTRIENT_PATTERNS = {
    "kcal": [r"energy\s*\(kcal\)\s*([\d.]+)", r"energy[^0-9]{0,15}([\d.]+)\s*kcal", r"calories?\D{0,10}([\d.]+)"],
    "added_sugar_g": [r"added\s+sugars?\s*\(g\)\s*([\d.]+)"],
    "sugar_g": [r"total\s+sugars?\s*\(g\)\s*([\d.]+)", r"(?<!added )sugars?\s*\(g\)\s*([\d.]+)"],
    "protein_g": [r"protein\s*\(g\)\s*([\d.]+)", r"protein\D{0,10}([\d.]+)\s*g"],
}


def parse_nutrition_text(text: Optional[str], basis: str = "g") -> Optional[Nutrition]:
    """'Energy (kcal) 159.0 (RDA 7.9%), ... Total Sugar (g) 6.2, ... Protein (g) 2.8, ...'
    -> Nutrition(kcal=159, sugar_g=6.2, protein_g=2.8). None if nothing recognisable."""
    if not text:
        return None
    s = text.lower()
    values: dict[str, float] = {}
    for field, patterns in _NUTRIENT_PATTERNS.items():
        for p in patterns:
            m = re.search(p, s)
            if m:
                try:
                    values[field] = float(m.group(1).rstrip("."))
                    break
                except ValueError:
                    continue
    if not values:
        return None
    return Nutrition(basis="ml" if basis == "ml" else "g", **values)


# ---------------------------------------------------------------- misc


def clean_ad_name(name: str) -> tuple[str, bool]:
    """'Nandhana Palace (Ad)' -> ('Nandhana Palace', True)"""
    cleaned = re.sub(r"\s*\(ad\)\s*$", "", name, flags=re.IGNORECASE).strip()
    return cleaned, cleaned != name.strip()


def strip_name_prefix(address_line: str) -> str:
    """Swiggy prefixes addresses with 'NAME SG: '. Drop everything up to that marker."""
    m = re.match(r"^.{0,60}?\bSG:\s*", address_line)
    return address_line[m.end():] if m else address_line


def find_pincode(text: Optional[str]) -> Optional[str]:
    m = re.search(r"\b(5\d{5})\b", text or "")     # Karnataka/AP/TN pincodes start with 5
    return m.group(1) if m else None


def first_value(obj: Any, keys: Iterable[str]) -> Any:
    """First value stored under any of `keys`, searching nested dicts/lists breadth-first."""
    keys = set(keys)
    queue = [obj]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in keys and v not in (None, "", [], {}):
                    return v
            queue.extend(cur.values())
        elif isinstance(cur, list):
            queue.extend(cur)
    return None