from fastapi import APIRouter

from app.api.v1.routes import auth, dashboard, patients, records, questions, surveys, registration, analytics

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(registration.router)
api_router.include_router(dashboard.router)
api_router.include_router(patients.router)
api_router.include_router(records.router)
api_router.include_router(questions.router)
api_router.include_router(surveys.router)
api_router.include_router(analytics.router)
