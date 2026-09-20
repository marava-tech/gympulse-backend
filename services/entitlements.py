"""Premium entitlement, kept in sync from RevenueCat webhooks.

RevenueCat app_user_id == our user_id (the app calls Purchases.logIn(user_id)).
Truth is `expires_at`, so a missed EXPIRATION webhook self-heals.
"""
import os
from datetime import datetime, timezone

from fastapi import Depends, HTTPException

from auth import get_current_user
from database import get_db
from services import ai_quota

ENTITLEMENT = os.environ.get("REVENUECAT_ENTITLEMENT", "premium")

# Events that (re)grant access until event.expiration_at_ms. CANCELLATION and BILLING_ISSUE
# keep access until that same expiry, so they take this path too; EXPIRATION ends it now.
_GRANT = {
    "INITIAL_PURCHASE", "RENEWAL", "PRODUCT_CHANGE", "UNCANCELLATION",
    "NON_RENEWING_PURCHASE", "CANCELLATION", "BILLING_ISSUE", "SUBSCRIPTION_EXTENDED",
}


async def is_premium(db, user_id: str) -> bool:
    doc = await db.entitlements.find_one({"user_id": user_id})
    if not doc or not doc.get("expires_at"):
        return False
    exp = doc["expires_at"]
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


async def apply_event(db, event: dict) -> bool:
    """Apply one RevenueCat event. Returns True if it changed an entitlement."""
    user_id = event.get("app_user_id") or ""
    if not user_id or user_id.startswith("$RCAnonymousID"):
        return False  # purchase made before the app logged in — RevenueCat re-sends after logIn
    ents = event.get("entitlement_ids") or ([event["entitlement_id"]] if event.get("entitlement_id") else [])
    if ents and ENTITLEMENT not in ents:
        return False
    kind = event.get("type")
    exp_ms = event.get("expiration_at_ms")
    if kind == "EXPIRATION":
        expires_at = datetime.now(timezone.utc)
    elif kind in _GRANT and exp_ms:
        expires_at = datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc)
    else:
        return False
    await db.entitlements.update_one(
        {"user_id": user_id},
        {"$set": {
            "expires_at": expires_at,
            "product_id": event.get("product_id"),
            "last_event": kind,
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    return True


async def require_premium(user_id: str = Depends(get_current_user)) -> None:
    """Dependency for premium-only endpoints. Users paying for their own AI key are exempt."""
    db = get_db()
    if await is_premium(db, user_id):
        return
    profile = await db.user_profile.find_one({"user_id": user_id})
    if ai_quota.has_own_key(profile):
        return
    raise HTTPException(403, detail={
        "code": "premium_required",
        "message": "This is a Premium feature.",
    })
