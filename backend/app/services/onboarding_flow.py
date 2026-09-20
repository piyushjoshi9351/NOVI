"""Conversational onboarding — the "get to know you" flow.

Novi asks a scripted but friendly set of questions (Q1-30, condensed into
mandatory + optional steps). The whole Q&A is mirrored into a real chat
Conversation, so it shows up in the student's chat history and the Career DNA
finalize step reads that same transcript through the existing chat pipeline:

  finalize -> refresh_dna_from_history() (LLM -> Career DNA + rescore)
           -> match_careers()             (persist career matches)
           -> onboarding completion LLM  (archetype, recommendations, 30-day plan)

Progress is deterministic: answered + skipped steps / total steps = 0..100%.
"""

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.onboarding_session import OnboardingSession
from app.models.user import User
from app.llm import prompts
from app.services import chat as chat_service
from app.services.providers import dna_dict, gemini, user_context


@dataclass
class Step:
    id: str
    section: str
    question: str
    kind: str  # single | multi | free
    options: list[str] | None = None
    optional: bool = False
    max_select: int | None = None
    hint: str | None = None


STEPS: list[Step] = [
    Step("name", "Basic profile", "What's your name? 👋", "free", hint="Just your first name is fine."),
    Step("grade", "Basic profile", "What grade are you in?", "single",
         ["Grade 9", "Grade 10", "Grade 11", "Grade 12"]),
    Step("curriculum", "Basic profile", "Which school curriculum do you follow?", "single",
         ["CBSE", "ICSE", "IB", "IGCSE", "State Board", "Other"]),
    Step("intent", "Why you're here", "What made you join Novi?", "single",
         ["🤷 I have no idea what I want to do",
          "🎯 I have a career in mind",
          "🎓 I have a university in mind",
          "🌎 I want to study abroad",
          "🧠 I want to understand myself better",
          "🚀 I want to build a stronger profile",
          "👨\u200d👩\u200d👧 My parents asked me to",
          "👀 I'm just curious"]),
    Step("saturday", "What you enjoy", "What could you happily spend an entire Saturday doing?", "multi",
         ["Playing sports", "Gaming", "Coding / building things", "Drawing / designing",
          "Making videos", "Reading", "Watching YouTube", "Music", "Hanging out with friends",
          "Starting something", "Helping people", "Learning random things", "Travelling / exploring",
          "Something else"]),
    Step("videos", "Curiosity", "What kind of videos do you usually end up watching?", "multi",
         ["Science", "Technology / AI", "Business", "Sports", "Gaming", "Fashion",
          "Psychology", "History", "News", "Comedy", "Design", "Cars", "Finance", "Other"]),
    Step("lose_track", "Flow", "What is something you can do for hours without getting bored? 🕰️",
         "free", hint="e.g. 'I can spend 5 hours editing videos.'"),
    Step("friend_describe", "Strengths", "Your best friend has to describe you. What would they say you're really good at?", "multi",
         ["Explaining things", "Making people laugh", "Solving problems", "Making things",
          "Leading", "Listening", "Persuading people", "Organising", "Creating",
          "Learning quickly", "Staying calm", "Helping people", "Thinking differently"]),
    Step("hard_problem", "How you think", "You get a difficult problem you've never seen before. What do you usually do?", "single",
         ["Figure it out myself",
          "Search for an answer",
          "Ask someone",
          "Try different things until something works",
          "Avoid it until I have to"]),
    Step("compete_vs_create", "How you think", "Which sounds more exciting?", "single",
         ["Winning a competition 🏆", "Building something nobody has built before 🚀"]),
    Step("people_vs_problems", "How you think", "Which sounds more interesting?", "single",
         ["Understanding why people behave the way they do",
          "Solving how something works",
          "Creating something beautiful",
          "Building a business",
          "Helping someone overcome a problem",
          "Discovering something nobody knows"]),
    Step("motivators", "What motivates you", "Which matter most to you? (pick up to 3)", "multi",
         ["Making money", "Freedom", "Helping people", "Being successful", "Creating something",
          "Recognition", "Learning", "Adventure", "Stability", "Making an impact",
          "Being the best", "Working with interesting people"],
         max_select=3),
    Step("future_30", "Your future", "Imagine you're 30 and everything has gone really well. What would your life look like? ✨",
         "free", hint="Describe the life you'd love — big or small."),
    Step("money_no_bars", "What drives you", "If you knew you'd be financially successful no matter what you chose, what would you spend your time doing?", "free"),
    Step("subjects_enjoy", "Academics", "Which subjects do you actually enjoy?", "multi",
         ["Maths", "Physics", "Chemistry", "Biology", "Computer Science", "English",
          "History", "Geography", "Economics", "Business", "Art", "Music",
          "Physical Education", "Other"]),
    Step("subjects_hard", "Academics", "Which subjects do you find difficult?", "multi",
         ["Maths", "Physics", "Chemistry", "Biology", "Computer Science", "English",
          "History", "Geography", "Economics", "Business", "Art", "Music",
          "Physical Education", "None really"]),
    Step("subject_marks_irrelevant", "Academics", "Which subject would you pick if you didn't have to worry about marks at all?", "free"),
    Step("academics", "Academics", "How are things going in school lately? (grades, subjects you love or struggle with)", "free",
         optional=True, hint="Optional — rough answers are fine."),
    Step("learning_style", "Learning", "When you want to learn something new, what do you usually do?", "single",
         ["Watch videos", "Read about it", "Try it myself", "Ask someone",
          "Take a course", "A mixture of these"]),
    Step("failure", "Resilience", "You work really hard on something and it doesn't work. What do you usually do?", "single",
         ["Try again", "Figure out what went wrong", "Ask someone", "Move on", "Get frustrated"]),
    Step("confidence", "Confidence", "Which feels most like you?", "single",
         ["I usually know what I'm good at",
          "I'm still figuring it out",
          "I know what I want, but I'm not sure I'm good enough",
          "I don't really know what I'm good at yet"]),
    Step("activities", "Outside school", "What do you currently do outside school?", "multi",
         ["Sport", "Music", "Dance", "Debate", "MUN", "Coding", "Art", "Theatre",
          "Volunteering", "Business", "Content creation", "Research", "Competitions",
          "Nothing yet"]),
    Step("activity_best", "Outside school", "Which one do you enjoy most?", "free"),
    Step("activity_seriousness", "Outside school", "How seriously do you pursue it?", "single",
         ["Casual", "Regular", "Competitive", "Advanced"]),
    Step("proud_project", "Profile", "Have you ever built or created something you're proud of?", "single",
         ["Yes 🎉", "Not yet"]),
    Step("project_detail", "Profile", "Tell me about it — I'd love to hear what you made.", "free"),
    Step("study_destination", "University", "Where would you ideally like to study?", "single",
         ["India", "US", "UK", "Canada", "Europe", "Asia", "Australia", "Not sure yet"]),
    Step("dream_universities", "University", "Any universities you've heard of or dream about? 🎓", "free"),
    Step("career_awareness", "Career", "Do you currently have a career in mind?", "single",
         ["Yes, definitely", "A few ideas", "Maybe", "No idea"]),
    Step("career_detail", "Career", "What is it?", "free"),
    Step("career_reason", "Career", "What makes you interested in it? (No wrong answers — I just want to understand the 'why'.)", "free"),
    Step("parent_influence", "Around you", "How involved are your parents in decisions about your future?", "single",
         ["They decide with me", "They advise me", "They have strong expectations",
          "They mostly leave it to me", "I'm not sure"]),
    Step("worried", "The important one", "What are you most worried about when you think about your future?", "single",
         ["Choosing the wrong career", "Not getting good enough marks",
          "Not getting into a good university", "Disappointing my parents",
          "Not knowing what I want", "Money", "Competition", "I'm not worried",
          "Something else"]),
    Step("novi_helper", "The final one", "If Novi could help you with ONE thing over the next year, what would you want it to be?", "free"),
]

