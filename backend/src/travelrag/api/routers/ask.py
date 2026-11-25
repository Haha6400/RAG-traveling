import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from travelrag.api.deps import CurrentUserDep, SessionDep
from travelrag.ml.ports import MLUnavailableError
from travelrag.qa.factory import build_qa_service

router = APIRouter(tags=["qa"])


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    place_id: uuid.UUID | None = Field(default=None, description="'Ask about this place'")
    trip_id: uuid.UUID | None = Field(default=None, description="Trip for plan questions")


class SourceOut(BaseModel):
    label: str
    kind: str
    place_id: uuid.UUID | None
    place_name: str | None
    video_id: uuid.UUID | None
    video_name: str | None
    start_ms: int | None
    end_ms: int | None
    snippet: str
    score: float | None


class AnswerOut(BaseModel):
    answer: str
    intent: str
    language: str
    no_data: bool
    llm_used: bool
    sources: list[SourceOut]
    places: list[dict[str, Any]]


@router.post("/ask")
async def ask(body: AskIn, user: CurrentUserDep, session: SessionDep) -> AnswerOut:
    try:
        result = await build_qa_service(session).ask(
            user.id, body.question, place_id=body.place_id, trip_id=body.trip_id
        )
    except MLUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return AnswerOut(
        answer=result.answer,
        intent=result.intent.value,
        language=result.language,
        no_data=result.no_data,
        llm_used=result.llm_used,
        sources=[SourceOut(**s.__dict__) for s in result.sources],
        places=result.places,
    )
