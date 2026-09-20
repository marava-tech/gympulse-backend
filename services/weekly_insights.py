"""Weekly nutrition insights: deterministic stats from food logs (this module) → LLM advice
(services/gemini.weekly_insights). Stats are computed here so the model only phrases what the
numbers already say, and so the logic is testable without a database or an LLM."""
from collections import Counter, defaultdict
from datetime import date

MIN_DAYS = 3  # below this the week isn't representative — no insights, no LLM call

_COOKING_FAT = {"fried", "deep_fried", "curry"}
_NOT_FOOD_SLOTS = {"supplement"}


def _pct(part: float, whole: float) -> float | None:
    return round(100 * part / whole) if whole else None


def compute_stats(food_logs: list[dict], goals: dict) -> dict:
    """food_logs: docs from db.food_logs for one week. goals: {"kcal": n|None, "protein_g": n|None}."""
    logs = [l for l in food_logs if l.get("meal_slot") not in _NOT_FOOD_SLOTS]
    days: dict[str, dict] = defaultdict(lambda: {"kcal": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0})
    slot_kcal: Counter = Counter()
    foods: Counter = Counter()
    fat_total = fat_cooked = 0.0
    restaurant = 0

    for log in logs:
        t = log.get("totals") or {}
        d = days[log["date"]]
        d["kcal"] += t.get("calories_kcal", 0)
        d["protein"] += t.get("protein_g", 0)
        d["carbs"] += t.get("carbs_g", 0)
        d["fat"] += t.get("fat_g", 0)
        slot_kcal[str(log.get("meal_slot"))] += t.get("calories_kcal", 0)
        restaurant += log.get("source_type") == "restaurant"
        for item in log.get("items") or []:
            foods[(item.get("name") or "").strip().lower()] += 1
            f = item.get("fat_g") or 0
            fat_total += f
            if item.get("cooking_method") in _COOKING_FAT:
                fat_cooked += f

    n = len(days)
    stats: dict = {"days_logged": n, "entries": len(logs)}
    if n == 0:
        return stats

    def avg(key: str) -> float:
        return round(sum(v[key] for v in days.values()) / n, 1)

    stats.update(avg_kcal=avg("kcal"), avg_protein_g=avg("protein"), avg_carbs_g=avg("carbs"), avg_fat_g=avg("fat"))

    goal_kcal, goal_protein = goals.get("kcal"), goals.get("protein_g")
    if goal_kcal:
        stats["goal_kcal"] = goal_kcal
        stats["days_over_kcal_goal"] = sum(v["kcal"] > goal_kcal * 1.1 for v in days.values())
        stats["days_within_10pct_of_kcal_goal"] = sum(abs(v["kcal"] - goal_kcal) <= goal_kcal * 0.1 for v in days.values())
    if goal_protein:
        stats["goal_protein_g"] = goal_protein
        stats["days_protein_below_80pct"] = sum(v["protein"] < goal_protein * 0.8 for v in days.values())

    hi = max(days.items(), key=lambda kv: kv[1]["kcal"])
    lo = min(days.items(), key=lambda kv: kv[1]["kcal"])
    stats["highest_kcal_day"] = {"date": hi[0], "kcal": round(hi[1]["kcal"])}
    stats["lowest_kcal_day"] = {"date": lo[0], "kcal": round(lo[1]["kcal"])}

    wk = [v["kcal"] for d_, v in days.items() if date.fromisoformat(d_).weekday() < 5]
    we = [v["kcal"] for d_, v in days.items() if date.fromisoformat(d_).weekday() >= 5]
    if wk and we:
        stats["weekday_avg_kcal"] = round(sum(wk) / len(wk))
        stats["weekend_avg_kcal"] = round(sum(we) / len(we))

    total_kcal = sum(slot_kcal.values())
    stats["kcal_share_by_meal_pct"] = {s: _pct(k, total_kcal) for s, k in slot_kcal.most_common()}
    stats["fat_from_fried_or_curry_pct"] = _pct(fat_cooked, fat_total)
    stats["restaurant_entries_pct"] = _pct(restaurant, len(logs))
    stats["most_logged_foods"] = [name for name, _ in foods.most_common(5) if name]
    return stats