STEP_BY_ID = {s.id: s for s in STEPS}
TOTAL = len(STEPS)

_MANDATORY = {s.id for s in STEPS if not s.optional}

_GRADE_RE = re.compile(r"(\d{1,2})")

# Steps that only make sense when an earlier answer opens them up.
_CONDITIONAL = {
    "activity_best": ("activities", lambda v: _has(v, "Nothing yet") and len(v) <= 1),
    "activity_seriousness": ("activities", lambda v: _has(v, "Nothing yet") and len(v) <= 1),
    "project_detail": ("proud_project", lambda v: str(v or "").strip().lower().startswith("no")),
    "career_detail": ("career_awareness", lambda v: str(v or "").strip().lower() in ("no idea", "no")),
    "career_reason": ("career_awareness", lambda v: str(v or "").strip().lower() in ("no idea", "no")),
}


def _has(values, needle: str) -> bool:
    return needle in [str(v).strip().lower() for v in (values or [])]


def get_session(user: User, db: Session) -> OnboardingSession | None:
    return db.scalar(select(OnboardingSession).where(OnboardingSession.user_id == user.id))


def _state(session: OnboardingSession | None) -> dict:
    if not session:
        return {"started": False, "done": False, "status": "none", "conversation_id": None,
                "percent": 0, "answered": 0, "total": TOTAL, "current": None,
                "transcript": [], "summary": None}

    answers = session.answers or {}
    skipped = session.skipped or []
    done = session.status == "done" and session.summary is not None
    completed = len(answers) + len([s for s in skipped if s in STEP_BY_ID])
    current = None
    if not done:
        idx = session.step_index or 0
        if idx < TOTAL:
            step = STEPS[idx]
            current = _step_out(step)

    return {
        "started": True,
        "done": done,
        "status": session.status,
        "conversation_id": session.conversation_id,
        "percent": min(100, round(completed / TOTAL * 100)),
        "answered": len(answers),
        "total": TOTAL,
        "current": current,
        "transcript": _transcript(session, answers, skipped),
        "summary": session.summary,
    }


