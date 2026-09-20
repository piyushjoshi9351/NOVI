from pydantic import BaseModel


class OnboardingStep(BaseModel):
    key: str
    label: str
    detail: str
    route: str
    done: bool


class OnboardingOut(BaseModel):
    steps: list[OnboardingStep]
    total: int
    done: int
    percent: int
    next_action: dict | None = None


# ---------------------------------------------------------------------------
# Conversational flow
# ---------------------------------------------------------------------------


class FlowStep(BaseModel):
    id: str
    section: str
    question: str
    kind: str
    options: list[str] | None = None
    optional: bool = False
    max_select: int | None = None
    hint: str | None = None


class TranscriptMessage(BaseModel):
    role: str
    content: str
    skipped: bool = False


class FlowState(BaseModel):
    started: bool
    done: bool
    status: str
    conversation_id: int | None = None
    percent: int
    answered: int
    total: int
    current: FlowStep | None = None
    transcript: list[TranscriptMessage]
    summary: dict | None = None


class AnswerIn(BaseModel):
    step_id: str
    answer: str = ""
    values: list[str] | None = None


class SkipIn(BaseModel):
    step_id: str


class FlowOut(BaseModel):
    started: bool = True
    done: bool
    status: str
    percent: int
    answered: int
    total: int
    current: FlowStep | None = None
    transcript: list[TranscriptMessage]
    summary: dict | None = None
    error: str | None = None