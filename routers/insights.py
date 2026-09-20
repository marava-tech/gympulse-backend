"""Weekly nutrition insights (Premium)."""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from routers.summary import _week_dates
from services import ai_quota, entitlements, gemini as gemini_svc
from services import weekly_insights as wi
from services.goal_history import today_for_user
from utils import get_openrouter_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/insights", tags=["insights"])

# Re-generate only if new logs arrived AND the cached copy is at least this old (bounds AI cost).
_REFRESH_AFTER = timedelta(hours=6)


def _current_week(profile: dict | None) -> str:
    iso = datetime.fromisoformat(today_for_user(profile or {})).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _clean(raw: dict) -> dict:
    """Validate/trim the model output so the app never gets a malformed shape."""
    def text(v, n):
        return v.strip()[:n] if isinstance(v, str) else ""

    fixes = [
        {"title": text(f.get("title"), 60), "detail": text(f.get("detail"), 300)}
        for f in (raw.get("fixes") or []) if isinstance(f, dict)
    ]
    out = {
        "headline": text(raw.get("headline"), 200),
        "wins": [w for w in (text(w, 200) for w in (raw.get("wins") or [])[:2]) if w],
        "fixes": [f for f in fixes if f["title"] and f["detail"]][:3],
        "next_week_goal": text(raw.get("next_week_goal"), 200),
    }
    if not out["headline"] or not out["fixes"]:
        raise ValueError("insights missing headline/fixes")
    return out


@router.get("/weekly")
async def weekly_insights(
    week: str | None = None,
    user_id: str = Depends(get_current_user),
    _premium: None = Depends(entitlements.require_premium),
):
    """week format YYYY-Www (e.g. 2026-W38); defaults to the current week in the user's timezone."""
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    week = week or _current_week(profile)
    try:
        start, end = _week_dates(week)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid week format. Use YYYY-Www (e.g. 2026-W38)")

    logs = await db.food_logs.find({"user_id": user_id, "date": {"$gte": start, "$lte": end}}).to_list(None)
    p = profile or {}
    goals = {
        "kcal": p.get("goal_kcal") or p.get("gym_goal_kcal") or p.get("rest_goal_kcal"),
        "protein_g": p.get("protein_g") or p.get("gym_protein_g") or p.get("rest_protein_g"),
    }
    stats = wi.compute_stats(logs, goals)
    base = {"week": week, "start_date": start, "end_date": end}

    if stats["days_logged"] < wi.MIN_DAYS:
        return {**base, "status": "not_enough_data", "days_logged": stats["days_logged"], "needed": wi.MIN_DAYS}

    cached = await db.weekly_insights.find_one({"user_id": user_id, "week": week})
    if cached:
        generated = cached["generated_at"]
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        fresh = datetime.now(timezone.utc) - generated < _REFRESH_AFTER
        if cached["log_count"] == len(logs) or fresh:
            return {**base, "status": "ok", "stats": cached["stats"], "insights": cached["insights"],
                    "generated_at": generated.isoformat()}

    api_key = get_openrouter_key(profile)
    if not api_key:
        raise HTTPException(402, "AI is not configured")
    try:
        insights = _clean(await gemini_svc.weekly_insights(stats, api_key=api_key))
    except (ValueError, KeyError) as e:
        logger.error("Weekly insights parse failure for %s: %s", user_id, e)
        if cached:  # stale insights beat an error
            return {**base, "status": "ok", "stats": cached["stats"], "insights": cached["insights"],
                    "generated_at": cached["generated_at"].isoformat()}
        raise HTTPException(502, "Couldn't generate insights, please try again")

    now = datetime.now(timezone.utc)
    await db.weekly_insights.update_one(
        {"user_id": user_id, "week": week},
        {"$set": {"stats": stats, "insights": insights, "log_count": len(logs),
                  "generated_at": now, "expires_at": now + timedelta(days=60)}},
        upsert=True,
    )
    return {**base, "status": "ok", "stats": stats, "insights": insights, "generated_at": now.isoformat()}
