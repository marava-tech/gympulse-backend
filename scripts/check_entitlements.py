"""Self-check for premium entitlements. MONGODB_URI=mongodb://127.0.0.1:27099 python -m scripts.check_entitlements"""
import asyncio
import os
import time

os.environ.setdefault("JWT_SECRET", "test")
os.environ["AI_FREE_SCANS_PER_DAY"] = "3"
os.environ["AI_PREMIUM_SCANS_PER_DAY"] = "5"
os.environ["REVENUECAT_WEBHOOK_AUTH"] = "s3cret"

from fastapi import HTTPException  # noqa: E402

from database import get_db  # noqa: E402
from routers import billing  # noqa: E402
from services import ai_quota, entitlements  # noqa: E402


def ev(kind, user="u1", days=30, **kw):
    e = {"type": kind, "app_user_id": user, "entitlement_ids": ["premium"],
         "product_id": "snapfit_monthly", "expiration_at_ms": int((time.time() + days * 86400) * 1000)}
    e.update(kw)
    return {"event": e}


async def raises(coro, status):
    try:
        await coro
    except HTTPException as e:
        assert e.status_code == status, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTP {status}")


async def main():
    db = get_db()
    for c in ("entitlements", "ai_usage", "user_profile"):
        await db[c].delete_many({})

    # webhook auth
    await raises(billing.revenuecat_webhook(ev("RENEWAL"), authorization=None), 401)
    await raises(billing.revenuecat_webhook(ev("RENEWAL"), authorization="wrong"), 401)
    assert (await billing.revenuecat_webhook(ev("INITIAL_PURCHASE"), authorization="s3cret"))["changed"]
    assert await entitlements.is_premium(db, "u1")

    # ignored events
    assert not await entitlements.apply_event(db, ev("RENEWAL", user="$RCAnonymousID:abc")["event"])
    assert not await entitlements.apply_event(db, ev("RENEWAL", user="u9", entitlement_ids=["other"])["event"])
    assert not await entitlements.apply_event(db, {"type": "TEST", "app_user_id": "u9"})
    assert not await entitlements.is_premium(db, "u9")

    # premium quota tier: 5/day (not 3), no reward scans
    profile = None
    for _ in range(5):
        await ai_quota.consume(db, "u1", profile)
    await raises(ai_quota.consume(db, "u1", profile), 429)
    st = await ai_quota.status(db, "u1", profile)
    assert st["premium"] and st["limit"] == 5, st
    await raises(ai_quota.grant_reward(db, "u1", profile), 400)

    # cancellation keeps access until expiry; expiration ends it; lapsed expires_at self-heals
    await entitlements.apply_event(db, ev("CANCELLATION")["event"])
    assert await entitlements.is_premium(db, "u1")
    await entitlements.apply_event(db, ev("EXPIRATION")["event"])
    assert not await entitlements.is_premium(db, "u1")
    await entitlements.apply_event(db, ev("RENEWAL", days=-1)["event"])
    assert not await entitlements.is_premium(db, "u1"), "past expiry must not count"

    # require_premium gate
    e = await raises(entitlements.require_premium("u2"), 403)
    assert e.detail["code"] == "premium_required"
    await db.user_profile.insert_one({"user_id": "u3", "openrouter_api_key": "sk-x"})
    await entitlements.require_premium("u3")  # own key → allowed
    await entitlements.apply_event(db, ev("INITIAL_PURCHASE", user="u4")["event"])
    await entitlements.require_premium("u4")  # premium → allowed
    print("entitlements OK")


asyncio.run(main())
