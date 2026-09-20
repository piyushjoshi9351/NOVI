from fastapi import APIRouter

from app.api import (
    auth,
    career_dna,
    careers,
    chat,
    checkins,
    dashboard,
    google_auth,
    memory,
    onboarding,
    parents,
    passport,
    roadmap,
    universities,
)
from app.core.config import settings
from app.m3.api.routes import router as m3_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(google_auth.router)
api_router.include_router(chat.router)
api_router.include_router(careers.router)
api_router.include_router(universities.router)
api_router.include_router(career_dna.router)
api_router.include_router(roadmap.router)
api_router.include_router(passport.router)
api_router.include_router(checkins.router)
api_router.include_router(dashboard.router)
api_router.include_router(parents.router)
api_router.include_router(memory.router)

# Onboarding: pick exactly one engine at startup. Default ("legacy") mounts the
# flow + voice engine (app/api/onboarding.py -> app/services/onboarding_flow.py)
# at /onboarding; "new" mounts the parallel conversational engine
# (app/routers/onboarding.py) at /onboarding instead. Either can be hot-swapped
# by setting ONBOARDING_ENGINE and redeploying (see README).
if settings.ONBOARDING_ENGINE == "new":
    from app.routers.onboarding import router as onboarding_flow_router

    api_router.include_router(onboarding_flow_router)
else:
    api_router.include_router(onboarding.router)

api_router.include_router(m3_router, prefix="/m3")