def _step_out(step: Step) -> dict:
    return {
        "id": step.id,
        "question": step.question,
        "kind": step.kind,
        "options": step.options,
        "optional": step.optional,
        "max_select": step.max_select,
        "hint": step.hint,
        "section": step.section,
    }


def _format_answer(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value or "")


def _transcript(session: OnboardingSession, answers: dict, skipped: list) -> list[dict]:
    """Reconstruct the conversation so far: Novi question + student answer (or skip)."""
    out: list[dict] = []
    upto = session.step_index or 0
    if session.status == "done":
        upto = TOTAL
    for i in range(min(upto, TOTAL)):
        step = STEPS[i]
        if step.id in answers:
            out.append({"role": "assistant", "content": step.question})
            out.append({"role": "user", "content": _format_answer(answers[step.id])})
        elif step.id in skipped:
            out.append({"role": "assistant", "content": step.question})
            out.append({"role": "user", "content": "⏭ Skipped", "skipped": True})
    return out


def start_flow(db: Session, user: User) -> dict:
    session = get_session(user, db)
    if not session:
        session = OnboardingSession(user_id=user.id, status="active", step_index=0,
                                    answers={}, skipped=[], summary=None)
        db.add(session)
        db.commit()
        db.refresh(session)
    return _state(session)


def get_flow(db: Session, user: User) -> dict:
    return _state(get_session(user, db))


