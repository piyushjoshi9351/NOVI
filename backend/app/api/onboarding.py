from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
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
from app.services import voice, voice_resolve
from app.services.onboarding_flow import STEP_BY_ID

# Flow + voice onboarding engine (app/services/onboarding_flow.py). Mounted at
# /onboarding by default (ONBOARDING_ENGINE != "new" in app/api/router.py). The
# parallel conversational engine (app/routers/onboarding.py) replaces it at
# /onboarding only when ONBOARDING_ENGINE=new.
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


class SpeakIn(BaseModel):
    text: str


class VoiceAnswerIn(BaseModel):
    step_id: str
    transcript: str


@router.post("/voice/speak")
async def voice_speak(
    data: SpeakIn,
    user: User = Depends(get_current_student),
):
    stream = voice.speak(data.text)

    try:
        first = await stream.__anext__()
    except StopAsyncIteration:
        raise HTTPException(status_code=502, detail="No audio produced")
    except voice.VoiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    async def _stream():
        yield first
        async for chunk in stream:
            yield chunk

    return StreamingResponse(_stream(), media_type="audio/mpeg")


@router.post("/voice/transcribe")
async def voice_transcribe(
    file: UploadFile = File(...),
    user: User = Depends(get_current_student),
):
    try:
        audio = await file.read()
        text = await voice.transcribe(audio, file.filename or "audio.webm")
    except voice.VoiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"text": text}


@router.post("/voice/answer")
async def voice_answer(
    data: VoiceAnswerIn,
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    step = STEP_BY_ID.get(data.step_id)
    if not step:
        raise HTTPException(status_code=404, detail="Unknown step.")

    resolved = await voice_resolve.resolve_answer(step, data.transcript)
    if resolved is None or (isinstance(resolved, list) and not resolved):
        return {
            "resolved": False,
            "error": "Couldn't map your spoken answer to a valid option — please try again.",
        }

    if step.kind == "multi":
        values = resolved if isinstance(resolved, list) else [resolved]
        result = await flow_service.submit_multi(db, user, step.id, values)
    else:
        result = await flow_service.submit_answer(db, user, step.id, resolved)
    return {"resolved": True, "resolved_value": resolved, **result}