"""Gym session tracking — attendance, photos, body analysis."""
import logging
import re
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from bson import ObjectId
from typing import Optional

from auth import get_current_user
from database import get_db
from models.gym_session import GymSessionCreate, PhotoAngle
from services import minio_client
from services.streak_calc import calculate_weekly_gym_streak, consecutive_gym_days_with_skip
from utils import parse_object_id, validate_image_upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gym-sessions", tags=["gym"])

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


@router.post("", status_code=201)
async def create_session(body: GymSessionCreate, user_id: str = Depends(get_current_user)):
    db = get_db()
    doc = {
        **body.model_dump(),
        "user_id": user_id,
        "photos": [],
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.gym_sessions.insert_one(doc)
    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)

    await _sync_gym_streak(user_id, db)

    return doc


@router.get("")
async def list_sessions(month: str, user_id: str = Depends(get_current_user)):
    """month format: YYYY-MM"""
    if not MONTH_RE.match(month):
        raise HTTPException(status_code=400, detail="month must be in YYYY-MM format")
    db = get_db()
    docs = await db.gym_sessions.find({"date": {"$regex": f"^{re.escape(month)}"}, "user_id": user_id}).to_list(None)
    return [_serialize(d) for d in docs]


@router.post("/latest/photos", status_code=201)
async def upload_latest_photo(
    angle: PhotoAngle = Form(...),
    photo: UploadFile = File(...),
    photo_date: Optional[str] = Form(None),
    user_id: str = Depends(get_current_user),
):
    db = get_db()
    target_date = photo_date or datetime.now(timezone.utc).date().isoformat()
    session = await db.gym_sessions.find_one({"date": target_date, "user_id": user_id})
    if not session:
        session_doc = {
            "date": target_date,
            "workout_type": "other",
            "attended": True,
            "notes": None,
            "user_id": user_id,
            "photos": [],
            "created_at": datetime.now(timezone.utc),
        }
        result = await db.gym_sessions.insert_one(session_doc)
        session_id = str(result.inserted_id)
    else:
        session_id = str(session["_id"])

    image_bytes = await photo.read()
    validate_image_upload(image_bytes, photo.filename or "", photo.content_type)
    filename = f"{uuid.uuid4()}.jpg"
    image_url = await minio_client.upload_image(image_bytes, minio_client.BUCKET_GYM, filename)

    photo_id = str(uuid.uuid4())
    photo_doc = {
        "photo_id": photo_id,
        "angle": angle.value,
        "image_url": image_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    await db.gym_sessions.update_one(
        {"_id": ObjectId(session_id)},
        {"$push": {"photos": photo_doc}},
    )
    return photo_doc


@router.post("/{session_id}/photos", status_code=201)
async def upload_photo(
    session_id: str,
    angle: PhotoAngle = Form(...),
    photo: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    db = get_db()
    oid = parse_object_id(session_id, "session_id")
    session = await db.gym_sessions.find_one({"_id": oid, "user_id": user_id})
    if not session:
        raise HTTPException(404, "Session not found")

    image_bytes = await photo.read()
    validate_image_upload(image_bytes, photo.filename or "", photo.content_type)
    filename = f"{uuid.uuid4()}.jpg"
    image_url = await minio_client.upload_image(image_bytes, minio_client.BUCKET_GYM, filename)

    photo_id = str(uuid.uuid4())
    photo_doc = {
        "photo_id": photo_id,
        "angle": angle.value,
        "image_url": image_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    await db.gym_sessions.update_one(
        {"_id": oid},
        {"$push": {"photos": photo_doc}},
    )

    return photo_doc



async def _sync_gym_streak(user_id: str, db):
    profile = await db.user_profile.find_one({"user_id": user_id})
    min_days = (profile or {}).get("gym_streak_min_days_per_week", 5)
    user_tz = (profile or {}).get("user_timezone", "UTC")
    docs = await db.daily_checkins.find({"gym": True, "user_id": user_id}, {"date": 1}).to_list(None)
    gym_date_list = [d["date"] for d in docs]
    weekly = await calculate_weekly_gym_streak(gym_date_list, min_days, user_tz)
    current_days, best_days = await consecutive_gym_days_with_skip(gym_date_list, max_skip=2, user_tz=user_tz)
    weekly["current_days"] = current_days
    weekly["best_days"] = best_days
    await db.user_profile.update_one(
        {"user_id": user_id}, {"$set": {"streaks.gym_weekly": weekly}}
    )