def _advance(session: OnboardingSession, answers: dict, skipped: list) -> int:
    idx = session.step_index or 0
    if idx >= TOTAL:
        return TOTAL

    answers = answers or {}
    skipped = skipped or []

    nxt = idx
    while nxt < TOTAL:
        step = STEPS[nxt]
        if step.id in answers or step.id in skipped:
            nxt += 1  # already handled — move on
            continue
        cond = _CONDITIONAL.get(step.id)
        if cond and nxt > 0 and cond[1](answers.get(cond[0])):
            nxt += 1  # auto-skip steps the earlier answer rules out
            continue
        return nxt
    return TOTAL


async def submit_answer(db: Session, user: User, step_id: str, raw: str) -> dict:
    step = STEP_BY_ID.get(step_id)
    if not step:
        return {**_state(get_session(user, db)), "error": "Unknown step."}
    session = get_session(user, db)
    if not session:
        session = None
        start_flow(db, user)
        session = get_session(user, db)

    answers = dict(session.answers or {})
    skipped = list(session.skipped or [])

    if step_id in answers:  # idempotent — don't double-record
        return {**_state(session), "error": None}

    value = _coerce(step, raw.strip())
    if value is None:
        return {**_state(session), "error": "That answer doesn't look right — try tapping an option."}

    answers[step_id] = value
    _apply_profile_side_effects(db, user, step_id, value)

    # Mirror the Q&A into the student's real chat conversation ("use the chat").
    conversation = chat_service.get_or_create_conversation(
        user, session.conversation_id, step.question, db
    )
    if conversation.title == "New Chat" or conversation.id != session.conversation_id:
        conversation.title = "Get to know you · Novi onboarding"
    session.conversation_id = conversation.id
    _append_step_messages(db, conversation.id, step, value)

    session.step_index = _advance(session, answers, skipped)
    session.answers = answers

    if session.step_index >= TOTAL:
        session.status = "complete"
        done = True
    else:
        done = False

    db.commit()
    db.refresh(session)

    if done:
        return await _finalize(db, user, session, answers, skipped)

    return {**_state(session), "error": None}


async def skip_step(db: Session, user: User, step_id: str) -> dict:
    session = get_session(user, db)
    if not session:
        return {"error": "No onboarding started."}
    step = STEP_BY_ID.get(step_id)
    if not step:
        return {**_state(session), "error": "Unknown step."}
    if step_id in (session.answers or {}):
        return {**_state(session), "error": None}
    if not step.optional:
        return {**_state(session), "error": "Nope — this one I really need to hear 🤍"}

    skipped = list(session.skipped or [])
    if step_id not in skipped:
        skipped.append(step_id)
    session.skipped = skipped
    session.step_index = _advance(session, session.answers or {}, skipped)
    if session.step_index >= TOTAL:
        session.status = "complete"
        db.commit()
        db.refresh(session)
        return await _finalize(db, user, session, session.answers or {}, skipped)
    db.commit()
    db.refresh(session)
    return {**_state(session), "error": None}


def _coerce(step: Step, raw: str):
    if not raw:
        return None
    if step.kind == "single":
        if step.options:
            lowered = {str(o).lower(): o for o in step.options}
            hit = lowered.get(raw.lower())
            return hit if hit is not None else None
        return raw
    if step.kind == "free":
        return raw
    return raw  # multi handled at the client (list) — see _coerce_multi


def _coerce_multi(step: Step, values: list[str]):
    if not values:
        return None
    if step.max_select and len(values) > step.max_select:
        values = values[: step.max_select]
    if step.options:
        lowered = {str(o).lower(): o for o in step.options}
        out = []
        for v in values:
            hit = lowered.get(str(v).strip().lower())
            out.append(hit if hit is not None else v.strip())
        return out or None
    return values


