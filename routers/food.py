"""Food logging — AI analysis pipeline + CRUD for logs."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form

from bson import ObjectId
from typing import Optional

from auth import get_current_user
from database import get_db
from models.food_log import FoodLogCreate, FoodItem, ItemEstimateRequest, MacroSource, MealSlot
from services import gemini as gemini_svc
from services import minio_client
from services.openfoodfacts import lookup_macros
from utils import validate_image_upload, get_openrouter_key

# Correction-blend config
_CORRECTION_EPSILON = 0.05   # ignore edits < 5% relative change (noise threshold)
_CORRECTION_MAX_BLEND = 0.40  # cap blend at ±40% to prevent runaway corrections

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/food", tags=["food"])


async def _resolve_macros(
    name: str,
    weight_g: float,
    api_key: str | None,
    cooking_method: str | None = None,
    source_type: str | None = None,
    user_id: str | None = None,
) -> dict:
    result = await lookup_macros(name, weight_g)
    if result:
        return result
    if not api_key:
        raise HTTPException(402, "Set your OpenRouter API key in Settings to use AI features")
    ai_result = await gemini_svc.estimate_macros(
        name, weight_g, api_key=api_key,
        cooking_method=cooking_method, source_type=source_type,
    )
    ai_result["source"] = "ai_estimated"

    # Blend in user's personal correction if one exists for this food name
    if user_id:
        name_norm = name.strip().lower()
        db = get_db()
        correction = await db.food_corrections.find_one({"user_id": user_id, "name_norm": name_norm})
        if correction:
            count = correction.get("count", 1)
            # Weight blend toward correction proportional to confidence (capped at 0.6 after 5+ edits)
            blend_weight = min(0.12 * count, 0.60)
            corr = correction["corrected_per_g"]

            def _apply(ai_val: float, corr_per_g: float, w: float) -> float:
                corrected = corr_per_g * weight_g
                delta_ratio = (corrected - ai_val) / ai_val if ai_val else 0.0
                # Cap blend delta at ±_CORRECTION_MAX_BLEND to prevent runaway
                clamped_delta = max(-_CORRECTION_MAX_BLEND, min(_CORRECTION_MAX_BLEND, delta_ratio))
                return round(ai_val * (1 + clamped_delta * w), 1)

            if ai_result.get("calories_kcal", 0) > 0:
                ai_result["calories_kcal"] = _apply(ai_result["calories_kcal"], corr["cal_per_g"], blend_weight)
                ai_result["protein_g"] = _apply(ai_result["protein_g"], corr["protein_per_g"], blend_weight)
                ai_result["carbs_g"] = _apply(ai_result["carbs_g"], corr["carbs_per_g"], blend_weight)
                ai_result["fat_g"] = _apply(ai_result["fat_g"], corr["fat_per_g"], blend_weight)
                ai_result["personalized"] = True

    return ai_result


async def _capture_corrections(items: list, user_id: str, db) -> None:
    """For each item that has an ai_original snapshot, compare per-gram macros with what
    the user actually saved.  If the relative change exceeds the noise threshold, upsert a
    running-average correction record in food_corrections so future estimates can be biased
    toward the user's preferred values.
    """
    for item in items:
        orig = item.get("ai_original") if isinstance(item, dict) else None
        if orig is None:
            continue
        weight = float(item.get("estimated_weight_g") or 0)
        if weight <= 0:
            continue

        # Current (user-accepted) per-gram values
        cur_cal = float(item.get("calories_kcal") or 0) / weight
        cur_prot = float(item.get("protein_g") or 0) / weight
        cur_carbs = float(item.get("carbs_g") or 0) / weight
        cur_fat = float(item.get("fat_g") or 0) / weight

        # Original AI per-gram values
        if isinstance(orig, dict):
            ai_cal = float(orig.get("calories_kcal") or 0) / weight
            ai_prot = float(orig.get("protein_g") or 0) / weight
            ai_carbs = float(orig.get("carbs_g") or 0) / weight
            ai_fat = float(orig.get("fat_g") or 0) / weight
        else:
            ai_cal = float(orig.calories_kcal) / weight
            ai_prot = float(orig.protein_g) / weight
            ai_carbs = float(orig.carbs_g) / weight
            ai_fat = float(orig.fat_g) / weight

        # Only record if at least one macro changed meaningfully
        if ai_cal <= 0:
            continue
        rel_change = abs(cur_cal - ai_cal) / ai_cal
        if rel_change < _CORRECTION_EPSILON:
            continue

        name_norm = item.get("name", "").strip().lower()
        if not name_norm:
            continue

        # Upsert a running average (weighted by count) of the corrected per-gram values
        existing = await db.food_corrections.find_one({"user_id": user_id, "name_norm": name_norm})
        if existing:
            n = existing.get("count", 1)
            def _blend(old: float, new: float, n: int) -> float:
                return (old * n + new) / (n + 1)

            await db.food_corrections.update_one(
                {"user_id": user_id, "name_norm": name_norm},
                {"$set": {
                    "corrected_per_g.cal_per_g": _blend(existing["corrected_per_g"]["cal_per_g"], cur_cal, n),
                    "corrected_per_g.protein_per_g": _blend(existing["corrected_per_g"]["protein_per_g"], cur_prot, n),
                    "corrected_per_g.carbs_per_g": _blend(existing["corrected_per_g"]["carbs_per_g"], cur_carbs, n),
                    "corrected_per_g.fat_per_g": _blend(existing["corrected_per_g"]["fat_per_g"], cur_fat, n),
                    "count": n + 1,
                    "updated_at": datetime.now(timezone.utc),
                }},
            )
        else:
            await db.food_corrections.insert_one({
                "user_id": user_id,
                "name_norm": name_norm,
                "original_per_g": {
                    "cal_per_g": ai_cal,
                    "protein_per_g": ai_prot,
                    "carbs_per_g": ai_carbs,
                    "fat_per_g": ai_fat,
                },
                "corrected_per_g": {
                    "cal_per_g": cur_cal,
                    "protein_per_g": cur_prot,
                    "carbs_per_g": cur_carbs,
                    "fat_per_g": cur_fat,
                },
                "count": 1,
                "updated_at": datetime.now(timezone.utc),
            })


async def _update_if_log(food_date: str, timestamp: datetime, user_id: str, db):
    """Derive IF adherence from the eating window in user_profile."""
    profile = await db.user_profile.find_one({"user_id": user_id})
    if not profile:
        return

    window_start = profile.get("eating_window_start", "13:00")
    window_end = profile.get("eating_window_end", "21:00")
    tz_name = profile.get("user_timezone", "UTC")

    start_h, start_m = map(int, window_start.split(":"))
    end_h, end_m = map(int, window_end.split(":"))

    # Convert UTC timestamp to the user's local timezone before comparison
    try:
        user_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown user_timezone '%s', falling back to UTC", tz_name)
        user_tz = ZoneInfo("UTC")

    local_time = timestamp.astimezone(user_tz)
    food_time = local_time.hour * 60 + local_time.minute
    window_start_min = start_h * 60 + start_m
    window_end_min = end_h * 60 + end_m

    if window_start_min <= window_end_min:
        in_window = window_start_min <= food_time <= window_end_min
    else:
        # Cross-midnight window (e.g. 22:00–06:00)
        in_window = food_time >= window_start_min or food_time <= window_end_min

    # Atomic upsert: adherence stays True only if every entry so far has been
    # in-window (defaults to True on insert), avoiding a read-then-write race
    # that could otherwise create duplicate if_logs docs for the same day.
    await db.if_logs.update_one(
        {"date": food_date, "user_id": user_id},
        [{"$set": {"adhered": {"$and": [{"$ifNull": ["$adhered", True]}, in_window]}}}],
        upsert=True,
    )


@router.post("/analyze")
async def analyze_food(
    photo: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    db = get_db()
    image_bytes = await photo.read()
    validate_image_upload(image_bytes, photo.filename or "", photo.content_type)

    # Fetch user's API key (falls back to server-wide key for internal testing)
    profile = await db.user_profile.find_one({"user_id": user_id})
    api_key = get_openrouter_key(profile)
    if not api_key:
        raise HTTPException(402, "Set your OpenRouter API key in Settings to use AI features")

    # Upload to MinIO for storage
    filename = f"{uuid.uuid4()}.jpg"
    image_url = await minio_client.upload_image(image_bytes, minio_client.BUCKET_FOOD, filename)

    # AI food analysis
    analysis = await gemini_svc.analyze_food(image_bytes, api_key=api_key)
    items = analysis.get("items", [])
    scale_weight_g = analysis.get("scale_weight_g")

    # Saved meal similarity check — only fetch name + item names (scoped to user)
    saved_meals = await db.saved_meals.find(
        {"user_id": user_id}, {"_id": 1, "name": 1, "items.name": 1}
    ).to_list(None)
    meal_suggestion = None
    if saved_meals and items:
        detected_names = {item["name"].lower() for item in items}
        best_overlap = 0.0
        best_meal = None
        for meal in saved_meals:
            meal_names = {i["name"].lower() for i in meal.get("items", [])}
            if not meal_names:
                continue
            overlap = len(detected_names & meal_names) / max(len(meal_names), 1)
            if overlap > best_overlap:
                best_overlap = overlap
                best_meal = meal
        if best_overlap >= 0.6 and best_meal:
            meal_suggestion = {
                "meal_id": str(best_meal["_id"]),
                "meal_name": best_meal["name"],
                "overlap": round(best_overlap, 2),
            }

    # Resolve macros for all detected items in parallel (with personalized correction blend)
    macro_list = await asyncio.gather(*[
        _resolve_macros(
            item["name"],
            item["estimated_weight_g"],
            api_key,
            cooking_method=item.get("cooking_method"),
            user_id=user_id,
        ) for item in items
    ])
    enriched_items = [{**item, **macros} for item, macros in zip(items, macro_list)]

    return {
        "items": enriched_items,
        "scale_weight_g": scale_weight_g,
        "image_url": image_url,
        "meal_suggestion": meal_suggestion,
    }


@router.post("/estimate-item")
async def estimate_item(body: ItemEstimateRequest, user_id: str = Depends(get_current_user)):
    """Re-estimate one item's macros once cooking method / home-vs-restaurant is known.

    The initial /analyze pass runs on the photo alone, before the user has set that
    context on the review screen, so this lets the client refresh the numbers in place.
    """
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    api_key = get_openrouter_key(profile)
    if not api_key:
        raise HTTPException(402, "Set your OpenRouter API key in Settings to use AI features")

    macros = await _resolve_macros(
        body.name, body.estimated_weight_g, api_key,
        cooking_method=body.cooking_method.value if body.cooking_method else None,
        source_type=body.source_type.value if body.source_type else None,
        user_id=user_id,
    )
    return macros


@router.post("/logs", status_code=201)
async def create_food_log(body: FoodLogCreate, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user)):
    db = get_db()
    now = datetime.now(timezone.utc)

    # Derive the log date in the user's local timezone so midnight-crossover entries
    # land on the correct day (e.g. 1 AM IST is still the same calendar day for the user).
    profile = await db.user_profile.find_one({"user_id": user_id})
    tz_name = (profile or {}).get("user_timezone", "UTC")
    try:
        user_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        user_tz = ZoneInfo("UTC")
    food_date = now.astimezone(user_tz).date().isoformat()

    # Fetch user's API key — only needed if OpenFoodFacts misses an item
    api_key = get_openrouter_key(profile)

    # Resolve macros — skip lookup for items that already carry all four macro values
    # (e.g. supplements logged from the quiz with macro_source='database').
    resolved_items = []
    total_cal = total_protein = total_carbs = total_fat = 0.0

    items_needing_lookup = [
        item for item in body.items
        if not (item.calories_kcal is not None and item.protein_g is not None
                and item.carbs_g is not None and item.fat_g is not None)
    ]
    macro_lookup_results = await asyncio.gather(*[
        _resolve_macros(
            item.name, item.estimated_weight_g, api_key,
            cooking_method=item.cooking_method.value if item.cooking_method else None,
            source_type=body.source_type.value if body.source_type else None,
            user_id=user_id,
        )
        for item in items_needing_lookup
    ])
    lookup_iter = iter(macro_lookup_results)

    for item in body.items:
        resolved = item.model_dump()
        if (item.calories_kcal is not None and item.protein_g is not None
                and item.carbs_g is not None and item.fat_g is not None):
            macros = {
                "calories_kcal": item.calories_kcal,
                "protein_g": item.protein_g,
                "carbs_g": item.carbs_g,
                "fat_g": item.fat_g,
            }
        else:
            macros = next(lookup_iter)
            resolved.update(macros)
        # Strip client-only correction snapshot before DB write
        resolved.pop("ai_original", None)
        resolved_items.append(resolved)
        total_cal += macros.get("calories_kcal", 0)
        total_protein += macros.get("protein_g", 0)
        total_carbs += macros.get("carbs_g", 0)
        total_fat += macros.get("fat_g", 0)

    doc = {
        "user_id": user_id,
        "date": food_date,
        "meal_slot": body.meal_slot,
        "source_type": body.source_type.value if body.source_type else None,
        "items": resolved_items,
        "image_url": body.image_url,
        "note": body.note,
        "totals": {
            "calories_kcal": round(total_cal, 1),
            "protein_g": round(total_protein, 1),
            "carbs_g": round(total_carbs, 1),
            "fat_g": round(total_fat, 1),
        },
        "created_at": now,
    }

    # Supplements are submitted once per day from the daily quiz.
    # Always clear previous supplement food logs on re-submit, then only insert if
    # there are actual items (handles the case where no supplements were taken).
    if body.meal_slot == MealSlot.supplement:
        await db.food_logs.delete_many({"date": food_date, "meal_slot": MealSlot.supplement.value, "user_id": user_id})
        if not body.items:
            return {"date": food_date, "meal_slot": "supplement", "items": [], "totals": {"calories_kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}}

    result = await db.food_logs.insert_one(doc)

    background_tasks.add_task(_update_if_log, food_date, now, user_id, db)
    # Capture any user corrections vs AI estimate for the learning loop
    items_with_originals = [item.model_dump() for item in body.items]
    background_tasks.add_task(_capture_corrections, items_with_originals, user_id, db)

    doc["_id"] = str(result.inserted_id)
    return doc


@router.delete("/logs/{log_id}", status_code=204)
async def delete_food_log(log_id: str, user_id: str = Depends(get_current_user)):
    db = get_db()
    try:
        oid = ObjectId(log_id)
    except Exception:
        raise HTTPException(400, "Invalid log ID")
    result = await db.food_logs.delete_one({"_id": oid, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(404, "Food log not found")


@router.put("/logs/{log_id}")
async def update_food_log(
    log_id: str,
    body: FoodLogCreate,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user),
):
    db = get_db()
    try:
        oid = ObjectId(log_id)
    except Exception:
        raise HTTPException(400, "Invalid log ID")

    # Check if the log exists and belongs to the user
    existing_log = await db.food_logs.find_one({"_id": oid, "user_id": user_id})
    if not existing_log:
        raise HTTPException(404, "Food log not found")

    profile = await db.user_profile.find_one({"user_id": user_id})
    api_key = get_openrouter_key(profile)

    resolved_items = []
    total_cal = total_protein = total_carbs = total_fat = 0.0

    items_needing_lookup = [
        item for item in body.items
        if not (item.calories_kcal is not None and item.protein_g is not None
                and item.carbs_g is not None and item.fat_g is not None)
    ]
    macro_lookup_results = await asyncio.gather(*[
        _resolve_macros(
            item.name, item.estimated_weight_g, api_key,
            cooking_method=item.cooking_method.value if item.cooking_method else None,
            source_type=body.source_type.value if body.source_type else None,
            user_id=user_id,
        )
        for item in items_needing_lookup
    ])
    lookup_iter = iter(macro_lookup_results)

    for item in body.items:
        resolved = item.model_dump()
        if (item.calories_kcal is not None and item.protein_g is not None
                and item.carbs_g is not None and item.fat_g is not None):
            macros = {
                "calories_kcal": item.calories_kcal,
                "protein_g": item.protein_g,
                "carbs_g": item.carbs_g,
                "fat_g": item.fat_g,
            }
        else:
            macros = next(lookup_iter)
            resolved.update(macros)
        # Strip client-only correction snapshot before DB write
        resolved.pop("ai_original", None)
        resolved_items.append(resolved)
        total_cal += macros.get("calories_kcal", 0)
        total_protein += macros.get("protein_g", 0)
        total_carbs += macros.get("carbs_g", 0)
        total_fat += macros.get("fat_g", 0)

    update_doc = {
        "meal_slot": body.meal_slot,
        "items": resolved_items,
        "note": body.note,
        "totals": {
            "calories_kcal": round(total_cal, 1),
            "protein_g": round(total_protein, 1),
            "carbs_g": round(total_carbs, 1),
            "fat_g": round(total_fat, 1),
        },
        "updated_at": datetime.now(timezone.utc),
    }
    if body.image_url is not None:
        update_doc["image_url"] = body.image_url

    await db.food_logs.update_one({"_id": oid, "user_id": user_id}, {"$set": update_doc})

    log_date = existing_log.get("date")
    if log_date:
        background_tasks.add_task(
            _update_if_log,
            log_date,
            existing_log.get("created_at") or datetime.now(timezone.utc),
            user_id,
            db,
        )
    # Capture any user corrections vs AI estimate for the learning loop
    items_with_originals = [item.model_dump() for item in body.items]
    background_tasks.add_task(_capture_corrections, items_with_originals, user_id, db)

    updated = await db.food_logs.find_one({"_id": oid})
    if updated:
        updated["_id"] = str(updated["_id"])
        if "created_at" in updated and isinstance(updated["created_at"], datetime):
            updated["created_at"] = updated["created_at"].isoformat()
        if "updated_at" in updated and isinstance(updated["updated_at"], datetime):
            updated["updated_at"] = updated["updated_at"].isoformat()
    return updated


@router.get("/logs")
async def get_food_logs(date: str, user_id: str = Depends(get_current_user)):
    """date format: YYYY-MM-DD (query param ?date=)"""
    db = get_db()
    docs = await db.food_logs.find({"date": date, "user_id": user_id}).to_list(None)

    grouped: dict[str, list] = {"breakfast": [], "lunch": [], "dinner": [], "extras": [], "supplement": [], "meal1": [], "meal2": [], "snack": []}
    slot_totals: dict[str, dict] = {}

    for doc in docs:
        slot = doc.get("meal_slot", "snack")
        doc["_id"] = str(doc["_id"])
        grouped.setdefault(slot, []).append(doc)

    # Calculate per-slot totals
    day_total = {"calories_kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    for slot, entries in grouped.items():
        t = {"calories_kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
        for entry in entries:
            totals = entry.get("totals", {})
            for k in t:
                t[k] += totals.get(k, 0)
                day_total[k] += totals.get(k, 0)
        slot_totals[slot] = {k: round(v, 1) for k, v in t.items()}

    return {
        "date": date,
        "entries": grouped,
        "slot_totals": slot_totals,
        "day_total": {k: round(v, 1) for k, v in day_total.items()},
    }


@router.get("/daily-totals")
async def get_daily_totals(days: int = 30, user_id: str = Depends(get_current_user)):
    """Returns daily aggregated calories, protein, carbs, and fat for the past N days."""
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    tz_name = (profile or {}).get("user_timezone", "UTC")
    try:
        user_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        user_tz = ZoneInfo("UTC")
    today = datetime.now(user_tz).date()
    match_stage: dict = {"user_id": user_id}
    if days > 0:
        start = (today - timedelta(days=days - 1)).isoformat()
        match_stage["date"] = {"$gte": start, "$lte": today.isoformat()}

    pipeline = [
        {"$match": match_stage},
        {"$group": {
            "_id": "$date",
            "calories_kcal": {"$sum": "$totals.calories_kcal"},
            "protein_g": {"$sum": "$totals.protein_g"},
            "carbs_g": {"$sum": "$totals.carbs_g"},
            "fat_g": {"$sum": "$totals.fat_g"},
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "calories_kcal": {"$round": ["$calories_kcal", 1]},
            "protein_g": {"$round": ["$protein_g", 1]},
            "carbs_g": {"$round": ["$carbs_g", 1]},
            "fat_g": {"$round": ["$fat_g", 1]},
        }},
        {"$sort": {"date": 1}},
    ]

    result = await db.food_logs.aggregate(pipeline).to_list(None)
    return {"totals": result}


@router.get("/logging-score")
async def get_logging_score(days: int = 7, user_id: str = Depends(get_current_user)):
    """Returns a logging consistency score (0–100) for the last N days.

    Score = (logged_days / total_days) * 100, plus a bonus for multi-meal coverage.
    Also includes a 7-day average kcal for the weekly headline.
    """
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    tz_name = (profile or {}).get("user_timezone", "UTC")
    try:
        user_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        user_tz = ZoneInfo("UTC")

    today = datetime.now(user_tz).date()
    start = (today - timedelta(days=days - 1)).isoformat()

    pipeline = [
        {"$match": {"user_id": user_id, "date": {"$gte": start, "$lte": today.isoformat()}}},
        {"$group": {
            "_id": "$date",
            "total_kcal": {"$sum": "$totals.calories_kcal"},
            "meal_slots": {"$addToSet": "$meal_slot"},
        }},
    ]
    day_docs = await db.food_logs.aggregate(pipeline).to_list(None)

    logged_days = len(day_docs)
    total_days = days

    avg_kcal = (
        sum(d.get("total_kcal", 0) for d in day_docs) / logged_days
        if logged_days > 0
        else None
    )

    # Bonus: average meal slot coverage per logged day
    slot_coverage = (
        sum(len(d.get("meal_slots", [])) for d in day_docs) / logged_days
        if logged_days > 0
        else 0
    )

    # Base score: coverage ratio
    base = (logged_days / total_days) * 100
    # Bonus up to 10 pts for logging ≥3 meals
    bonus = min(10, (slot_coverage / 3) * 10)
    score = round(min(100, base + bonus))

    # Trend: compare last 7 days avg vs previous 7 days avg (if enough data)
    trend_delta = None
    if days >= 7:
        prev_start = (today - timedelta(days=days * 2 - 1)).isoformat()
        prev_end = (today - timedelta(days=days)).isoformat()
        prev_pipeline = [
            {"$match": {"user_id": user_id, "date": {"$gte": prev_start, "$lte": prev_end}}},
            {"$group": {"_id": None, "total_kcal": {"$sum": "$totals.calories_kcal"}, "count": {"$sum": 1}}},
        ]
        prev_docs = await db.food_logs.aggregate(prev_pipeline).to_list(None)
        if prev_docs and prev_docs[0].get("count", 0) > 0:
            prev_avg = prev_docs[0]["total_kcal"] / prev_docs[0]["count"]
            if avg_kcal is not None:
                trend_delta = round(avg_kcal - prev_avg)

    return {
        "score": score,
        "logged_days": logged_days,
        "total_days": total_days,
        "avg_kcal_7d": round(avg_kcal) if avg_kcal is not None else None,
        "trend_delta_kcal": trend_delta,
        "slot_coverage_avg": round(slot_coverage, 1),
    }
