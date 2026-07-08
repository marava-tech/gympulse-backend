"""Sleep quality logs — hours_slept → quality auto-derived."""
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, HTTPException
from auth import get_current_user
from database import get_db
from models.sleep_log import SleepLogCreate, SleepQuality
from utils import require_feature_enabled

router = APIRouter(prefix="/api/sleep-logs", tags=["sleep"])

_DEFAULT_THRESHOLDS = {
    "worst_max": 4.0,
    "bad_max": 6.0,
    "average_max": 7.0,
    "good_max": 8.0,
}


def _derive_quality(hours: float, thresholds: dict) -> SleepQuality:
    worst_max = thresholds.get("worst_max", 4.0)
    bad_max = thresholds.get("bad_max", 6.0)
    average_max = thresholds.get("average_max", 7.0)
    good_max = thresholds.get("good_max", 8.0)

    if hours < worst_max:
        return SleepQuality.worst
    if hours < bad_max:
        return SleepQuality.bad
    if hours < average_max:
        return SleepQuality.average
    if hours < good_max:
        return SleepQuality.good
    return SleepQuality.better


@router.post("", status_code=201)
async def log_sleep(body: SleepLogCreate, user_id: str = Depends(get_current_user)):
    # Validate hours_slept range
    if not (1.0 <= body.hours_slept <= 12.0):
        raise HTTPException(422, "hours_slept must be between 1 and 12")

    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    require_feature_enabled(profile, "sleep_tracking", "Sleep tracking")
    tz_name = (profile or {}).get("user_timezone", "UTC")
    try:
        user_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        user_tz = ZoneInfo("UTC")

    # Only allow logging for today or yesterday in the USER's timezone
    today = datetime.now(user_tz).date()
    yesterday = today - timedelta(days=1)
    if body.date not in (today.isoformat(), yesterday.isoformat()):
        raise HTTPException(422, "date must be today or yesterday")

    raw_thresholds = (profile or {}).get("sleep_thresholds", _DEFAULT_THRESHOLDS)
    thresholds = raw_thresholds if isinstance(raw_thresholds, dict) else _DEFAULT_THRESHOLDS

    quality = _derive_quality(body.hours_slept, thresholds)

    existing = await db.sleep_logs.find_one({"date": body.date, "user_id": user_id})
    if existing:
        await db.sleep_logs.update_one(
            {"date": body.date, "user_id": user_id},
            {"$set": {
                "hours_slept": body.hours_slept,
                "quality": quality.value,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        existing["hours_slept"] = body.hours_slept
        existing["quality"] = quality.value
        existing["_id"] = str(existing["_id"])
        return existing

    doc = {
        "date": body.date,
        "hours_slept": body.hours_slept,
        "quality": quality.value,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.sleep_logs.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return doc


@router.get("")
async def get_sleep_logs(
    days: int = 30,
    start: str | None = None,
    end: str | None = None,
    user_id: str = Depends(get_current_user),
):
    db = get_db()
    query: dict = {"user_id": user_id}
    if start or end:
        date_filter: dict = {}
        if start:
            date_filter["$gte"] = start
        if end:
            date_filter["$lte"] = end
        query["date"] = date_filter
    elif days > 0:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        query["date"] = {"$gte": cutoff}
    docs = await db.sleep_logs.find(query).sort("date", 1).to_list(None)
    for d in docs:
        d["_id"] = str(d["_id"])
    return {"logs": docs}


@router.get("/weekly-averages")
async def weekly_sleep_averages(user_id: str = Depends(get_current_user)):
    """Return per-ISO-week average sleep duration, grouped Mon–Sun, sorted ascending."""
    from collections import defaultdict
    from datetime import date as _date

    db = get_db()
    docs = await db.sleep_logs.find({"hours_slept": {"$ne": None}, "user_id": user_id}).sort("date", 1).to_list(None)

    week_data: dict[str, list[float]] = defaultdict(list)
    for doc in docs:
        try:
            d = _date.fromisoformat(doc["date"])
            monday = d - timedelta(days=d.weekday())
            week_data[monday.isoformat()].append(float(doc["hours_slept"]))
        except Exception:
            continue

    weeks = []
    for week_start in sorted(week_data.keys()):
        hours = week_data[week_start]
        weeks.append({
            "week_start": week_start,
            "avg_sleep": round(sum(hours) / len(hours), 1),
            "entries": len(hours),
        })

    return {"weeks": weeks[-4:]}
