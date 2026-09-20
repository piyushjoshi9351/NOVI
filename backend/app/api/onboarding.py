from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_student
from app.models.user import User
from app.schemas.onboarding import (
    AnswerIn,
    FlowOut,
    FlowState,
    OnboardingOut,
    SkipIn,
)
from app.services import onboarding as onboarding_service
from app.services import onboarding_flow as flow_service

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.get("", response_model=OnboardingOut)
async def get_onboarding(
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    return onboarding_service.onboarding(db, user)


@router.get("/flow", response_model=FlowState)
async def get_flow(
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    return flow_service.get_flow(db, user)


@router.post("/flow/start", response_model=FlowState)
async def start_flow(
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    return flow_service.start_flow(db, user)


@router.post("/flow/answer", response_model=FlowOut)
async def answer_step(
    data: AnswerIn,
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    if data.values:
        return await flow_service.submit_multi(db, user, data.step_id, data.values)
    return await flow_service.submit_answer(db, user, data.step_id, data.answer)


@router.post("/flow/skip", response_model=FlowOut)
async def skip_step(
    data: SkipIn,
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    return await flow_service.skip_step(db, user, data.step_id)


@router.post("/flow/reset", response_model=FlowState)
async def reset_flow(
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    return flow_service.reset_flow(db, user)