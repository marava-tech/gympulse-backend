# GymPulse Backend

FastAPI backend for the GymPulse AI fitness tracker.

## What It Does

- Authentication and profile APIs.
- Food logs, saved foods, saved meals, summaries, and streaks.
- Gym sessions, sleep logs, supplements, settings, and daily check-ins.
- Body photo and body analysis workflows.
- Notifications, email, FCM, AI analysis, MinIO, and OpenFoodFacts integrations.

## Stack

- FastAPI.
- MongoDB through Motor/PyMongo.
- Pydantic.
- Uvicorn.
- Python service modules under `routers/`, `models/`, and `services/`.

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

## Project Structure

```text
main.py        # FastAPI entrypoint
auth.py        # Auth helpers
database.py    # MongoDB connection
models/        # Data models
routers/       # API route modules
services/      # Email, FCM, AI, MinIO, OpenFoodFacts, TDEE
scripts/       # Migration/maintenance scripts
```

## Environment

Start from `.env.example` and keep real values local/server-side only.

Typical settings include:

```env
MONGODB_URI=replace-with-mongodb-uri
JWT_SECRET=replace-with-secure-secret
EMAIL_USERNAME=replace-with-email
EMAIL_PASSWORD=replace-with-app-password
FIREBASE_CREDENTIALS=replace-with-path-or-secret
MINIO_ENDPOINT=replace-with-minio-endpoint
```

## Verify

```bash
python3 -m compileall .
```

Add focused tests for changed business logic.

## Safety Rules

- Do not commit `.env*`, Firebase credentials, API keys, or database credentials.
- Keep API response changes synchronized with `../app`.
- Avoid logging sensitive health, body, or nutrition request data.

