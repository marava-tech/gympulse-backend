"""User settings — API key management."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ApiKeyBody(BaseModel):
    api_key: str


@router.put("/api-key", status_code=204)
async def save_api_key(body: ApiKeyBody, user_id: str = Depends(get_current_user)):
    db = get_db()
    await db.user_profile.update_one(
        {"user_id": user_id},
        {"$set": {"openrouter_api_key": body.api_key.strip()}},
    )


@router.get("/api-key")
async def get_api_key(user_id: str = Depends(get_current_user)):
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    key = (profile or {}).get("openrouter_api_key")
    if not key:
        return {"set": False, "masked": None}
    masked = "•" * (len(key) - 4) + key[-4:]
    return {"set": True, "masked": masked}


@router.post("/test-api-key")
async def test_api_key(body: ApiKeyBody, user_id: str = Depends(get_current_user)):
    from services.gemini import _chat, _TEXT_MODEL
    try:
        res = await _chat(
            model=_TEXT_MODEL,
            messages=[{"role": "user", "content": "Respond with exactly the word: 'Success'"}],
            api_key=body.api_key.strip(),
            max_tokens=10
        )
        if "success" in res.lower():
            return {"valid": True, "message": "API key verified successfully. AI is responding."}
        else:
            return {"valid": False, "message": f"Unexpected response: {res}"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"API key verification failed: {str(e)}")