async def submit_multi(db: Session, user: User, step_id: str, values: list[str]) -> dict:
    step = STEP_BY_ID.get(step_id)
    if not step or step.kind != "multi":
        return await submit_answer(db, user, step_id, values[0] if values else "")
    value = _coerce_multi(step, values)
    if value is None:
        return {**_state(get_session(user, db)), "error": None}

    session = get_session(user, db)
    if not session:
        start_flow(db, user)
        session = get_session(user, db)

    answers = dict(session.answers or {})
    skipped = list(session.skipped or [])
    if step_id in answers:
        return {**_state(session), "error": None}

    answers[step_id] = value
    _apply_profile_side_effects(db, user, step_id, value)

    conversation = chat_service.get_or_create_conversation(
        user, session.conversation_id, step.question, db
    )
    if conversation.title == "New Chat" or conversation.id != session.conversation_id:
        conversation.title = "Get to know you · Novi onboarding"
    session.conversation_id = conversation.id
    _append_step_messages(db, conversation.id, step, value)

    session.step_index = _advance(session, answers, skipped)
    session.answers = answers
    if session.step_index >= TOTAL:
        session.status = "complete"
        done = True
    else:
        done = False
    db.commit()
    db.refresh(session)

    if done:
        return await _finalize(db, user, session, answers, skipped)
    return {**_state(session), "error": None}


def _append_step_messages(db: Session, conversation_id: int, step: Step, value) -> None:
    from app.models.chat import Message
    from app.models.enums import MessageRole

    db.add(Message(conversation_id=conversation_id, role=MessageRole.ASSISTANT, content=step.question))
    db.add(Message(conversation_id=conversation_id, role=MessageRole.USER, content=_format_answer(value)))
    db.commit()


def _apply_profile_side_effects(db: Session, user: User, step_id: str, value) -> None:
    changed = False
    if step_id == "name":
        parts = str(value).strip().split()
        if parts:
            if user.first_name != parts[0]:
                user.first_name = parts[0]
                changed = True
            if len(parts) > 1 and user.last_name != " ".join(parts[1:]):
                user.last_name = " ".join(parts[1:])
                changed = True
    elif step_id == "grade":
        m = _GRADE_RE.search(str(value))
        if m and user.grade != int(m.group(1)):
            user.grade = int(m.group(1))
            changed = True
    elif step_id == "curriculum":
        if str(value).lower() != "other" and user.school != str(value):
            user.school = str(value)
            changed = True
    if changed:
        db.commit()


async def _finalize(db: Session, user: User, session: OnboardingSession, answers: dict, skipped: list) -> dict:
    """Onboarding is complete: build Career DNA from the chat transcript, persist
    career matches, then produce the reveal summary (archetype + recommendations)."""
    from app.services.career_dna import refresh_dna_from_history
    from app.services.careers import match_careers
    from app.schemas.career import CareerMatchRequest

    transcript = _transcript(session, answers, skipped)
    history = []
    if session.conversation_id:
        try:
            history = chat_service.get_chat_history(user, session.conversation_id, db)
        except Exception as exc:
            print(f"[onboarding] transcript load failed: {exc}")
    if not history:
        history = transcript

    dna = None
    try:
        dna = await refresh_dna_from_history(
            user, history, db, conversation_id=session.conversation_id
        )
    except Exception as exc:
        print(f"[onboarding] dna finalize failed: {exc}")

    matches = []
    try:
        matches = await match_careers(db, user, CareerMatchRequest(limit=8))
    except Exception as exc:
        print(f"[onboarding] career match failed: {exc}")

    summary = await _build_summary(db, user, history, dna, matches)
    session.summary = summary
    session.status = "done"
    db.commit()
    db.refresh(session)

    return {**_state(session), "error": None}


