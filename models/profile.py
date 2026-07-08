from pydantic import BaseModel, Field
from typing import Optional, List


class NotificationPrefs(BaseModel):
    if_window: bool = True
    sleep_prompt: bool = True
    weight_reminder: bool = True
    gym_photo_nudge: bool = True
    weekly_summary: bool = True
    daily_checkin_reminder: bool = True  # 10pm summary: today's kcal + protein + remaining
    end_of_day_reconcile: bool = True    # 8:30pm nudge if < 2 meals logged today


class SleepThresholds(BaseModel):
    worst_max: float = 4.0   # < this → worst
    bad_max: float = 6.0     # < this → bad
    average_max: float = 7.0 # < this → average
    good_max: float = 8.0    # < this → good; >= this → better


class FeatureFlags(BaseModel):
    """Optional tracking features — all default off. GymPulse's core is nutrition tracking."""
    weight_tracking: bool = False
    sleep_tracking: bool = False
    progress_photos: bool = False
    health_sync: bool = False


class ProfileCreate(BaseModel):
    height_cm: float
    weight_kg: float
    age: int
    sex: str  # "male" | "female"
    eating_window_start: str = "13:00"  # HH:MM
    eating_window_end: str = "21:00"
    user_timezone: str = "UTC"  # IANA timezone, e.g. "Asia/Kolkata"
    notification_prefs: NotificationPrefs = Field(default_factory=NotificationPrefs)
    gym_streak_min_days_per_week: int = 5
    sleep_thresholds: SleepThresholds = Field(default_factory=SleepThresholds)
    photo_url: Optional[str] = None
    gym_days: Optional[List[int]] = None  # 0=Mon … 6=Sun
    if_enabled: bool = False
    subtract_bowl_weight: bool = False
    openrouter_api_key: Optional[str] = None
    feature_flags: FeatureFlags = Field(default_factory=FeatureFlags)


class ProfilePatch(BaseModel):
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    age: Optional[int] = None
    sex: Optional[str] = None
    eating_window_start: Optional[str] = None
    eating_window_end: Optional[str] = None
    user_timezone: Optional[str] = None
    notification_prefs: Optional[NotificationPrefs] = None
    fcm_token: Optional[str] = None
    gym_streak_min_days_per_week: Optional[int] = None
    sleep_thresholds: Optional[SleepThresholds] = None
    photo_url: Optional[str] = None
    # Manual goal overrides (skip TDEE recalc for these when provided)
    goal_kcal: Optional[int] = None
    protein_g: Optional[int] = None
    carbs_g: Optional[int] = None
    fat_g: Optional[int] = None
    # Gym / rest day schedule (0=Mon … 6=Sun)
    gym_days: Optional[List[int]] = None
    gym_goal_kcal: Optional[int] = None
    gym_protein_g: Optional[int] = None
    gym_carbs_g: Optional[int] = None
    gym_fat_g: Optional[int] = None
    rest_goal_kcal: Optional[int] = None
    rest_protein_g: Optional[int] = None
    rest_carbs_g: Optional[int] = None
    rest_fat_g: Optional[int] = None
    if_enabled: Optional[bool] = None
    subtract_bowl_weight: Optional[bool] = None
    openrouter_api_key: Optional[str] = None
    disabled_meal_slots: Optional[List[str]] = None  # e.g. ["breakfast"]
    feature_flags: Optional[FeatureFlags] = None


class TDEEResult(BaseModel):
    tdee_kcal: int
    goal_kcal: int      # TDEE + 75 (recomp)
    protein_g: int      # 2g/kg
    carbs_g: int
    fat_g: int
    real_tdee_kcal: Optional[int] = None  # adaptive, derived from intake+weight data
    tdee_source: str = "formula"          # "formula" | "adaptive"
