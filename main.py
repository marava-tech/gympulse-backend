"""Fitness OS — FastAPI backend (port 8850)."""
import asyncio
import os
import logging
import logging.config
from contextlib import asynccontextmanager
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import get_db, ensure_indexes
from services import fcm as fcm_svc

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("gympulse")

# ─── Required environment variable check ─────────────────────────────────────
_REQUIRED_ENV = ["MONGODB_URI", "JWT_SECRET", "FIREBASE_PROJECT_ID"]

def _check_env():
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

from routers import (
    auth_router,
    profile,
    food,
    saved_meals,
    saved_foods,
    supplements,
    gym,
    body_analysis,
    body_photos,
    sleep,
    weight_photos,
    streaks,
    summary,
    notifications,
    daily_checkin,
    settings as settings_router,
    tdee as tdee_router,
    health_sync,
)

scheduler = AsyncIOScheduler()


async def _send_weekly_summary():
    """Send FCM notification for weekly summary every Sunday 8pm."""
    db = get_db()
    iso = datetime.now(timezone.utc).date().isocalendar()
    week = f"{iso.year}-W{iso.week:02d}"
    profiles = await db.user_profile.find({"fcm_token": {"$ne": None}}).to_list(None)

    async def _send(profile_doc):
        if not profile_doc.get("notification_prefs", {}).get("weekly_summary", True):
            return
        try:
            await fcm_svc.send_notification(
                profile_doc["fcm_token"],
                "Weekly Summary Ready",
                "Your fitness week is complete — tap to see your summary.",
                {"week": week},
            )
        except Exception as e:
            logger.error("Failed to send weekly summary FCM for user %s: %s", profile_doc.get("user_id"), e)

    await asyncio.gather(*[_send(p) for p in profiles])





async def _send_checkin_reminder(second: bool = False):
    """Remind at 10pm / 11:30pm IST to complete today's check-in — only if not done.

    Runs at 16:30 UTC (22:00 IST) and 18:00 UTC (23:30 IST).
    """
    db = get_db()
    profiles = await db.user_profile.find({"fcm_token": {"$ne": None}}).to_list(None)

    async def _send(profile_doc):
        if not profile_doc.get("notification_prefs", {}).get("daily_checkin_reminder", True):
            return
        user_id = profile_doc.get("user_id")
        tz_name = profile_doc.get("user_timezone", "UTC")
        try:
            user_tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            user_tz = ZoneInfo("UTC")

        today = datetime.now(user_tz).date().isoformat()
        if await db.daily_checkins.find_one({"user_id": user_id, "date": today}):
            return  # already checked in — no reminder

        notif_body = (
            "Last call — you still haven't logged today's check-in. Takes 2 mins."
            if second else
            "Time for your evening check-in — takes 2 mins."
        )

        try:
            await fcm_svc.send_notification(
                profile_doc["fcm_token"],
                "📋 Daily Check-in",
                notif_body,
                {"type": "daily_quiz", "date": today},
            )
        except Exception as e:
            logger.error("Failed to send check-in reminder FCM for user %s: %s", user_id, e)

    await asyncio.gather(*[_send(p) for p in profiles])


async def _end_of_day_reconcile():
    """Send FCM nudge at ~8:30 PM local time if the user logged fewer than 2 meals today.

    Runs at 15:00 UTC (covers IST 20:30). Uses date in user's local timezone.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    db = get_db()
    profiles = await db.user_profile.find({"fcm_token": {"$ne": None}}).to_list(None)

    async def _send(profile_doc):
        if not profile_doc.get("notification_prefs", {}).get("end_of_day_reconcile", True):
            return
        user_id = profile_doc.get("user_id")
        tz_name = profile_doc.get("user_timezone", "UTC")
        try:
            user_tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            user_tz = ZoneInfo("UTC")

        today = datetime.now(user_tz).date().isoformat()
        count = await db.food_logs.count_documents({"user_id": user_id, "date": today})
        if count >= 2:
            return

        try:
            await fcm_svc.send_notification(
                profile_doc["fcm_token"],
                "Don't forget to log your meals!",
                f"Only {count} meal{'s' if count != 1 else ''} logged today — close the gap before bed.",
                {"type": "eod_reconcile"},
            )
        except Exception as e:
            logger.error("Failed to send EOD reconcile FCM for user %s: %s", user_id, e)

    await asyncio.gather(*[_send(p) for p in profiles])


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_env()
    await ensure_indexes()
    # Sunday at 20:00 UTC — weekly summary FCM
    scheduler.add_job(_send_weekly_summary, "cron", day_of_week="sun", hour=20, minute=0)

    # Daily at 16:30 UTC (22:00 IST) — check-in reminder if not done
    scheduler.add_job(_send_checkin_reminder, "cron", hour=16, minute=30)
    # Daily at 18:00 UTC (23:30 IST) — second check-in reminder if still not done
    scheduler.add_job(_send_checkin_reminder, "cron", hour=18, minute=0, kwargs={"second": True})
    # Daily at 15:00 UTC (20:30 IST) — EOD reconcile nudge if < 2 meals logged
    scheduler.add_job(_end_of_day_reconcile, "cron", hour=15, minute=0)
    scheduler.start()
    logger.info("Fitness OS backend started")
    yield
    scheduler.shutdown()
    logger.info("Fitness OS backend stopped")


app = FastAPI(
    title="Fitness OS API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.include_router(auth_router.router)
app.include_router(profile.router)
app.include_router(food.router)
app.include_router(saved_meals.router)
app.include_router(saved_foods.router)
app.include_router(supplements.router)
app.include_router(gym.router)
app.include_router(body_analysis.router)
app.include_router(body_photos.router)
app.include_router(sleep.router)
app.include_router(weight_photos.router)
app.include_router(streaks.router)
app.include_router(summary.router)
app.include_router(notifications.router)
app.include_router(daily_checkin.router)
app.include_router(settings_router.router)
app.include_router(tdee_router.router)
app.include_router(health_sync.router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "fitness-os",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
