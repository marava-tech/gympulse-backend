"""Adaptive TDEE insight — GET /api/tdee/insight, POST /api/tdee/apply."""
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from services.adaptive_tdee import compute_real_tdee, detect_plateau, build_suggestion
from services.goal_history import snapshot_goal_history

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tdee", tags=["tdee"])

_WINDOW_DAYS = 21


async def _gather_data(db, user_id: str) -> tuple[dict, dict, list]:
    """Pull intake + weight data for the insight window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_WINDOW_DAYS)).date().isoformat()

    # Aggregate daily kcal from food_logs
    pipeline = [
        {"$match": {"user_id": user_id, "date": {"$gte": cutoff}}},
        {"$group": {"_id": "$date", "total_kcal": {"$sum": "$total_kcal"}}},
    ]
    intake_docs = await db.food_logs.aggregate(pipeline).to_list(None)
    intake_by_date = {d["_id"]: d["total_kcal"] for d in intake_docs}

    # Weight entries
    weight_docs = await db.weight_photos.find(
        {"user_id": user_id, "date": {"$gte": cutoff}},
        {"date": 1, "weight_kg": 1},
    ).to_list(None)
    weight_by_date = {
        d["date"]: d["weight_kg"]
        for d in weight_docs
        if d.get("weight_kg") is not None
    }

    # Last 4 ISO weeks for plateau detection
    week_pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%G-W%V", "date": {"$dateFromString": {"dateString": "$date"}}}},
            "avg_weight": {"$avg": "$weight_kg"},
        }},
        {"$sort": {"_id": 1}},
        {"$limit": 4},
    ]
    week_docs = await db.weight_photos.aggregate(week_pipeline).to_list(None)
    weekly_avgs = [{"week_start": d["_id"], "avg_weight": d["avg_weight"]} for d in week_docs]

    return intake_by_date, weight_by_date, weekly_avgs


@router.get("/insight")
async def get_tdee_insight(user_id: str = Depends(get_current_user)):
    """Return real-TDEE insight + suggestion for the current user."""
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    if not profile:
        raise HTTPException(404, "Profile not found")

    intake_by_date, weight_by_date, weekly_avgs = await _gather_data(db, user_id)
    insight = compute_real_tdee(intake_by_date, weight_by_date, window_days=_WINDOW_DAYS)
    is_plateau = detect_plateau(weekly_avgs)
    suggestion = build_suggestion(insight, profile, is_plateau=is_plateau)

    return {
        **insight,
        "suggestion": suggestion,
        "current_goal_kcal": profile.get("goal_kcal"),
        "real_tdee_kcal_stored": profile.get("real_tdee_kcal"),
        "tdee_source": profile.get("tdee_source", "formula"),
    }


@router.post("/apply")
async def apply_tdee_suggestion(user_id: str = Depends(get_current_user)):
    """Apply the suggested TDEE-derived goal to the user profile."""
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    if not profile:
        raise HTTPException(404, "Profile not found")

    intake_by_date, weight_by_date, weekly_avgs = await _gather_data(db, user_id)
    insight = compute_real_tdee(intake_by_date, weight_by_date, window_days=_WINDOW_DAYS)
    is_plateau = detect_plateau(weekly_avgs)
    suggestion = build_suggestion(insight, profile, is_plateau=is_plateau)

    if suggestion["type"] == "none" or suggestion["suggested_goal_kcal"] is None:
        raise HTTPException(400, "No actionable suggestion available right now.")

    new_goal = suggestion["suggested_goal_kcal"]
    real_tdee = insight.get("real_tdee_kcal")

    await db.user_profile.update_one(
        {"user_id": user_id},
        {"$set": {
            "goal_kcal": new_goal,
            "real_tdee_kcal": real_tdee,
            "tdee_source": "adaptive",
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    updated_profile = await db.user_profile.find_one({"user_id": user_id})
    await snapshot_goal_history(db, user_id, updated_profile)

    logger.info("User %s applied adaptive TDEE: goal=%s real_tdee=%s", user_id, new_goal, real_tdee)
    return {
        "goal_kcal": new_goal,
        "real_tdee_kcal": real_tdee,
        "tdee_source": "adaptive",
        "suggestion_type": suggestion["type"],
        "message": suggestion["message"],
    }
