"""Health Connect / Google Health sync state — tracks when the client's
manual "sync missed data" backfill last completed, so the next run only
covers the gap since then instead of re-scanning the user's full history."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/api/health-sync", tags=["health-sync"])


@router.get("/last-synced")
async def get_last_synced(user_id: str = Depends(get_current_user)):
    db = get_db()
    doc = await db.user_profile.find_one({"user_id": user_id}, {"health_last_synced_at": 1})
    if not doc:
        raise HTTPException(404, "Profile not found")
    last_synced = doc.get("health_last_synced_at")
    return {"last_synced_at": last_synced.isoformat() if last_synced else None}


@router.put("/last-synced")
async def set_last_synced(user_id: str = Depends(get_current_user)):
    db = get_db()
    now = datetime.now(timezone.utc)
    result = await db.user_profile.update_one(
        {"user_id": user_id}, {"$set": {"health_last_synced_at": now}}
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Profile not found")
    return {"last_synced_at": now.isoformat()}
