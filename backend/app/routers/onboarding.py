"""Public conversational-onboarding API (deterministic steps first).

Reads the ONBOARDING_STEPS registry (app/onboarding/steps.py) and the onboarding
tables (users.onboarding_step, onboarding_answers, student_profile, countries,
curriculums, grades, subjects) to serve the 15-step flow.

- GET  /state            -> current step (options pre-resolved server-side)
- GET  /countries        -> country rows
- GET  /curriculums      -> curriculums for a country_code
- GET  /grades           -> grades for country_code + curriculum_id
- GET  /subjects         -> subjects for country_code + curriculum_id + grade_id
- POST /answer           -> validate + save an answer, advance, return next step

AI-assisted steps ("university", "career_name", "career_reason", "primary_goal") ask
the student's LettA agent for a short reply; the durable facts are then extracted
deterministically and persisted via the internal/ onboarding endpoints (the same
routes the registered LettA tools POST to). If LettA is unreachable the steps still
complete with a fallback reply.

The legacy 8-step onboarding router (app/api/onboarding.py) is only mounted when
ONBOARDING_ENGINE=legacy, at /api/v1/onboarding/legacy - untouched.
"""

from datetime import datetime, timezone
import logging
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_student
from app.letta_client import create_student_agent, send_onboarding_message
from app.models.catalog import Country, Curriculum, Grade, Subject
from app.models.onboarding_data import OnboardingAnswer, StudentProfile
from app.models.user import User
from app.onboarding.steps import ONBOARDING_STEPS, InputType, StepType

logger = logging.getLogger("novi.onboarding")

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

# Sentinel stored in users.onboarding_step once the flow is past the last step.
DONE_STEP = "completed"

AI_ASSISTED_STEPS = {"university", "career_name", "career_reason", "primary_goal"}

# Server-side option lists for the static:* options_source values. Expanded later.
STATIC_OPTIONS = {
    "saturday_activities": [
        "Hanging out with friends",
        "Gaming",
        "Sports",
        "Reading or watching something",
        "Creating art or music",
        "Exploring outdoors",
        "Coding or building things",
        "Helping out at home",
        "Cooking or baking",
        "Something else",
    ],
    "strengths": [
        "Creative",
        "Kind and patient",
        "A leader",
        "Good at sports",
        "Good with technology",
        "Curious and always learning",
        "A good friend",
        "Good at explaining things",
        "Organised",
        "Good at solving problems",
        "Another strength",
    ],
    "learning_style": [
        "Watch a video or listen to an explanation",
        "Read or take notes",
        "Try it hands-on",
        "Talk or write it through with someone",
        "Do practice questions",
        "Teach it to someone else",
        "Another way",
    ],
    "confidence": [
        "Usually confident",
        "Confident about some things",
        "Often doubt myself",
        "Better when I prepare first",
        "Hard to choose",
        "Something else",
    ],
    "career_stage": [
        "Yes, definitely",
        "I have a clear idea",
        "I have a few ideas",
        "No idea",
        "Rather not say",
    ],
}

# Novel transition line LettA is asked for on AI-assisted steps; used verbatim
# when LettA is unreachable or answers with no plain text.
FALLBACK_REPLIES = {
    "university": "Got it — I've noted that university down.",
    "career_name": "Nice — I've noted that down. What's drawing you to it?",
    "career_reason": "That's really helpful. What's one thing you'd want Novi to help you with?",
    "primary_goal": "Got it! I'll keep that front and center for you.",
}

INTERNAL_ROUTES = {
    "university": "university",
    "career_name": "career",
    "career_reason": "career",
    "primary_goal": "goal",
}


