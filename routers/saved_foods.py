"""Saved food library — individual items for quick reuse."""
import logging
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from database import get_db
from models.saved_food import SavedFoodCreate
from utils import parse_object_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/saved-foods", tags=["saved-foods"])


class _UseBody(BaseModel):
    meal_slot: str
    estimated_weight_g: Optional[float] = None
    date: Optional[str] = None


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


@router.get("")
async def list_saved_foods(
    skip: int = 0, limit: int = 100, user_id: str = Depends(get_current_user)
):
    db = get_db()
    docs = (
        await db.saved_foods.find({"user_id": user_id})
        .sort("use_count", -1)
        .skip(skip)
        .limit(limit)
        .to_list(None)
    )
    return [_serialize(d) for d in docs]


@router.post("", status_code=201)
async def create_saved_food(body: SavedFoodCreate, user_id: str = Depends(get_current_user)):
    db = get_db()
    doc = {
        **body.model_dump(),
        "user_id": user_id,
        "use_count": 0,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.saved_foods.insert_one(doc)
    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)
    return doc


@router.delete("/{food_id}", status_code=204)
async def delete_saved_food(food_id: str, user_id: str = Depends(get_current_user)):
    db = get_db()
    oid = parse_object_id(food_id, "food_id")
    result = await db.saved_foods.delete_one({"_id": oid, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(404, "Saved food not found")


@router.post("/{food_id}/use", status_code=201)
async def use_saved_food(
    food_id: str, body: _UseBody, user_id: str = Depends(get_current_user)
):
    """Log a saved food item to food_logs and increment use_count."""
    db = get_db()
    oid = parse_object_id(food_id, "food_id")
    food = await db.saved_foods.find_one({"_id": oid, "user_id": user_id})
    if not food:
        raise HTTPException(404, "Saved food not found")

    now = datetime.now(timezone.utc)
    profile = await db.user_profile.find_one({"user_id": user_id})
    tz_name = (profile or {}).get("user_timezone", "UTC")
    try:
        user_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        user_tz = ZoneInfo("UTC")

    if body.date:
        try:
            food_date = date.fromisoformat(body.date).isoformat()
        except ValueError:
            raise HTTPException(400, "Invalid date, expected YYYY-MM-DD")
    else:
        food_date = now.astimezone(user_tz).date().isoformat()

    # Scale macros proportionally if weight was overridden
    saved_weight = food["estimated_weight_g"]
    target_weight = body.estimated_weight_g if body.estimated_weight_g is not None else saved_weight
    scale = target_weight / saved_weight if saved_weight > 0 else 1.0

    item = {
        "name": food["name"],
        "estimated_weight_g": round(target_weight, 1),
        "calories_kcal": round(food["calories_kcal"] * scale, 1),
        "protein_g": round(food["protein_g"] * scale, 1),
        "carbs_g": round(food["carbs_g"] * scale, 1),
        "fat_g": round(food["fat_g"] * scale, 1),
        "macro_source": food.get("macro_source", "ai_estimated"),
    }

    log_doc = {
        "user_id": user_id,
        "date": food_date,
        "meal_slot": body.meal_slot,
        "items": [item],
        "image_url": food.get("image_url"),
        "note": f"Saved food: {food['name']}",
        "totals": {
            "calories_kcal": item["calories_kcal"],
            "protein_g": item["protein_g"],
            "carbs_g": item["carbs_g"],
            "fat_g": item["fat_g"],
        },
        "created_at": now,
    }
    result = await db.food_logs.insert_one(log_doc)
    await db.saved_foods.update_one(
        {"_id": oid, "user_id": user_id}, {"$inc": {"use_count": 1}}
    )
    log_doc["_id"] = str(result.inserted_id)
    return log_doc
