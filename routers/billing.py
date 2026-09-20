"""RevenueCat webhook → entitlements."""
import hmac
import os

from fastapi import APIRouter, Header, HTTPException

from database import get_db
from services import entitlements

router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.post("/revenuecat", status_code=200)
async def revenuecat_webhook(body: dict, authorization: str | None = Header(None)):
    """Set the same value as REVENUECAT_WEBHOOK_AUTH in RevenueCat → Webhooks → Authorization header."""
    secret = os.environ.get("REVENUECAT_WEBHOOK_AUTH", "")
    if not secret or not hmac.compare_digest(authorization or "", secret):
        raise HTTPException(401, "Unauthorized")
    changed = await entitlements.apply_event(get_db(), body.get("event") or {})
    return {"ok": True, "changed": changed}
