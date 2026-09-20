"""Self-check for weekly insights (LLM stubbed). MONGODB_URI=mongodb://127.0.0.1:27099 python -m scripts.check_weekly_insights"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("JWT_SECRET", "test")
os.environ["OPENROUTER_API_KEY"] = "sk-server"

from fastapi import HTTPException  # noqa: E402

from database import get_db  # noqa: E402
from routers import insights  # noqa: E402
from services import gemini, weekly_insights as wi  # noqa: E402

WEEK = "2026-W38"  # Mon 2026-09-14 .. Sun 2026-09-20


def log(day, slot, kcal, protein, fat, name="dal", cooking="curry", src="home"):
    return {"user_id": "u1", "date": f"2026-09-{day:02d}", "meal_slot": slot, "source_type": src,
            "totals": {"calories_kcal": kcal, "protein_g": protein, "carbs_g": 50, "fat_g": fat},
            "items": [{"name": name, "calories_kcal": kcal, "protein_g": protein, "fat_g": fat, "cooking_method": cooking}]}


def test_stats():
    logs = [
        log(14, "lunch", 800, 20, 30), log(14, "dinner", 900, 25, 40, cooking="boiled"),   # Mon 1700
        log(15, "lunch", 500, 40, 10, name="egg", cooking="boiled"),                        # Tue 500
        log(19, "dinner", 2400, 30, 90, src="restaurant"),                                  # Sat 2400
        log(19, "supplement", 50, 10, 0, name="whey", cooking=None),                        # excluded
    ]
    s = wi.compute_stats(logs, {"kcal": 1800, "protein_g": 100})
    assert s["days_logged"] == 3 and s["entries"] == 4, s
    assert s["avg_kcal"] == round((1700 + 500 + 2400) / 3, 1), s["avg_kcal"]
    assert s["days_over_kcal_goal"] == 1 and s["days_within_10pct_of_kcal_goal"] == 1, s  # Mon 1700 is within 10% of 1800
    assert s["days_protein_below_80pct"] == 3, s          # 45, 40, 30 vs goal 100
    assert s["highest_kcal_day"]["kcal"] == 2400 and s["lowest_kcal_day"]["kcal"] == 500
    assert s["weekday_avg_kcal"] == 1100 and s["weekend_avg_kcal"] == 2400, s
    assert s["restaurant_entries_pct"] == 25
    assert s["fat_from_fried_or_curry_pct"] == round(100 * (30 + 90) / (30 + 40 + 10 + 90)), s
    assert "supplement" not in s["kcal_share_by_meal_pct"]
    assert wi.compute_stats([], {}) == {"days_logged": 0, "entries": 0}
    # goals absent → no goal keys, no crash
    assert "goal_kcal" not in wi.compute_stats(logs, {"kcal": None, "protein_g": None})


GOOD = {"headline": "Protein ran low.", "wins": ["Logged 3 days."], "fixes": [{"title": "Add protein", "detail": "Add an egg."}],
        "next_week_goal": "Hit 100g protein on 4 days."}


async def main():
    test_stats()
    db = get_db()
    for c in ("food_logs", "weekly_insights", "user_profile"):
        await db[c].delete_many({})
    await db.user_profile.insert_one({"user_id": "u1", "goal_kcal": 1800, "protein_g": 100, "user_timezone": "Asia/Kolkata"})

    calls = []
    reply = {"v": GOOD}

    async def fake(stats, api_key):
        calls.append(stats)
        if isinstance(reply["v"], Exception):
            raise reply["v"]
        return reply["v"]
    gemini.weekly_insights = fake

    async def get(week=WEEK):
        return await insights.weekly_insights(week=week, user_id="u1", _premium=None)

    # < 3 days: no LLM call
    await db.food_logs.insert_many([log(14, "lunch", 800, 20, 30), log(15, "lunch", 500, 40, 10)])
    r = await get()
    assert r["status"] == "not_enough_data" and r["days_logged"] == 2 and not calls, r

    # 3 days: generates once, then serves from cache
    await db.food_logs.insert_one(log(16, "dinner", 900, 25, 40))
    r = await get()
    assert r["status"] == "ok" and r["insights"]["headline"] == "Protein ran low." and len(calls) == 1, r
    assert calls[0]["goal_kcal"] == 1800
    await get()
    assert len(calls) == 1, "same logs → cached"

    # new log but cache is fresh (<6h) → still cached
    await db.food_logs.insert_one(log(17, "lunch", 700, 30, 20))
    await get()
    assert len(calls) == 1, "fresh cache must hold even with new logs"

    # cache older than 6h + new logs → regenerates
    await db.weekly_insights.update_one({"user_id": "u1"}, {"$set": {"generated_at": datetime.now(timezone.utc) - timedelta(hours=7)}})
    await get()
    assert len(calls) == 2, "stale cache + new logs → regenerate"

    # malformed model output: stale cache is served instead of an error
    await db.food_logs.insert_one(log(18, "lunch", 600, 30, 20))
    await db.weekly_insights.update_one({"user_id": "u1"}, {"$set": {"generated_at": datetime.now(timezone.utc) - timedelta(hours=7)}})
    reply["v"] = {"headline": "", "fixes": []}
    r = await get()
    assert r["status"] == "ok" and r["insights"]["headline"] == "Protein ran low.", r

    # malformed output with no cache → 502; bad week → 400
    await db.weekly_insights.delete_many({})
    try:
        await get()
        raise AssertionError("expected 502")
    except HTTPException as e:
        assert e.status_code == 502
    try:
        await get("garbage")
        raise AssertionError("expected 400")
    except HTTPException as e:
        assert e.status_code == 400

    # _clean trims and caps
    c = insights._clean({"headline": "h", "wins": ["a", "b", "c"], "next_week_goal": 5,
                         "fixes": [{"title": "t", "detail": "d"}] * 5 + ["junk", {"title": "", "detail": "x"}]})
    assert len(c["wins"]) == 2 and len(c["fixes"]) == 3 and c["next_week_goal"] == "", c
    print("weekly_insights OK")


asyncio.run(main())
