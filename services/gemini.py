"""LLM vision calls via OpenRouter — food analysis, body analysis."""
import base64
import json
import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# gemini-2.5-pro for vision (best accuracy); gemini-2.5-flash for text (fast + cheap)
_VISION_MODEL = os.environ.get("OPENROUTER_VISION_MODEL", "google/gemini-2.5-pro")
_TEXT_MODEL = os.environ.get("OPENROUTER_TEXT_MODEL", "google/gemini-2.5-flash")


def _img_url(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    b64 = base64.b64encode(image_bytes).decode()
    return f"data:{mime};base64,{b64}"


def _parse_json(text: str | None) -> dict:
    if not text:
        raise ValueError("LLM returned an empty response")
    text = text.strip()
    text = re.sub(r"^```(?:json)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # LLM sometimes returns truncated output — extract the first valid JSON object
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise


async def _chat(model: str, messages: list[dict], api_key: str, system: str | None = None, max_tokens: int | None = None) -> str:
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://gympulse-backend.marava.tech",
    }
    payload: dict = {"model": model, "messages": messages}
    if system:
        payload["messages"] = [{"role": "system", "content": system}] + messages
    if max_tokens:
        payload["max_tokens"] = max_tokens
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(_OPENROUTER_URL, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


_FOOD_SYSTEM = (
    "You are an expert nutritionist and food analyst trained on professional dietary databases "
    "(USDA FoodData Central, NCCDB, Indian Food Composition Tables — IFCT 2017). "
    "You specialize in visual food identification and accurate portion weight estimation from photos, "
    "with deep expertise in Indian home-cooked and restaurant food. "
    "Indian cooked food almost always contains cooking oil or ghee that is NOT visible in the photo — "
    "you must account for this invisible fat in every curry, sabzi, gravy, and tadka. "
    "Your estimates are used for fat-loss health tracking; precision matters and you must NEVER "
    "under-estimate calories — always err on the side of slight overestimation."
)

_FOOD_ANALYZE_PROMPT = """Analyze this food photo with high precision. Identify every distinct food item visible.

Weight estimation guidelines:
- Reference: standard dinner plate ≈ 26cm diameter, side plate ≈ 20cm, standard bowl ≈ 400–600ml
- Common anchors: cooked rice (1 cup ≈ 200g), chicken breast (medium ≈ 160g), bread slice ≈ 30g, egg ≈ 55g, banana (medium ≈ 120g)
- For Indian food: 1 roti ≈ 40g, 1 cup dal ≈ 250g, 1 cup sabzi ≈ 200g, 1 serving rice ≈ 150–200g
- Factor in cooking method: fried items are denser, boiled/steamed have more water weight

Scale reading rules (CRITICAL):
- If a kitchen scale display is visible, read that number and put it in scale_weight_g
- scale_weight_g is the GROSS weight = food + bowl/plate/container combined
- Do NOT use the scale reading as a food item's estimated_weight_g — that field must always be a visual estimate
- estimated_weight_g per item is your visual portion estimate, independent of any scale reading

Naming rules:
- Be specific: "grilled chicken breast" not "chicken", "steamed basmati rice" not "rice"
- List composite dishes as one item (e.g., "paneer butter masala") — do NOT break into individual ingredients
- Include preparation: "deep-fried samosa", "boiled egg", "raw cucumber slices"

Cooking method classification (REQUIRED per item):
- Look at visual cues — sheen/gloss (oil), browning/crust (frying), char marks (grilling), gravy/sauce (curry) — to classify each item's preparation.
- cooking_method must be exactly one of: "raw", "boiled", "steamed", "grilled", "fried", "curry", "deep_fried"
- "raw" is ONLY for uncooked food (salads, fruit, raw vegetables) — do not default to raw for any cooked dish
- Composite dishes with visible gravy/sauce (dal, sabzi, curry, masala) → "curry"
- Visibly oily/crispy coating or batter → "deep_fried"; light pan-sear/sauté sheen → "fried"
- Char marks or grill lines → "grilled"; no visible fat/oil and matte surface (idli, boiled egg, steamed rice) → "boiled" or "steamed"

Return ONLY valid JSON, no explanation, no markdown:
{"items": [{"name": "string", "estimated_weight_g": number, "cooking_method": "string"}], "scale_weight_g": number or null}"""


async def analyze_food(image_bytes: bytes, api_key: str) -> dict:
    """Return {items: [{name, estimated_weight_g}], scale_weight_g: float|None}"""
    messages = [{"role": "user", "content": [
        {"type": "text", "text": _FOOD_ANALYZE_PROMPT},
        {"type": "image_url", "image_url": {"url": _img_url(image_bytes)}},
    ]}]
    text = await _chat(_VISION_MODEL, messages, api_key=api_key, system=_FOOD_SYSTEM)
    return _parse_json(text)


_BODY_SYSTEM = (
    "You are a certified physique assessment specialist with expertise in visual body composition analysis. "
    "You provide objective, calibrated body fat percentage estimates based on visible muscle definition, "
    "subcutaneous fat distribution, vascularity, and skin fold appearance. Your assessments are used for "
    "fitness progress tracking — be accurate and consistent, not flattering."
)


async def analyze_body_photo(
    current_image_bytes: bytes,
    prev_image_bytes: Optional[bytes],
    angle: str,
    api_key: str,
) -> dict:
    """Returns {caption, bf_low_pct, bf_high_pct}"""
    comparison_note = (
        "The FIRST image is a previous reference photo. The SECOND image is the current photo being assessed. "
        "Note any visible changes in muscle fullness, fat distribution, or definition.\n\n"
        if prev_image_bytes else ""
    )

    prompt = (
        f"{comparison_note}"
        f"Assess the {angle} view for body composition.\n\n"
        "Body fat % visual reference:\n"
        "  Male: 3–5% (competition shredded), 6–9% (visible abs + striations), 10–14% (abs visible), "
        "15–19% (soft abs), 20–25% (no definition), 25%+ (significant fat cover)\n"
        "  Female: 10–13% (athlete), 14–17% (fitness), 18–24% (average fit), 25–31% (average), 32%+ (above average)\n\n"
        "Evaluate: visible muscle separation, abdominal definition, vascularity, subcutaneous fat "
        "at waist/hips/chest/arms, overall body proportions.\n\n"
        "Return ONLY valid JSON:\n"
        '{"bf_low_pct": number, "bf_high_pct": number, "caption": "string"}\n\n'
        "- bf_low_pct / bf_high_pct: realistic ±2–3% range (e.g., 14 and 17)\n"
        "- caption: 2–3 sentences of objective observations (muscle groups visible, fat distribution, "
        "any notable changes if comparison photo present)\n"
        "Output ONLY the JSON."
    )

    content: list = [{"type": "text", "text": prompt}]
    if prev_image_bytes:
        content.append({"type": "image_url", "image_url": {"url": _img_url(prev_image_bytes)}})
    content.append({"type": "image_url", "image_url": {"url": _img_url(current_image_bytes)}})

    messages = [{"role": "user", "content": content}]
    text = await _chat(_VISION_MODEL, messages, api_key=api_key, system=_BODY_SYSTEM)
    return _parse_json(text)


_COMPARE_SYSTEM = (
    "You are an elite physique coach and certified body composition analyst. "
    "You specialize in tracking visual progress over time using photographic evidence. "
    "Your assessments are objective, detailed, and actionable — not motivational fluff."
)


async def compare_body_photos(photos: list[dict], api_key: str) -> dict:
    """
    Compare 2–3 body photos chronologically.
    Each dict: {image_bytes: bytes, date: str, angle: str}
    Returns structured comparison JSON.
    """
    photos_sorted = sorted(photos, key=lambda p: p["date"])
    n = len(photos_sorted)
    first_date = photos_sorted[0]["date"]
    last_date = photos_sorted[-1]["date"]
    angle = photos_sorted[0]["angle"]

    from datetime import date as dt_date
    try:
        d1 = dt_date.fromisoformat(first_date)
        d2 = dt_date.fromisoformat(last_date)
        duration_days = (d2 - d1).days
    except Exception:
        duration_days = 0

    photo_labels = "\n".join(
        f"  Photo {i+1}: {p['date']}" for i, p in enumerate(photos_sorted)
    )

    prompt = (
        f"You are given {n} {angle}-view body photos taken in chronological order:\n"
        f"{photo_labels}\n\n"
        f"Total duration: {duration_days} days ({duration_days // 7} weeks)\n\n"
        "Analyze the visual progression across ALL photos. Compare muscle definition, "
        "fat distribution, vascularity, posture, and overall body composition changes.\n\n"
        "Then assess: given the duration, what progress was realistically EXPECTED for someone "
        "doing consistent training and diet? Are the results ahead, on track, or behind expectations?\n\n"
        "Return ONLY valid JSON in this exact structure:\n"
        "{\n"
        '  "duration_days": number,\n'
        '  "overall": "improved" | "maintained" | "declined",\n'
        '  "improvements": ["specific observation 1", "..."],\n'
        '  "deimprovements": ["specific observation 1", "..."],\n'
        '  "bf_estimate_latest": "e.g. 14–17%",\n'
        '  "expected_in_duration": "What typical progress looks like in this timeframe",\n'
        '  "verdict": "Are results ahead / on track / behind expectations — 1–2 sentences",\n'
        '  "suggestions": ["actionable suggestion 1", "actionable suggestion 2", "..."]\n'
        "}\n\n"
        "Rules:\n"
        "- improvements and deimprovements: specific, observable, body-part-level detail\n"
        "- If no visible change in an area, omit it\n"
        "- suggestions: 3–5 concrete, prioritized actions\n"
        "- Output ONLY the JSON, no markdown fences"
    )

    content: list = [{"type": "text", "text": prompt}]
    for p in photos_sorted:
        content.append({"type": "image_url", "image_url": {"url": _img_url(p["image_bytes"])}})

    messages = [{"role": "user", "content": content}]
    text = await _chat(_VISION_MODEL, messages, api_key=api_key, system=_COMPARE_SYSTEM)
    result = _parse_json(text)
    result["duration_days"] = duration_days
    result["first_date"] = first_date
    result["last_date"] = last_date
    return result


_MACRO_SYSTEM = (
    "You are a registered dietitian with expert-level knowledge of food composition databases "
    "(USDA FoodData Central, NCCDB, IFCT 2017). "
    "You provide accurate macronutrient values scaled to the exact weight given. "
    "Return precise numbers, not estimates rounded to 5s.\n\n"
    "CRITICAL RULES FOR INDIAN COOKED FOOD:\n"
    "1. COOKING OIL/GHEE: Any curry, gravy, sabzi, fry, or tadka contains invisible cooking fat. "
    "Add 1–2 tbsp oil (120–240 kcal) per home-cooked serving; 2–3 tbsp for restaurant/dhaba. "
    "For a 200g chicken or paneer curry, fat should rarely be below 15–20g. "
    "If your fat estimate is under 12g for a cooked curry, recalculate — it is almost certainly wrong.\n"
    "2. COOKED RICE CARBS: Cooked white rice = ~28g carbs per 100g (NOT 40g — that is raw rice). "
    "250g cooked rice ≈ 70g carbs, ~325 kcal.\n"
    "3. PACKAGED ITEMS: Use the label values exactly. "
    "Home-cooked/restaurant items: estimate conservatively HIGH on calories.\n"
    "4. BIAS DIRECTION: When uncertain, round calories UP for cooked food, not down. "
    "Under-estimating intake sabotages a calorie deficit; a slight over-estimate is safer.\n"
    "5. CONSISTENCY CHECK: Before returning, verify: protein_g×4 + carbs_g×4 + fat_g×9 ≈ calories_kcal "
    "(within 5%). If they don't reconcile, recalculate.\n"
    "6. CONFIDENCE: Return a confidence level (high/medium/low) and the main source of uncertainty."
)

_MACRO_PROMPT_TEMPLATE = (
    "Calculate macros for {weight_g}g of {food_name}.{context_line}\n\n"
    "Steps:\n"
    "1. Identify per-100g values from nutritional databases (USDA / NCCDB / IFCT 2017)\n"
    "2. Scale proportionally to {weight_g}g\n"
    "3. For Indian cooked items: add invisible cooking oil/ghee per the system rules\n"
    "4. {cooking_instruction}\n"
    "5. Verify: protein_g×4 + carbs_g×4 + fat_g×9 must equal calories_kcal within 5%; fix if not\n"
    "6. When uncertain, round calories UP not down\n\n"
    "Return ONLY valid JSON:\n"
    '{{"calories_kcal": number, "protein_g": number, "carbs_g": number, "fat_g": number, '
    '"confidence": "high" | "medium" | "low", "uncertainty_note": "string"}}\n\n'
    "Output ONLY the JSON, no explanation."
)

_COOKING_INSTRUCTIONS: dict[str, str] = {
    "raw": "Food is raw — use uncooked nutritional values with no added fat.",
    "boiled": "Food is boiled — use cooked values, no added fat from cooking.",
    "steamed": "Food is steamed — use cooked values, no added fat from cooking.",
    "grilled": "Food is grilled — use cooked values, add minimal fat (~1 tsp oil/ghee for grilling).",
    "fried": "Food is pan-fried — add 1–2 tbsp oil/ghee (120–240 kcal) to the estimate.",
    "curry": "Food is in a curry/gravy — add 1–2 tbsp cooking oil/ghee plus spice base fat to the estimate.",
    "deep_fried": "Food is deep-fried — add 2–3 tbsp absorbed oil (240–360 kcal); fat should be significantly higher than uncooked.",
}


async def estimate_macros(
    food_name: str,
    weight_g: float,
    api_key: str,
    cooking_method: str | None = None,
    source_type: str | None = None,
) -> dict:
    """Fallback macro estimation when OpenFoodFacts has no match."""
    context_parts = []
    if cooking_method and cooking_method != "raw":
        context_parts.append(f"cooking method: {cooking_method.replace('_', '-')}")
    if source_type == "restaurant":
        context_parts.append("from a restaurant (use higher oil/butter estimates)")
    context_line = f" Context: {', '.join(context_parts)}." if context_parts else ""

    cooking_instruction = _COOKING_INSTRUCTIONS.get(
        cooking_method or "raw",
        "For Indian cooked items: add invisible cooking oil/ghee per the system rules.",
    )
    if source_type == "restaurant":
        cooking_instruction += " Restaurant cooking: add extra 30–40% oil/butter vs home cooking."

    prompt = _MACRO_PROMPT_TEMPLATE.format(
        food_name=food_name,
        weight_g=weight_g,
        context_line=context_line,
        cooking_instruction=cooking_instruction,
    )
    messages = [{"role": "user", "content": prompt}]
    text = await _chat(_TEXT_MODEL, messages, api_key=api_key, system=_MACRO_SYSTEM, max_tokens=200)
    return _parse_json(text)
