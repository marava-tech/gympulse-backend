"""
One-time backfill: seed a goal_history entry for every existing user_profile that
doesn't have one yet, so historical day-detail views (from before goal history was
tracked) show a frozen snapshot of the goals as they were at profile creation,
rather than silently falling through to "whatever the goal is today".

Usage:
    MONGODB_URI="mongodb://..." python scripts/backfill_goal_history.py
"""
import asyncio
import os
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from motor.motor_asyncio import AsyncIOMotorClient

GOAL_SNAPSHOT_FIELDS = (
    "goal_kcal", "protein_g", "carbs_g", "fat_g",
    "gym_goal_kcal", "gym_protein_g", "gym_carbs_g", "gym_fat_g",
    "rest_goal_kcal", "rest_protein_g", "rest_carbs_g", "rest_fat_g",
)


async def main():
    uri = os.environ["MONGODB_URI"]
    client = AsyncIOMotorClient(uri)
    db = client["fitness_os"]

    async for profile in db.user_profile.find({}):
        user_id = profile["user_id"]
        existing = await db.goal_history.find_one({"user_id": user_id})
        if existing:
            print(f"user {user_id}: already has goal_history, skipping")
            continue

        created_at = profile.get("created_at")
        tz_name = profile.get("user_timezone", "UTC")
        try:
            user_tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            user_tz = ZoneInfo("UTC")
        if created_at is not None:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            effective_date = created_at.astimezone(user_tz).date().isoformat()
        else:
            effective_date = "1970-01-01"

        snapshot = {field: profile.get(field) for field in GOAL_SNAPSHOT_FIELDS}
        await db.goal_history.update_one(
            {"user_id": user_id, "effective_date": effective_date},
            {"$set": {**snapshot, "user_id": user_id, "effective_date": effective_date}},
            upsert=True,
        )
        print(f"user {user_id}: seeded goal_history at {effective_date} with {snapshot}")

    client.close()
    print("\nBackfill complete.")


if __name__ == "__main__":
    asyncio.run(main())
