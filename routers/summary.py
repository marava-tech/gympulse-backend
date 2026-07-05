"""Summary aggregation — daily and weekly views."""
import asyncio
import logging
from datetime import date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from auth import get_current_user
from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/summary", tags=["summary"])


async def _goal_for_date(db, user_id: str, date: str, gym_attended: bool) -> dict:
    """Resolve the calorie/macro goal that was configured as of `date`.

    Looks up the most recent goal_history snapshot on or before `date`. Falls back to
    the live profile for users/dates that predate goal history tracking.
    """
    entry = await db.goal_history.find_one(
        {"user_id": user_id, "effective_date": {"$lte": date}},
        sort=[("effective_date", -1)],
    )
    if entry is None:
        entry = await db.user_profile.find_one({"user_id": user_id}) or {}

    def pick(base_field: str, gym_field: str, rest_field: str):
        value = entry.get(gym_field if gym_attended else rest_field)
        if value is None:
            value = entry.get(base_field, 0)
        return value

    return {
        "calories_kcal": pick("goal_kcal", "gym_goal_kcal", "rest_goal_kcal"),
        "protein_g": pick("protein_g", "gym_protein_g", "rest_protein_g"),
        "carbs_g": pick("carbs_g", "gym_carbs_g", "rest_carbs_g"),
        "fat_g": pick("fat_g", "gym_fat_g", "rest_fat_g"),
    }


