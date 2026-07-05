"""
Migration:
  1. Patch the existing user with an email (for OTP auth), remove password_hash.
  2. Add user_id to all documents in every collection.
  3. Drop bowls collection, remove bowl_id from food_logs.

Usage:
    MONGODB_URI="mongodb://..." USER_EMAIL="you@example.com" python scripts/migrate_add_user_id.py
"""
import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient


COLLECTIONS_TO_TAG = [
    "food_logs",
    "weight_photos",
    "body_photos",
    "body_photo_comparisons",
    "sleep_logs",
    "gym_sessions",
    "daily_checkins",
    "if_logs",
    "supplement_logs",
    "supplements",
    "saved_meals",
    "user_profile",
]


async def main():
    uri = os.environ["MONGODB_URI"]
    user_email = os.environ.get("USER_EMAIL", "").strip().lower()
    if not user_email:
        print("ERROR: USER_EMAIL environment variable is required. Aborting.")
        return

    client = AsyncIOMotorClient(uri)
    db = client["gympulse"]

    # Fetch the first user to get user_id
    first_user = await db.users.find_one({})
    if not first_user:
        print("ERROR: No users found in the 'users' collection. Aborting.")
        client.close()
        return

    user_id = str(first_user["_id"])
    print(f"Found existing user: {user_id} (username={first_user.get('username', '?')})")

    # Patch user: add email, remove password_hash
    await db.users.update_one(
        {"_id": first_user["_id"]},
        {
            "$set": {"email": user_email},
            "$unset": {"password_hash": ""},
        },
    )
    print(f"  users: set email={user_email}, removed password_hash")

    # Add user_id to all documents that don't already have it
    print(f"\nTagging documents with user_id={user_id} ...")
    for collection_name in COLLECTIONS_TO_TAG:
        col = db[collection_name]
        result = await col.update_many(
            {"user_id": {"$exists": False}},
            {"$set": {"user_id": user_id}},
        )
        print(f"  {collection_name}: updated {result.modified_count} documents")

    # Drop the bowls collection
    await db.drop_collection("bowls")
    print("\nDropped 'bowls' collection")

    # Remove bowl_id field from food_logs
    result = await db.food_logs.update_many(
        {"bowl_id": {"$exists": True}},
        {"$unset": {"bowl_id": ""}},
    )
    print(f"  food_logs: removed bowl_id from {result.modified_count} documents")

    client.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    asyncio.run(main())