async def _build_summary(
    db: Session, user: User, history: list[dict], dna, matches: list | None = None
) -> dict:
    current = dna_dict(dna)
    fallback = _fallback_summary(user, current)

    fallback_careers = []
    fallback_experiences = []
    for m in matches or []:
        title = (m.career.title if m.career else None) or ""
        if title:
            fallback_careers.append(title)
        reason = ""
        for r in m.reasons or []:
            if isinstance(r, str) and r.strip():
                reason = r.strip()
                break
            if isinstance(r, dict):
                reason = str(r.get("reason") or r.get("text") or "").strip()
                if reason:
                    break
        fallback_experiences.append(reason or f"Explore being a {title} — see what a day in the life looks like.")
    fallback_careers = fallback_careers[:8]
    fallback_experiences = list(dict.fromkeys(x for x in fallback_experiences if x))[:6]

    try:
        result = await gemini.complete_json(
            prompts.onboarding_completion_prompt(history, current, user_context(user)),
            system=prompts.ONBOARDING_COMPLETION_SYSTEM,
        )
        if not isinstance(result, dict):
            raise ValueError("bad shape")
    except Exception as exc:
        print(f"[onboarding] summary LLM failed: {exc}")
        return fallback

    def as_list(key):
        v = result.get(key)
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()][:10]

    recommendations = result.get("recommendations")
    if not isinstance(recommendations, dict):
        recommendations = {}

    def rec_list(key):
        v = recommendations.get(key)
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()][:10]

    plan = result.get("plan_30")
    if isinstance(plan, str):
        plan = [line.strip() for line in plan.splitlines() if line.strip()]
    if not isinstance(plan, list):
        plan = []
    plan = [str(x).strip() for x in plan if str(x).strip()][:12]

    return {
        "label": str(result.get("label") or "").strip() or fallback["label"],
        "identity": str(result.get("identity") or result.get("label") or "").strip() or fallback["label"],
        "traits": as_list("traits") or fallback["traits"],
        "explore": as_list("explore") or fallback["explore"],
        "focus": str(result.get("focus") or "").strip() or fallback["focus"],
        "reflection": str(result.get("reflection") or "").strip(),
        "recommendations": {
            "careers": rec_list("careers") or fallback_careers or fallback["recommendations"]["careers"],
            "experiences": rec_list("experiences") or fallback_experiences or fallback["recommendations"]["experiences"],
            "subjects": rec_list("subjects") or fallback["recommendations"]["subjects"],
            "university": str(recommendations.get("university") or "").strip(),
        },
        "plan_30": plan or fallback["plan_30"],
    }


def _fallback_summary(user: User, current: dict) -> dict:
    traits = (current.get("traits") or [])[:4]
    zones = (current.get("career_zones") or [])[:4]
    interests = (current.get("interests") or [])[:4]
    subjects = (current.get("subjects") or [])[:4]
    goals = (current.get("goals") or [])[:2]
    explore = zones or interests or ["exploring your options"]
    focus = "Discover → Experiment → Build"
    if goals:
        focus = f"Build toward: {goals[0]}"
    reflection = current.get("novi_reflection") or (
        f"Okay… I think I'm starting to get {user.display_name.split(' ')[0]}. "
        "We'll keep learning together — this is just the beginning."
    )
    return {
        "label": "EXPLORER",
        "identity": "You are an EXPLORER",
        "traits": traits or ["Curious"],
        "explore": explore,
        "focus": focus,
        "reflection": reflection,
        "recommendations": {
            "careers": [],
            "experiences": [],
            "subjects": subjects,
            "university": "",
        },
        "plan_30": [
            "Week 1 · Explore the careers Novi suggests on the Careers page.",
            "Week 1 · Pick one small project or activity you already enjoy.",
            "Week 2 · Add your first passport entry — anything you've built or done.",
            "Week 2 · Chat with Novi once — tell her what you're curious about.",
            "Week 3 · Set one goal on the Roadmap page.",
            "Week 3 · Try one new thing you've never done before.",
            "Week 4 · Check in and reflect on what you learned about yourself.",
        ],
    }


def reset_flow(db: Session, user: User) -> dict:
    session = get_session(user, db)
    if session:
        db.delete(session)
        db.commit()
    return start_flow(db, user)