@router.get("")
async def get_day_summary(date: str, user_id: str = Depends(get_current_user)):
    """date format: YYYY-MM-DD — returns all tracked data for that day."""
    db = get_db()

    food_docs, sleep_doc, gym_session, checkin, weight_doc = await asyncio.gather(
        db.food_logs.find({"date": date, "user_id": user_id}).to_list(None),
        db.sleep_logs.find_one({"date": date, "user_id": user_id}),
        db.gym_sessions.find_one({"date": date, "attended": True, "user_id": user_id}),
        db.daily_checkins.find_one({"date": date, "user_id": user_id}),
        db.weight_photos.find_one({"date": date, "weight_kg": {"$ne": None}, "user_id": user_id}),
    )

    food_totals = {"calories_kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    for doc in food_docs:
        t = doc.get("totals", {})
        for k in food_totals:
            food_totals[k] += t.get(k, 0)
    food_summary = {k: round(v, 1) for k, v in food_totals.items()}
    food_summary["entries_count"] = len(food_docs)

    sleep_summary = None
    if sleep_doc:
        sleep_summary = {
            "hours_slept": sleep_doc.get("hours_slept"),
            "quality": sleep_doc.get("quality"),
        }

    gym_attended = bool(gym_session) or bool((checkin or {}).get("gym"))
    workout_type = None
    gym_notes = None
    gym_photos = []
    if gym_session:
        workout_type = gym_session.get("workout_type")
        gym_notes = gym_session.get("notes")
        gym_photos = gym_session.get("photos") or []
    elif checkin:
        workout_type = checkin.get("workout_type")

    supp_names = []
    if checkin:
        for entry in checkin.get("supplement_entries") or []:
            if entry.get("taken"):
                supp_names.append({"name": entry["name"], "count": entry.get("quantity"), "unit": entry.get("unit")})
    weight_summary = {"weight_kg": weight_doc["weight_kg"]} if weight_doc else None

    if_followed = (checkin or {}).get("if_followed")
    if_summary = {"adhered": if_followed} if if_followed is not None else None

    goals = await _goal_for_date(db, user_id, date, gym_attended)

    return {
        "date": date,
        "food": food_summary,
        "sleep": sleep_summary,
        "gym": {
            "attended": gym_attended,
            "workout_type": workout_type,
            "notes": gym_notes,
            "photos": gym_photos,
        },
        "supplements": supp_names,
        "weight": weight_summary,
        "if_log": if_summary,
        "goals": goals,
    }


def _week_dates(week_str: str) -> tuple[str, str]:
    """Parse YYYY-WW and return (monday_date, sunday_date) as YYYY-MM-DD strings."""
    year, week = map(int, week_str.split("-W"))
    monday = date.fromisocalendar(year, week, 1)
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


@router.get("/weekly")
async def weekly_summary(week: str, user_id: str = Depends(get_current_user)):
    """week format: YYYY-WN (e.g. 2026-W21)"""
    try:
        # Normalize: "YYYY-WW" or "YYYY-WN"
        if "-W" not in week:
            raise ValueError("missing -W separator")
        start_date, end_date = _week_dates(week)
    except (ValueError, TypeError) as e:
        logger.debug("Invalid week param '%s': %s", week, e)
        raise HTTPException(400, "Invalid week format. Use YYYY-WN (e.g. 2026-W21)")

    db = get_db()
    date_filter = {"date": {"$gte": start_date, "$lte": end_date}, "user_id": user_id}

    # Food logs
    food_logs = await db.food_logs.find(date_filter).to_list(None)
    days_logged = len({l["date"] for l in food_logs})
    avg_cal = avg_protein = avg_carbs = avg_fat = 0.0
    if food_logs:
        days_with_food = {l["date"] for l in food_logs}
        day_totals: dict[str, dict] = {}
        for log in food_logs:
            d = log["date"]
            if d not in day_totals:
                day_totals[d] = {"cal": 0, "protein": 0, "carbs": 0, "fat": 0}
            t = log.get("totals", {})
            day_totals[d]["cal"] += t.get("calories_kcal", 0)
            day_totals[d]["protein"] += t.get("protein_g", 0)
            day_totals[d]["carbs"] += t.get("carbs_g", 0)
            day_totals[d]["fat"] += t.get("fat_g", 0)
        n = len(day_totals)
        avg_cal = round(sum(v["cal"] for v in day_totals.values()) / n, 1)
        avg_protein = round(sum(v["protein"] for v in day_totals.values()) / n, 1)
        avg_carbs = round(sum(v["carbs"] for v in day_totals.values()) / n, 1)
        avg_fat = round(sum(v["fat"] for v in day_totals.values()) / n, 1)

    # Gym sessions
    gym_sessions = await db.gym_sessions.find({**date_filter, "attended": True}).to_list(None)
    gym_days = len(gym_sessions)

    # IF adherence
    if_logs = await db.if_logs.find(date_filter).to_list(None)
    if_adhered = sum(1 for l in if_logs if l.get("adhered", False))
    if_total = len(if_logs)

    # Sleep breakdown
    sleep_logs = await db.sleep_logs.find(date_filter).to_list(None)
    sleep_breakdown = {}
    for log in sleep_logs:
        q = log.get("quality", "unknown")
        sleep_breakdown[q] = sleep_breakdown.get(q, 0) + 1

    # Weight delta
    weight_logs = await db.weight_photos.find({**date_filter, "weight_kg": {"$ne": None}}).sort("date", 1).to_list(None)
    weight_delta = None
    if len(weight_logs) >= 2:
        weight_delta = round(weight_logs[-1]["weight_kg"] - weight_logs[0]["weight_kg"], 2)

    # Best meal photo (highest calorie entry with image)
    best_photo = None
    for log in sorted(food_logs, key=lambda l: l.get("totals", {}).get("calories_kcal", 0), reverse=True):
        if log.get("image_url"):
            best_photo = log["image_url"]
            break

    profile = await db.user_profile.find_one({"user_id": user_id})
    goal_kcal = profile.get("goal_kcal", 0) if profile else 0

    return {
        "week": week,
        "start_date": start_date,
        "end_date": end_date,
        "nutrition": {
            "avg_calories_kcal": avg_cal,
            "avg_protein_g": avg_protein,
            "avg_carbs_g": avg_carbs,
            "avg_fat_g": avg_fat,
            "goal_kcal": goal_kcal,
            "days_logged": days_logged,
        },
        "gym": {"sessions": gym_days},
        "intermittent_fasting": {"adhered_days": if_adhered, "total_logged_days": if_total},
        "sleep": {"breakdown": sleep_breakdown, "days_logged": len(sleep_logs)},
        "weight_delta_kg": weight_delta,
        "best_meal_photo_url": best_photo,
    }
