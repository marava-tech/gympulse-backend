import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    return _client


def get_db():
    return get_client()["fitness_os"]


async def ensure_indexes():
    db = get_db()
    # date indexes — used in almost every range query
    for collection in [
        "food_logs", "weight_photos", "sleep_logs",
        "gym_sessions", "daily_checkins", "if_logs", "supplement_logs",
    ]:
        await db[collection].create_index([("date", 1)], background=True)
    # supplement lookup by ID
    await db.supplement_logs.create_index([("supplement_id", 1)], background=True)
    # compound indexes for common query patterns
    await db.food_logs.create_index([("date", 1), ("meal_slot", 1)], background=True)
    await db.daily_checkins.create_index([("date", -1)], background=True)
    await db.gym_sessions.create_index([("photos.analysis", 1), ("date", 1)], background=True)
    await db.weight_photos.create_index([("photo_id", 1)], background=True)
    # user_id compound indexes for multi-user isolation
    await db.user_profile.create_index([("user_id", 1)], background=True, unique=True)
    await db.food_logs.create_index([("user_id", 1), ("date", 1)], background=True)
    await db.supplement_logs.create_index([("user_id", 1), ("supplement_id", 1)], background=True)
    await db.gym_sessions.create_index([("user_id", 1), ("date", 1)], background=True)
    await db.sleep_logs.create_index([("user_id", 1), ("date", 1)], background=True)
    await db.daily_checkins.create_index([("user_id", 1), ("date", 1)], background=True)
    await db.supplements.create_index([("user_id", 1)], background=True)
    await db.saved_meals.create_index([("user_id", 1)], background=True)
    await db.saved_foods.create_index([("user_id", 1), ("use_count", -1)], background=True)
    await db.weight_photos.create_index([("user_id", 1), ("date", 1)], background=True)
    await db.if_logs.create_index([("user_id", 1), ("date", 1)], background=True)
    # goal history — snapshot of calorie/macro goals effective from a given date
    await db.goal_history.create_index(
        [("user_id", 1), ("effective_date", 1)], background=True, unique=True
    )
    # sparse indexes for streak boolean filters
    await db.daily_checkins.create_index([("user_id", 1), ("gym", 1)], sparse=True, background=True)
    await db.daily_checkins.create_index([("user_id", 1), ("if_followed", 1)], sparse=True, background=True)
    # Food corrections — per-user per-food-name correction learning store
    await db.food_corrections.create_index(
        [("user_id", 1), ("name_norm", 1)], background=True, unique=True
    )
    # OTP expiry — MongoDB auto-deletes documents after expires_at
    await db.otp_requests.create_index("expires_at", expireAfterSeconds=0, background=True)
    await db.users.create_index("email", unique=True, sparse=True, background=True)
    logger.info("MongoDB indexes ensured")
