"""The one normalized data model every adapter returns and every workflow consumes.

All money is in rupees (adapters convert Zepto's paisa).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

GroceryPlatform = Literal["zepto", "amazon_now"]
FoodPlatform = Literal["swiggy", "zomato"]
Platform = Literal["zepto", "amazon_now", "swiggy", "zomato"]
Intent = Literal["grocery", "food_dish", "food_restaurant", "unknown"]


# ---------------------------------------------------------------- router

class RouterOutput(BaseModel):
    intent: Intent = Field(description="Which flow handles this request")
    item: Optional[str] = Field(None, description="Product or dish, e.g. 'ice cream', 'biryani'")
    qualifier: Optional[str] = Field(None, description="Constraint words, e.g. 'guilt-free', 'veg', 'spicy'")
    budget: Optional[int] = Field(None, description="Budget in rupees, only if the user stated one")
    restaurant_name: Optional[str] = Field(None, description="Restaurant name if the user named one")
    place: Optional[str] = Field(None, description="Where to deliver, only if the user said it: 'home', 'college' ...")
    confidence: float = Field(1.0, ge=0, le=1, description="How sure you are about the intent, 0-1")


# ---------------------------------------------------------------- addresses

class SavedAddress(BaseModel):
    """One saved address in one app. Only Zepto exposes coordinates."""
    platform: Platform
    address_id: str
    label: Optional[str] = None          # 'Home', 'Clg', 'Other' ... (user-chosen, not reliable)
    text: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    pincode: Optional[str] = None


# ---------------------------------------------------------------- grocery

class Nutrition(BaseModel):
    """Values as printed on the label; Indian labels declare per 100 g / 100 ml."""
    kcal: Optional[float] = None
    sugar_g: Optional[float] = None
    added_sugar_g: Optional[float] = None
    protein_g: Optional[float] = None
    basis: Literal["g", "ml"] = "g"


class Product(BaseModel):
    platform: GroceryPlatform
    product_id: str                              # Zepto: productVariantId
    store_product_id: Optional[str] = None       # Zepto: storeProductId (needed for cart)
    name: str
    brand: Optional[str] = None
    pack_size: Optional[float] = None            # numeric amount, e.g. 500
    pack_unit: Optional[Literal["g", "ml", "pcs"]] = None
    price: float                                 # selling price in rupees
    mrp: Optional[float] = None
    delivery_fee: float = 0.0
    handling_fee: float = 0.0
    discount: float = 0.0
    in_stock: bool = True
    is_ad: bool = False
    rating: Optional[float] = None
    url: Optional[str] = None
    image_url: Optional[str] = None
    nutrition: Optional[Nutrition] = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @property
    def effective_price(self) -> float:
        """item + delivery + handling - discount"""
        return round(self.price + self.delivery_fee + self.handling_fee - self.discount, 2)

    @property
    def price_per_100(self) -> Optional[float]:
        if not self.pack_size or self.pack_unit not in ("g", "ml"):
            return None
        return round(self.effective_price / self.pack_size * 100, 2)


class FilterSpec(BaseModel):
    """What the qualifier (e.g. 'guilt-free') means in numbers, per 100 g/ml."""
    max_kcal: Optional[float] = None
    max_sugar_g: Optional[float] = None
    min_protein_g: Optional[float] = None
    search_terms: list[str] = Field(default_factory=list, description="Extra search queries, e.g. 'protein ice cream'")
    notes: Optional[str] = None


class ScoredProduct(BaseModel):
    product: Product
    health_score: Optional[float] = None         # G in [0, 1]
    matched_group: Optional[str] = None          # same product across platforms shares a group id


# ---------------------------------------------------------------- food

class RestaurantListing(BaseModel):
    """One restaurant as seen on one platform. Neither app exposes restaurant coordinates;
    distance_km is measured from the address used in the search."""
    platform: FoodPlatform
    restaurant_id: str
    name: str                                    # cleaned: '(Ad)' suffix removed
    rating: Optional[float] = None
    eta_min: Optional[float] = None
    distance_km: Optional[float] = None
    area: Optional[str] = None
    offer: Optional[str] = None                  # e.g. 'Flat ₹40 OFF'
    cuisines: list[str] = Field(default_factory=list)
    is_open: bool = True
    is_ad: bool = False
    lat: Optional[float] = None
    lng: Optional[float] = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class Outlet(BaseModel):
    """One physical restaurant branch, merged across platforms."""
    outlet_id: str
    name: str
    listings: dict[str, RestaurantListing] = Field(default_factory=dict)   # key = platform
    score: Optional[float] = None                                          # R in [0, 1]


class MenuItem(BaseModel):
    platform: FoodPlatform
    restaurant_id: str
    restaurant_name: Optional[str] = None
    item_id: str                     # the id the cart needs: Swiggy menu_item_id, Zomato variant_id ('v_...')
    name: str
    variant: Optional[str] = None    # 'half', 'full', '500 ml' ...
    price: float                     # listed price in rupees
    discounted_price: Optional[float] = None
    is_veg: Optional[bool] = None
    in_stock: bool = True
    has_variants: bool = False       # needs a variant choice before it can go in the cart
    has_addons: bool = False         # add-ons are optional
    rating: Optional[float] = None
    description: Optional[str] = None
    category: Optional[str] = None
    kcal: Optional[float] = None     # Zomato publishes these for many dishes
    protein_g: Optional[float] = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @property
    def unit_price(self) -> float:
        return self.discounted_price if self.discounted_price is not None else self.price


class FoodSearch(BaseModel):
    """Result of one dish/restaurant search on one platform."""
    platform: FoodPlatform
    restaurants: list[RestaurantListing] = Field(default_factory=list)
    dishes: list[MenuItem] = Field(default_factory=list)


class MenuRow(BaseModel):
    """One dish in the merged menu, with its listing on each platform (if any)."""
    row_id: str
    name: str
    variant: Optional[str] = None
    items: dict[str, MenuItem] = Field(default_factory=dict)              # key = platform
    quantity: int = 1


class PriceQuote(BaseModel):
    """Effective price of a whole selection on one platform.
    total = items + packaging + delivery + platform_fee + taxes - discount"""
    platform: FoodPlatform
    items_total: float = 0.0
    packaging: float = 0.0
    delivery: float = 0.0
    platform_fee: float = 0.0
    taxes: float = 0.0
    discount: float = 0.0
    missing_items: list[str] = Field(default_factory=list)

    @property
    def total(self) -> float:
        return round(self.items_total + self.packaging + self.delivery
                     + self.platform_fee + self.taxes - self.discount, 2)

    @property
    def complete(self) -> bool:
        return not self.missing_items


# ---------------------------------------------------------------- cart

class CartLine(BaseModel):
    name: str
    quantity: int
    price: Optional[float] = None    # line price in rupees, if the app reports it


PaymentState = Literal["PENDING", "PAID", "FAILED", "REFUND", "UNKNOWN"]


class PaymentRequest(BaseModel):
    """An order created in 'pending payment' state, waiting for you to pay by UPI."""
    platform: Platform
    order_id: Optional[str] = None
    amount: Optional[float] = None           # what the app says you'll pay
    approved_amount: float                   # what you typed 'confirm' for
    upi_link: Optional[str] = None           # upi://pay?... (shown as a QR you scan)
    qr_png_base64: Optional[str] = None      # QR image the app generated
    payment_url: Optional[str] = None        # https link (Zepto's payment page, etc.)
    message: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)   # ids needed to check/finalize (paasId, cartId ...)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @property
    def amount_mismatch(self) -> bool:
        """True if the app wants more than you approved (by over ₹1)."""
        return self.amount is not None and self.amount > self.approved_amount + 1


class CartResult(BaseModel):
    platform: Platform
    ok: bool
    lines: list[CartLine] = Field(default_factory=list)
    item_total: Optional[float] = None
    total: Optional[float] = None    # what the app says you'll pay, fees and offers included
    charges: dict[str, float] = Field(default_factory=dict)   # e.g. {'delivery': 30, 'taxes': 18.5}
    cart_id: Optional[str] = None
    cart_url: Optional[str] = None
    message: str = ""
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)