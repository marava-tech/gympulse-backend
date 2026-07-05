"""User profile — create, read, update + TDEE calculation."""
import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from auth import get_current_user
from database import get_db
from models.profile import ProfileCreate, ProfilePatch
from services.tdee import calculate_tdee
from services.fcm import send_notification
from services import minio_client
from services.goal_history import snapshot_goal_history
from utils import validate_image_upload

router = APIRouter(prefix="/api/profile", tags=["profile"])


def _tdee_dict(doc: dict) -> dict:
    gym_days = doc.get("gym_days") or []
    return calculate_tdee(
        doc["weight_kg"], doc["height_cm"], doc["age"], doc["sex"],
        gym_days_per_week=len(gym_days),
    )


@router.post("", status_code=201)
async def create_profile(body: ProfileCreate, user_id: str = Depends(get_current_user)):
    db = get_db()
    existing = await db.user_profile.find_one({"user_id": user_id})
    if existing:
        raise HTTPException(400, "Profile already exists — use PATCH to update")

    gym_days = body.gym_days or []
    tdee = calculate_tdee(body.weight_kg, body.height_cm, body.age, body.sex, gym_days_per_week=len(gym_days))
    doc = {
        **body.model_dump(),
        **tdee,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "fcm_token": None,
        "streaks": {},
    }
    result = await db.user_profile.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    await snapshot_goal_history(db, user_id, doc)
    return doc


@router.get("")
async def get_profile(user_id: str = Depends(get_current_user)):
    db = get_db()
    doc = await db.user_profile.find_one({"user_id": user_id})
    if not doc:
        raise HTTPException(404, "Profile not found")
    doc["_id"] = str(doc["_id"])
    return doc


@router.patch("")
async def patch_profile(body: ProfilePatch, user_id: str = Depends(get_current_user)):
    db = get_db()
    update_data = dict(body.model_dump(exclude_unset=True))
    if not update_data:
        raise HTTPException(400, "No fields provided")

    doc = await db.user_profile.find_one({"user_id": user_id})
    if not doc:
        raise HTTPException(404, "Profile not found")

    merged = {**doc, **update_data}
    gym_days = merged.get("gym_days") or []
    tdee = calculate_tdee(
        merged["weight_kg"], merged["height_cm"], merged["age"], merged["sex"],
        gym_days_per_week=len(gym_days),
    )

    # Preserve user-overridden macro/calorie goals across any PATCH.
    # - If the user is explicitly setting a goal field NOW  → use that value and mark it overridden.
    # - If the user previously overrode a goal field (stored in doc["goal_overrides"])
    #   and is NOT changing it this time → restore the saved value so TDEE recalc doesn't wipe it.
    macro_fields = ("goal_kcal", "protein_g", "carbs_g", "fat_g")
    existing_overrides: set[str] = set(doc.get("goal_overrides") or [])

    for field in macro_fields:
        if field in update_data:
            # Explicit override in this request — honour it and record it.
            tdee[field] = update_data[field]
            existing_overrides.add(field)
        elif field in existing_overrides:
            # Previously overridden — restore saved value so TDEE doesn't reset it.
            if field in doc:
                tdee[field] = doc[field]

    # Persist the current set of overridden fields.
    update_data["goal_overrides"] = list(existing_overrides)

    old_goal = doc.get("goal_kcal", 0)
    new_goal = tdee["goal_kcal"]
    if abs(new_goal - old_goal) > 50 and doc.get("fcm_token"):
        try:
            await send_notification(
                doc["fcm_token"],
                "Goal Updated",
                f"Your daily calorie goal changed to {new_goal} kcal",
            )
        except Exception as e:
            logger.error("Failed to send goal update FCM: %s", e)

    update_data.update(tdee)
    update_data["updated_at"] = datetime.now(timezone.utc)

    await db.user_profile.update_one({"user_id": user_id}, {"$set": update_data})
    updated = await db.user_profile.find_one({"user_id": user_id})
    await snapshot_goal_history(db, user_id, updated)
    updated["_id"] = str(updated["_id"])
    return updated


@router.post("/photo")
async def upload_profile_photo(
    photo: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    """Upload profile picture via upload-api; stores URL in user_profile.photo_url."""
    db = get_db()
    image_bytes = await photo.read()
    validate_image_upload(image_bytes, photo.filename or "", photo.content_type)

    filename = f"profile_{uuid.uuid4()}.jpg"
    photo_url = await minio_client.upload_image(image_bytes, minio_client.BUCKET_PROFILE, filename)

    await db.user_profile.update_one({"user_id": user_id}, {"$set": {"photo_url": photo_url}})
    return {"photo_url": photo_url}
