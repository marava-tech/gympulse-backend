"""Shared helper for snapshotting a user's calorie/macro goals per day.

Any code path that writes goal_kcal/protein_g/carbs_g/fat_g (or the gym/rest
variants) onto user_profile should call snapshot_goal_history() afterwards so
that history views (day-detail) show whatever goal was configured on a given
date, not whatever the goal happens to be right now.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

GOAL_SNAPSHOT_FIELDS = (
    "goal_kcal", "protein_g", "carbs_g", "fat_g",
    "gym_goal_kcal", "gym_protein_g", "gym_carbs_g", "gym_fat_g",
    "rest_goal_kcal", "rest_protein_g", "rest_carbs_g", "rest_fat_g",
)


def today_for_user(doc: dict) -> str:
    tz_name = (doc or {}).get("user_timezone", "UTC")
    try:
        user_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        user_tz = ZoneInfo("UTC")
    return datetime.now(timezone.utc).astimezone(user_tz).date().isoformat()


async def snapshot_goal_history(db, user_id: str, doc: dict) -> None:
    """Upsert today's goal snapshot so it becomes frozen history once the day rolls over."""
    effective_date = today_for_user(doc)
    snapshot = {field: doc.get(field) for field in GOAL_SNAPSHOT_FIELDS}
    await db.goal_history.update_one(
        {"user_id": user_id, "effective_date": effective_date},
        {"$set": {**snapshot, "user_id": user_id, "effective_date": effective_date}},
        upsert=True,
    )
