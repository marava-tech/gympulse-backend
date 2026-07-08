"""Shared utility helpers."""
import logging
import os
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException, UploadFile

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

# Magic bytes for supported image formats
_IMAGE_MAGIC: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG", "PNG"),
    (b"RIFF", "WebP"),  # WebP starts with RIFF....WEBP
]


def get_openrouter_key(profile: dict | None) -> str | None:
    """Return the user's personal OpenRouter key, falling back to the server-wide key."""
    return (profile or {}).get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY")


def require_feature_enabled(profile: dict | None, flag: str, label: str) -> None:
    """Raise HTTP 403 unless the given feature_flags.<flag> is true on the profile."""
    flags = (profile or {}).get("feature_flags") or {}
    if not flags.get(flag, False):
        raise HTTPException(403, f"{label} is disabled — enable it in Settings first")


def parse_object_id(value: str, label: str = "ID") -> ObjectId:
    """Parse a string into ObjectId; raises HTTP 400 on invalid format."""
    try:
        return ObjectId(value)
    except (InvalidId, Exception):
        raise HTTPException(400, f"Invalid {label}: '{value}' is not a valid ID")


def validate_image_upload(image_bytes: bytes, filename: str, content_type: str | None) -> None:
    """Raise HTTP 400 if file exceeds size limit or is not a recognised image."""
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"Image too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB)")

    # Validate by magic bytes — ignore unreliable MIME type from multipart headers
    # (Android reports image/jpg, some pickers send application/octet-stream, etc.)
    header = image_bytes[:12]
    for magic, _ in _IMAGE_MAGIC:
        if header.startswith(magic):
            return
    # WebP needs an extra check: bytes 8-12 must be "WEBP"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return

    raise HTTPException(400, "Unsupported file type. Please upload a JPEG, PNG, or WebP image.")
