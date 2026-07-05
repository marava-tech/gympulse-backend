import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import OperationFailure

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None

# IndexOptionsConflict / IndexKeySpecsConflict — raised when an index with the
# same auto-generated name already exists with different options (e.g. we
# added `unique=True` to a previously non-unique index).
_INDEX_CONFLICT_CODES = (85, 86)


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    return _client


def get_db():
    return get_client()["gympulse"]


async def _create_index(collection, keys, **kwargs):
    """create_index that recovers when a same-named index already exists with
    different options, by dropping and recreating it with the new options."""
    try:
        await collection.create_index(keys, **kwargs)
    except OperationFailure as e:
        if e.code not in _INDEX_CONFLICT_CODES:
            raise
        name = kwargs.get("name") or "_".join(f"{k}_{v}" for k, v in keys)
        logger.warning("Recreating conflicting index %s on %s", name, collection.name)
        await collection.drop_index(name)
        await collection.create_index(keys, **kwargs)


async def ensure_indexes():
    db = get_db()
    # date indexes — used in almost every range query
    for collection in [
        "food_logs", "weight_photos", "sleep_logs",
        "gym_sessions", "daily_checkins", "if_logs", "supplement_logs",
    ]:
        await _create_index(db[collection], [("date", 1)], background=True)
    # supplement lookup by ID
    await _create_index(db.supplement_logs, [("supplement_id", 1)], background=True)
    # compound indexes for common query patterns
    await _create_index(db.food_logs, [("date", 1), ("meal_slot", 1)], background=True)
    await _create_index(db.daily_checkins, [("date", -1)], background=True)
    await _create_index(db.gym_sessions, [("photos.analysis", 1), ("date", 1)], background=True)
    await _create_index(db.weight_photos, [("photo_id", 1)], background=True)
    # user_id compound indexes for multi-user isolation
    await _create_index(db.user_profile, [("user_id", 1)], background=True, unique=True)
    await _create_index(db.food_logs, [("user_id", 1), ("date", 1)], background=True)
    await _create_index(db.supplement_logs, [("user_id", 1), ("supplement_id", 1)], background=True)
    await _create_index(db.gym_sessions, [("user_id", 1), ("date", 1)], background=True)
    await _create_index(db.sleep_logs, [("user_id", 1), ("date", 1)], background=True)
    await _create_index(db.daily_checkins, [("user_id", 1), ("date", 1)], background=True)
    await _create_index(db.supplements, [("user_id", 1)], background=True)
    await _create_index(db.saved_meals, [("user_id", 1)], background=True)
    await _create_index(db.saved_foods, [("user_id", 1), ("use_count", -1)], background=True)
    await _create_index(db.weight_photos, [("user_id", 1), ("date", 1)], background=True)
    await _create_index(db.if_logs, [("user_id", 1), ("date", 1)], background=True, unique=True)
    # goal history — snapshot of calorie/macro goals effective from a given date
    await _create_index(
        db.goal_history, [("user_id", 1), ("effective_date", 1)], background=True, unique=True
    )
    # sparse indexes for streak boolean filters
    await _create_index(db.daily_checkins, [("user_id", 1), ("gym", 1)], sparse=True, background=True)
    await _create_index(db.daily_checkins, [("user_id", 1), ("if_followed", 1)], sparse=True, background=True)
    # Food corrections — per-user per-food-name correction learning store
    await _create_index(
        db.food_corrections, [("user_id", 1), ("name_norm", 1)], background=True, unique=True
    )
    # OTP expiry — MongoDB auto-deletes documents after expires_at
    await _create_index(db.otp_requests, "expires_at", expireAfterSeconds=0, background=True)
    await _create_index(db.users, "email", unique=True, sparse=True, background=True)
    logger.info("MongoDB indexes ensured")
