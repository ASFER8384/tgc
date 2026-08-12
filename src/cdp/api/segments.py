from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from cdp.activation.service import ActivationService
from cdp.api.deps import ActorDep, SessionDep
from cdp.models import Segment
from cdp.segments.compiler import SegmentDefinitionError
from cdp.segments.service import SegmentService

router = APIRouter(tags=["segments"])


class SegmentIn(BaseModel):
    key: str
    name: str
    definition: dict = Field(
        examples=[
            {
                "all": [
                    {"brand_purchased": "aleena"},
                    {"brand_not_purchased": "rawash"},
                    {"trait": "aov", "op": "gte", "value": 400},
                ]
            }
        ]
    )
    required_consent: str | None = "marketing_whatsapp"
    description: str | None = None
    # Which brand is asking. Required in practice for anything that names a
    # consent purpose or reads brand behaviour — the compiler refuses without it
    # rather than quietly answering for the whole company.
    brand: str | None = None


class SegmentOut(BaseModel):
    key: str
    name: str
    definition: dict
    required_consent: str | None
    description: str | None
    brand: str | None


class EvaluationOut(BaseModel):
    key: str
    size: int
    person_ids: list[str]


class ActivationOut(BaseModel):
    run_id: str
    destination: str
    requested: int
    delivered: int
    failed: int
    skipped_no_consent: int


@router.get("/segments", response_model=list[SegmentOut])
async def list_segments(session: SessionDep, actor: ActorDep) -> list[SegmentOut]:
    rows = await session.scalars(select(Segment).order_by(Segment.key))
    return [
        SegmentOut(
            key=s.key,
            name=s.name,
            definition=s.definition,
            required_consent=s.required_consent,
            description=s.description,
            brand=s.brand,
        )
        for s in rows
    ]


@router.post("/segments", response_model=SegmentOut, status_code=status.HTTP_201_CREATED)
async def upsert_segment(body: SegmentIn, session: SessionDep, actor: ActorDep) -> SegmentOut:
    try:
        segment = await SegmentService(session, actor=actor).upsert(
            body.key,
            body.name,
            body.definition,
            required_consent=body.required_consent,
            description=body.description,
            brand=body.brand,
        )
    except SegmentDefinitionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return SegmentOut(
        key=segment.key,
        name=segment.name,
        definition=segment.definition,
        required_consent=segment.required_consent,
        description=segment.description,
        brand=segment.brand,
    )


@router.post("/segments/{key}/evaluate", response_model=EvaluationOut)
async def evaluate_segment(key: str, session: SessionDep, actor: ActorDep) -> EvaluationOut:
    try:
        ids = await SegmentService(session, actor=actor).evaluate(key)
    except SegmentDefinitionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return EvaluationOut(key=key, size=len(ids), person_ids=ids)


@router.post("/segments/{key}/activate", response_model=ActivationOut)
async def activate_segment(
    key: str, destination: str, session: SessionDep, actor: ActorDep
) -> ActivationOut:
    try:
        run = await ActivationService(session, actor=actor).run(key, destination)
    except SegmentDefinitionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return ActivationOut(
        run_id=run.id,
        destination=run.destination,
        requested=run.requested,
        delivered=run.delivered,
        failed=run.failed,
        skipped_no_consent=run.skipped_no_consent,
    )
