"""AI scan quota — status + rewarded-ad bonus."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db
from services import ai_quota

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.get("/quota")
async def get_quota(user_id: str = Depends(get_current_user)):
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    return await ai_quota.status(db, user_id, profile)


@router.post("/reward")
async def claim_reward(user_id: str = Depends(get_current_user)):
    """Call after the user finishes a rewarded ad: +1 scan for today."""
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    return await ai_quota.grant_reward(db, user_id, profile)
