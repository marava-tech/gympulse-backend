"""Progress weight photos — single source of truth for all weight data."""
import logging
import uuid
from datetime import datetime, timezone, date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form

from auth import get_current_user
from database import get_db
from services import minio_client
from services.tdee import calculate_tdee
from services.fcm import send_notification
from services.goal_history import snapshot_goal_history
from utils import validate_image_upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/weight-photos", tags=["weight-photos"])


@router.post("", status_code=201)
async def upload_weight_photo(
    photo_date: str = Form(...),
    weight_kg: Optional[float] = Form(None),
    photo: Optional[UploadFile] = File(None),
    user_id: str = Depends(get_current_user),
):
    if weight_kg is None and (photo is None or photo.size == 0):
        raise HTTPException(status_code=422, detail="At least one of weight_kg or photo must be provided")

    db = get_db()
    image_url: Optional[str] = None

    if photo and photo.filename:
        image_bytes = await photo.read()
        validate_image_upload(image_bytes, photo.filename, photo.content_type)
        filename = f"{uuid.uuid4()}.jpg"
        image_url = await minio_client.upload_image(image_bytes, minio_client.BUCKET_GYM, filename)

    photo_id = str(uuid.uuid4())
    doc = {
        "photo_id": photo_id,
        "date": photo_date,
        "weight_kg": weight_kg,
        "image_url": image_url,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc),
    }
    await db.weight_photos.insert_one(doc)
    doc["_id"] = str(doc.pop("_id", photo_id))

    # Recalculate TDEE when weight is provided, but preserve any custom goal overrides.
    if weight_kg is not None:
        try:
            profile = await db.user_profile.find_one({"user_id": user_id})
            if profile:
                old_goal = profile.get("goal_kcal", 0)
                gym_days = profile.get("gym_days") or []
                tdee = calculate_tdee(weight_kg, profile["height_cm"], profile["age"], profile["sex"], gym_days_per_week=len(gym_days))

                # Restore user-overridden goal fields so a new weight log doesn't wipe them.
                overrides: set[str] = set(profile.get("goal_overrides") or [])
                for field in ("goal_kcal", "protein_g", "carbs_g", "fat_g"):
                    if field in overrides and field in profile:
                        tdee[field] = profile[field]

                await db.user_profile.update_one(
                    {"user_id": user_id}, {"$set": {**tdee, "weight_kg": weight_kg}}
                )
                updated_profile = await db.user_profile.find_one({"user_id": user_id})
                await snapshot_goal_history(db, user_id, updated_profile)
                if abs(tdee["goal_kcal"] - old_goal) > 50 and profile.get("fcm_token"):
                    try:
                        await send_notification(
                            profile["fcm_token"],
                            "Daily goal updated",
                            f"New weight {weight_kg}kg → {tdee['goal_kcal']} kcal/day",
                        )
                    except Exception as e:
                        logger.error("Failed to send weight update FCM: %s", e)
        except Exception as e:
            logger.error("TDEE recalculation failed: %s", e)

    return doc


@router.get("")
async def list_weight_photos(
    days: int = 0,
    limit: int = 50,
    user_id: str = Depends(get_current_user),
):
    db = get_db()
    query: dict = {"user_id": user_id}
    if days > 0:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        query["date"] = {"$gte": cutoff}
    docs = await db.weight_photos.find(query).sort("date", -1).limit(limit).to_list(None)
    for d in docs:
        d["_id"] = str(d["_id"])
    return {"photos": docs}


@router.get("/weekly-averages")
async def weekly_weight_averages(user_id: str = Depends(get_current_user)):
    """Return per-ISO-week average weight, grouped Mon–Sun, sorted ascending."""
    from collections import defaultdict
    from datetime import date as _date

    db = get_db()
    docs = await db.weight_photos.find({"weight_kg": {"$ne": None}, "user_id": user_id}).sort("date", 1).to_list(None)

    week_data: dict[str, list[float]] = defaultdict(list)
    for doc in docs:
        try:
            d = _date.fromisoformat(doc["date"])
            monday = d - timedelta(days=d.weekday())
            week_data[monday.isoformat()].append(float(doc["weight_kg"]))
        except Exception:
            continue

    weeks = []
    for week_start in sorted(week_data.keys()):
        weights = week_data[week_start]
        weeks.append({
            "week_start": week_start,
            "avg_weight": round(sum(weights) / len(weights), 1),
            "entries": len(weights),
        })

    return {"weeks": weeks[-4:]}


@router.delete("/{photo_id}", status_code=204)
async def delete_weight_photo(photo_id: str, user_id: str = Depends(get_current_user)):
    db = get_db()
    result = await db.weight_photos.delete_one({"photo_id": photo_id, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Weight photo not found")