def _clean_university_text(text: str) -> str:
    """Drop common intro phrasing so "I want to study at Stanford" -> "Stanford"."""
    cleaned = re.sub(
        r"^(?:i\s+)?(?:would\s+)?(?:love|want|like|hope|dream of)\s+.*?\b(?:to\s+)?(?:study|go|join|attend)\s+(?:at|to)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^(?:my|the)\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip() or text.strip()


def _extract_facts(step: dict, value: Any) -> dict | None:
    """Deterministic extraction of the durable facts the AI-assisted steps feed
    into student_profile (written through the same internal endpoints the LettA
    tools call)."""
    step_id = step["id"]
    text = str(value).strip()

    if step_id in ("career_name", "career_reason", "primary_goal"):
        if not text:
            return None
        if step_id == "career_name":
            return {"career_name": text, "interest_reason_summary": ""}
        if step_id == "career_reason":
            return {"career_name": "", "interest_reason_summary": text}
        return {"goal_summary": text}

    if step_id == "university":
        if not text:
            return None
        m = re.match(r"^(.*?)\s*[,;\n]\s*(.+)$", text) or re.match(
            r"^(.*?\S+)\s+in\s+(\S.*)$", text
        )
        if m:
            name, location = m.group(1).strip(), m.group(2).strip()
        else:
            name, location = text.rstrip(".!?"), ""
        name = _clean_university_text(name)
        if not name:
            return None
        return {
            "university_name": name,
            "location": location,
            "confidence": "high" if name and location else "low",
        }

    return None


def _internal_onboarding_write(student_id: int, step_id: str, payload: dict) -> None:
    """Persist extracted facts via the internal/onboarding endpoints (exactly
    what the registered LettA tools POST to)."""
    route = INTERNAL_ROUTES[step_id]
    url = f"{settings.INTERNAL_CALLBACK_BASE_URL.rstrip('/')}/internal/onboarding/{student_id}/{route}"
    response = httpx.post(
        url,
        json=payload,
        headers={"X-Internal-Secret": settings.INTERNAL_SHARED_SECRET},
        timeout=10.0,
    )
    response.raise_for_status()


class AnswerIn(BaseModel):
    step_id: str
    value: Any = None


def _step_by_id(step_id: str) -> dict | None:
    return next((s for s in ONBOARDING_STEPS if s["id"] == step_id), None)


def _current_step(user: User) -> dict | None:
    step_id = user.onboarding_step or ONBOARDING_STEPS[0]["id"]
    return _step_by_id(step_id)


def _profile(db: Session, user: User) -> StudentProfile | None:
    return db.get(StudentProfile, user.id)


def _options(db: Session, step: dict, profile: StudentProfile | None) -> list[dict]:
    """Resolve server-side options for a step, cascading on prior answers.

    The frontend never chains calls: whatever step /state returns already has
    the correct, filtered options.
    """
    source = step.get("options_source") or ""
    has = lambda profile, attr: profile is not None and getattr(profile, attr) is not None

    if source == "countries":
        rows = db.scalars(select(Country).order_by(Country.name)).all()
        return [{"value": r.code, "label": r.name} for r in rows]

    if source == "curriculums":
        if has(profile, "country_code"):
            rows = db.scalars(
                select(Curriculum)
                .where(Curriculum.country_code == profile.country_code)
                .order_by(Curriculum.name)
            ).all()
            return [{"value": r.id, "label": r.name} for r in rows]
        return []

    if source == "grades":
        if has(profile, "country_code") and has(profile, "curriculum_id"):
            rows = db.scalars(
                select(Grade).where(
                    Grade.country_code == profile.country_code,
                    Grade.curriculum_id == profile.curriculum_id,
                )
            ).all()
            ordered = sorted(rows, key=lambda r: int(r.normalized_level or 0))
            return [{"value": r.id, "label": r.label} for r in ordered]
        return []

    if source == "subjects":
        if has(profile, "country_code") and has(profile, "curriculum_id") and has(profile, "grade_id"):
            rows = db.scalars(
                select(Subject).where(
                    Subject.country_code == profile.country_code,
                    Subject.curriculum_id == profile.curriculum_id,
                    Subject.grade_id == profile.grade_id,
                ).order_by(Subject.name)
            ).all()
            return [{"value": r.id, "label": r.name} for r in rows]
        return []

    if source.startswith("static:"):
        key = source.split(":", 1)[1]
        return [{"value": label, "label": label} for label in STATIC_OPTIONS.get(key, [])]

    return []


def _validate(step: dict, value: Any, options: list[dict]) -> None:
    input_type = step["input_type"]
    valid_values = {o["value"] for o in options}

    if input_type == InputType.TEXT:
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(status_code=422, detail="Expected a text answer")
        return

    if input_type in (InputType.SINGLE_SELECT, InputType.SEARCHABLE_SELECT):
        if not isinstance(value, str) or value not in valid_values:
            raise HTTPException(status_code=422, detail="Value must be one of the provided options")
        return

    if input_type == InputType.MULTI_SELECT:
        if not isinstance(value, list) or not value:
            raise HTTPException(status_code=422, detail="Select at least one option")
        if not all(isinstance(v, str) for v in value):
            raise HTTPException(status_code=422, detail="All selections must be valid options")
        # The conversational frontend lets students pick an "Other"/"Something
        # else" option and type their own value, which is submitted as one
        # free-text entry alongside (or instead of) the canned options.
        custom = [v for v in value if v not in valid_values]
        if len(custom) > 1:
            raise HTTPException(status_code=422, detail="All selections must be valid options")
        return

    raise HTTPException(status_code=422, detail="Unsupported input type")


def _prefill(db: Session, user: User, step: dict) -> Any:
    ans = db.scalar(
        select(OnboardingAnswer)
        .where(
            OnboardingAnswer.student_id == user.id,
            OnboardingAnswer.step_id == step["id"],
        )
        .order_by(OnboardingAnswer.created_at.desc(), OnboardingAnswer.id.desc())
        .limit(1)
    )
    return ans.raw_value if ans else None


def _state_payload(db: Session, step: dict, user: User, profile: StudentProfile | None = None) -> dict:
    profile = profile if profile is not None else _profile(db, user)
    return {
        "step_id": step["id"],
        "order": step["order"],
        "type": step["type"].value,
        "question": step["question"],
        "input_type": step["input_type"].value,
        "options": _options(db, step, profile),
        "prefill": _prefill(db, user, step),
    }


@router.get("/state")
def onboarding_state(
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    # Create Letta agent if needed (first time hitting state or answer).
    # If LettA is unavailable the flow still works — the AI steps degrade to the
    # deterministic fallback and LettA is retried on the next request.
    if user.letta_agent_id is None:
        try:
            user.letta_agent_id = create_student_agent(str(user.id))
            db.commit()
            logger.info("created LettA agent %s for student %s", user.letta_agent_id, user.id)
        except Exception as exc:
            db.rollback()
            logger.warning("could not create LettA agent for student %s: %s", user.id, exc)

    step = _current_step(user)
    if step is None:
        return {"completed": True}
    return _state_payload(db, step, user)


@router.get("/countries")
def list_countries(
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    rows = db.scalars(select(Country).order_by(Country.name)).all()
    return [{"code": r.code, "name": r.name} for r in rows]


@router.get("/curriculums")
def list_curriculums(
    country_code: str = Query(..., description="ISO 3166 alpha-2 country code"),
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(Curriculum).where(Curriculum.country_code == country_code).order_by(Curriculum.name)
    ).all()
    return [{"id": r.id, "country_code": r.country_code, "name": r.name} for r in rows]


@router.get("/grades")
def list_grades(
    country_code: str = Query(...),
    curriculum_id: str = Query(...),
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(Grade).where(
            Grade.country_code == country_code,
            Grade.curriculum_id == curriculum_id,
        )
    ).all()
    ordered = sorted(rows, key=lambda r: int(r.normalized_level or 0))
    return [
        {"id": r.id, "country_code": r.country_code, "curriculum_id": r.curriculum_id,
         "label": r.label, "normalized_level": r.normalized_level}
        for r in ordered
    ]


@router.get("/subjects")
def list_subjects(
    country_code: str = Query(...),
    curriculum_id: str = Query(...),
    grade_id: str = Query(...),
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(Subject).where(
            Subject.country_code == country_code,
            Subject.curriculum_id == curriculum_id,
            Subject.grade_id == grade_id,
        ).order_by(Subject.name)
    ).all()
    return [
        {"id": r.id, "country_code": r.country_code, "curriculum_id": r.curriculum_id,
         "grade_id": r.grade_id, "name": r.name}
        for r in rows
    ]


@router.post("/answer")
def submit_answer(
    data: AnswerIn,
    user: User = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    # Ensure Letta agent exists
    if user.letta_agent_id is None:
        try:
            user.letta_agent_id = create_student_agent(str(user.id))
            db.commit()
            logger.info("created LettA agent %s for student %s", user.letta_agent_id, user.id)
        except Exception as exc:
            db.rollback()
            logger.warning("could not create LettA agent for student %s: %s", user.id, exc)

    step = _step_by_id(data.step_id)
    current = _current_step(user)
    if step is None or current is None or step["id"] != current["id"]:
        raise HTTPException(status_code=404, detail="Step not found or not the current step")

    profile = _profile(db, user)
    options = _options(db, step, profile)
    _validate(step, data.value, options)

    db.add(OnboardingAnswer(student_id=user.id, step_id=step["id"], raw_value=data.value))

    if profile is None:
        profile = StudentProfile(student_id=user.id)
        db.add(profile)

    letta_reply = None

    if step["type"] == StepType.DETERMINISTIC:
        if step["id"] == "career":
            profile.has_career_in_mind = data.value != "No idea"
        else:
            setattr(profile, step["save_field"], data.value)
    else:  # AI_ASSISTED
        if step["id"] == "university":
            profile.university_raw_text = data.value

        # 1) Ask LettA to extract + reply (best-effort). LettA's own agent loop
        #    can't reliably run tools against Gemini 3.x (MEMORY_SYSTEM.md §1),
        #    so a quiet failure here must not block the onboarding.
        letta_reply = None
        if user.letta_agent_id:
            letta_reply = send_onboarding_message(user.letta_agent_id, step["id"], data.value)

        # 2) Persist the durable facts through the internal endpoints (the same
        #    routes the registered LettA tools POST to). Deterministic so the
        #    extracted profile lands regardless of the agent loop state.
        extracted = _extract_facts(step, data.value)
        if extracted:
            if step["id"] == "career_reason" and profile:
                extracted["career_name"] = profile.career_name or ""
            try:
                _internal_onboarding_write(user.id, step["id"], extracted)
                logger.info("persisted onboarding step '%s' for student %s", step["id"], user.id)
            except Exception as exc:
                logger.warning("failed to persist step '%s' for student %s: %s", step["id"], user.id, exc)

        # 3) Reply line: LettA's own words when it answered, else a warm line.
        if not letta_reply:
            letta_reply = FALLBACK_REPLIES.get(step["id"], "Got it — let's keep going!")

    # Handle branching after "career" step
    # If career answer is "No idea", skip career_name and career_reason, go to primary_goal
    if step["id"] == "career":
        if data.value != "No idea":
            # Has career in mind -> go to career_name
            career_idx = next(i for i, s in enumerate(ONBOARDING_STEPS) if s["id"] == "career")
            nxt = ONBOARDING_STEPS[career_idx + 1]  # career_name
        else:
            # No idea -> skip to primary_goal
            nxt = _step_by_id("primary_goal")
    elif step["id"] in ("career_name", "career_reason"):
        # Normal progression through the new career sub-steps
        idx = ONBOARDING_STEPS.index(step)
        nxt = ONBOARDING_STEPS[idx + 1] if idx + 1 < len(ONBOARDING_STEPS) else None
    else:
        # All other steps use normal sequential progression
        idx = ONBOARDING_STEPS.index(step)
        nxt = ONBOARDING_STEPS[idx + 1] if idx + 1 < len(ONBOARDING_STEPS) else None
    
    if nxt is None:
        user.onboarding_step = DONE_STEP
        user.onboarding_completed_at = datetime.now(timezone.utc)
        db.commit()
        return {"completed": True}

    user.onboarding_step = nxt["id"]
    db.commit()
    
    payload = _state_payload(db, nxt, user, profile)
    if letta_reply:
        payload["letta_reply"] = letta_reply
    return payload