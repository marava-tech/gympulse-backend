"""Saved meal library — CRUD."""
import logging
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from models.saved_meal import SavedMealCreate, SavedMealPatch
from utils import parse_object_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/saved-meals", tags=["saved-meals"])


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


@router.post("", status_code=201)
async def create_saved_meal(body: SavedMealCreate, user_id: str = Depends(get_current_user)):
    db = get_db()
    totals = {"calories_kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    for item in body.items:
        totals["calories_kcal"] += item.calories_kcal
        totals["protein_g"] += item.protein_g
        totals["carbs_g"] += item.carbs_g
        totals["fat_g"] += item.fat_g

    doc = {
        **body.model_dump(),
        "user_id": user_id,
        "totals": {k: round(v, 1) for k, v in totals.items()},
        "use_count": 0,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.saved_meals.insert_one(doc)
    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)
    return doc


@router.get("")
async def list_saved_meals(
    skip: int = 0, limit: int = 50, user_id: str = Depends(get_current_user)
):
    db = get_db()
    docs = await db.saved_meals.find({"user_id": user_id}).sort("use_count", -1).skip(skip).limit(limit).to_list(None)
    return [_serialize(d) for d in docs]


@router.patch("/{meal_id}")
async def update_saved_meal(
    meal_id: str, body: SavedMealPatch, user_id: str = Depends(get_current_user)
):
    db = get_db()
    oid = parse_object_id(meal_id, "meal_id")
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(400, "No fields provided")
    result = await db.saved_meals.update_one({"_id": oid, "user_id": user_id}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(404, "Saved meal not found")
    doc = await db.saved_meals.find_one({"_id": oid, "user_id": user_id})
    return _serialize(doc)


@router.delete("/{meal_id}", status_code=204)
async def delete_saved_meal(meal_id: str, user_id: str = Depends(get_current_user)):
    db = get_db()
    oid = parse_object_id(meal_id, "meal_id")
    result = await db.saved_meals.delete_one({"_id": oid, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(404, "Saved meal not found")


@router.post("/{meal_id}/use", status_code=201)
async def use_saved_meal(
    meal_id: str, meal_slot: str, date_str: str | None = None, user_id: str = Depends(get_current_user)
):
    """Log a saved meal directly to food_logs and increment use_count."""
    db = get_db()
    oid = parse_object_id(meal_id, "meal_id")
    meal = await db.saved_meals.find_one({"_id": oid, "user_id": user_id})
    if not meal:
        raise HTTPException(404, "Saved meal not found")

    now = datetime.now(timezone.utc)
    profile = await db.user_profile.find_one({"user_id": user_id})
    tz_name = (profile or {}).get("user_timezone", "UTC")
    try:
        user_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        user_tz = ZoneInfo("UTC")

    if date_str:
        try:
            food_date = date.fromisoformat(date_str).isoformat()
        except ValueError:
            raise HTTPException(400, "Invalid date, expected YYYY-MM-DD")
    else:
        food_date = now.astimezone(user_tz).date().isoformat()

    doc = {
        "user_id": user_id,
        "date": food_date,
        "meal_slot": meal_slot,
        "items": meal["items"],
        "image_url": meal.get("image_url"),
        "note": f"Saved meal: {meal['name']}",
        "totals": meal["totals"],
        "created_at": now,
    }
    result = await db.food_logs.insert_one(doc)
    await db.saved_meals.update_one({"_id": oid, "user_id": user_id}, {"$inc": {"use_count": 1}})
    doc["_id"] = str(result.inserted_id)
    return doc
