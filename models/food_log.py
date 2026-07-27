from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum


class MealSlot(str, Enum):
    breakfast = "breakfast"
    lunch = "lunch"
    dinner = "dinner"
    extras = "extras"
    supplement = "supplement"
    # Legacy values — kept for backward compatibility
    meal1 = "meal1"
    meal2 = "meal2"
    snack = "snack"


class MacroSource(str, Enum):
    database = "database"
    ai_estimated = "ai_estimated"


class CookingMethod(str, Enum):
    raw = "raw"
    boiled = "boiled"
    steamed = "steamed"
    grilled = "grilled"
    fried = "fried"
    curry = "curry"
    deep_fried = "deep_fried"


class SourceType(str, Enum):
    home = "home"
    restaurant = "restaurant"


class AiOriginalMacros(BaseModel):
    """Snapshot of the AI-estimated macros before the user edited them.
    Sent from the client at save time to enable the correction learning loop."""
    calories_kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float


class FoodItem(BaseModel):
    name: str
    estimated_weight_g: float
    calories_kcal: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    macro_source: MacroSource = MacroSource.ai_estimated
    cooking_method: Optional[CookingMethod] = None
    # AI estimate snapshot — present only when the client has an original AI estimate
    # to compare against. Used to capture corrections; stripped before persistence.
    ai_original: Optional[AiOriginalMacros] = None


class FoodLogCreate(BaseModel):
    meal_slot: MealSlot
    items: List[FoodItem]
    image_url: Optional[str] = None
    note: Optional[str] = None
    source_type: Optional[SourceType] = None
    # Calendar day (YYYY-MM-DD) the entry should be logged under, e.g. when the
    # user is adding food to a previous day from the food diary. Falls back to
    # the user's current local day when omitted.
    date: Optional[str] = None


class AnalyzeRequest(BaseModel):
    # For when analysis result comes back and needs macro lookup
    pass


class ItemEstimateRequest(BaseModel):
    """Re-estimate a single item's macros after the user sets cooking method / source type
    on the review screen (the initial /analyze pass runs before that context is known)."""
    name: str
    estimated_weight_g: float
    cooking_method: Optional[CookingMethod] = None
    source_type: Optional[SourceType] = None


class DescribeFoodRequest(BaseModel):
    """Free-text food description to be parsed into items + macros, e.g.
    '100g cooked chicken breast with 2 tsp oil'."""
    text: str
    source_type: Optional[SourceType] = None


class AnalyzedItem(BaseModel):
    name: str
    estimated_weight_g: float
    source: str = "ai"


class FoodAnalysisResponse(BaseModel):
    items: List[AnalyzedItem]
    scale_weight_g: Optional[float] = None
    image_url: str
