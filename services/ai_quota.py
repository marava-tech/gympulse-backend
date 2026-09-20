"""Per-user daily quota for AI scans that run on the server's OpenRouter key.

Users who saved their own OpenRouter key are exempt (they pay for their own calls).
Extra scans are earned via rewarded ads (POST /api/ai/reward), capped per day.
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from typing import Optional

from fastapi import Depends, Form, HTTPException

from auth import get_current_user
from database import get_db

FREE_DAILY = int(os.environ.get("AI_FREE_SCANS_PER_DAY", "3"))
PREMIUM_DAILY = int(os.environ.get("AI_PREMIUM_SCANS_PER_DAY", "25"))
MAX_REWARDS_PER_DAY = int(os.environ.get("AI_MAX_REWARDS_PER_DAY", "5"))


def has_own_key(profile: dict | None) -> bool:
    return bool((profile or {}).get("openrouter_api_key"))


async def _premium(db, user_id: str) -> bool:
    # local import: entitlements imports this module (has_own_key)
    from services.entitlements import is_premium
    return await is_premium(db, user_id)


def _today(profile: dict | None) -> str:
    try:
        tz = ZoneInfo((profile or {}).get("user_timezone") or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date().isoformat()


async def _ensure_doc(db, user_id: str, day: str) -> None:
    await db.ai_usage.update_one(
        {"user_id": user_id, "date": day},
        {"$setOnInsert": {
            "used": 0, "bonus": 0, "rewards": 0,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=3),
        }},
        upsert=True,
    )


async def status(db, user_id: str, profile: dict | None) -> dict:
    day = _today(profile)
    await _ensure_doc(db, user_id, day)
    doc = await db.ai_usage.find_one({"user_id": user_id, "date": day})
    premium = await _premium(db, user_id)
    limit = (PREMIUM_DAILY if premium else FREE_DAILY) + doc["bonus"]
    return {
        "premium": premium,
        "unlimited": has_own_key(profile),
        "limit": limit,
        "used": doc["used"],
        "remaining": max(limit - doc["used"], 0),
        "rewards_left": max(MAX_REWARDS_PER_DAY - doc["rewards"], 0),
    }


async def consume(db, user_id: str, profile: dict | None) -> None:
    """Take one scan from today's quota or raise 429 (structured detail the app keys off)."""
    if has_own_key(profile):
        return
    day = _today(profile)
    await _ensure_doc(db, user_id, day)
    base = PREMIUM_DAILY if await _premium(db, user_id) else FREE_DAILY
    res = await db.ai_usage.update_one(
        {"user_id": user_id, "date": day,
         "$expr": {"$lt": ["$used", {"$add": [base, "$bonus"]}]}},
        {"$inc": {"used": 1}},
    )
    if res.modified_count == 0:
        st = await status(db, user_id, profile)
        raise HTTPException(429, detail={
            "code": "ai_quota_exhausted",
            "message": "You've used today's free AI scans.",
            "limit": st["limit"],
            "rewards_left": st["rewards_left"],
        })


async def refund(db, user_id: str, profile: dict | None) -> None:
    """Give the scan back when the AI call itself failed."""
    if has_own_key(profile):
        return
    await db.ai_usage.update_one(
        {"user_id": user_id, "date": _today(profile), "used": {"$gt": 0}},
        {"$inc": {"used": -1}},
    )


async def grant_reward(db, user_id: str, profile: dict | None) -> dict:
    # ponytail: trusts the client after a rewarded ad; per-day cap bounds abuse.
    # Upgrade to AdMob server-side verification (SSV) if abuse shows up.
    if await _premium(db, user_id):
        raise HTTPException(400, "Premium has no ads — no bonus scans needed")
    day = _today(profile)
    await _ensure_doc(db, user_id, day)
    res = await db.ai_usage.update_one(
        {"user_id": user_id, "date": day, "rewards": {"$lt": MAX_REWARDS_PER_DAY}},
        {"$inc": {"bonus": 1, "rewards": 1}},
    )
    if res.modified_count == 0:
        raise HTTPException(429, "No more bonus scans available today")
    return await status(db, user_id, profile)


async def _take(user_id: str, count: bool):
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    if count:
        await consume(db, user_id, profile)
    return db, profile


# Dependencies: take a scan up front, give it back if the handler raises.
async def scan_gate(user_id: str = Depends(get_current_user)):
    db, profile = await _take(user_id, True)
    try:
        yield
    except Exception:
        await refund(db, user_id, profile)
        raise


async def photo_scan_gate(
    model: Optional[str] = Form(None),
    user_id: str = Depends(get_current_user),
):
    # A retry with a different model re-scans the same photo, so it's free.
    count = not model
    db, profile = await _take(user_id, count)
    try:
        yield
    except Exception:
        if count:
            await refund(db, user_id, profile)
        raise
