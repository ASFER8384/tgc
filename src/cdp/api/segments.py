from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from cdp.activation.service import ActivationService
from cdp.api.deps import ActorDep, SessionDep
from cdp.models import (
    ActivationDelivery,
    ActivationRun,
    AuditLog,
    Person,
    Segment,
    SegmentMember,
)
from cdp.segments.compiler import SegmentDefinitionError, compile_segment
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
    skipped_identifier_risk: int


class DeliveryOut(BaseModel):
    """One person, one destination, one outcome.

    The dashboard counts say how many were refused. This says which, and on what
    grounds — the row a regulator asks for, and the one that answers "why did
    this customer get that message?"."""

    person_id: str
    display_name: str | None
    destination: str
    consent_basis: str | None
    status: str
    detail: str | None
    occurred_at: datetime


@router.get("/activations", response_model=list[DeliveryOut])
async def list_deliveries(
    session: SessionDep, actor: ActorDep, limit: int = 50
) -> list[DeliveryOut]:
    rows = (
        await session.execute(
            select(ActivationDelivery, Person.display_name)
            .outerjoin(Person, Person.id == ActivationDelivery.person_id)
            .order_by(ActivationDelivery.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        DeliveryOut(
            person_id=row.person_id,
            display_name=name,
            destination=row.destination,
            consent_basis=row.consent_basis,
            status=row.status,
            detail=row.detail,
            occurred_at=row.created_at,
        )
        for row, name in rows
    ]


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


class PreviewIn(BaseModel):
    definition: dict
    required_consent: str | None = "marketing_whatsapp"
    brand: str | None = None


class PreviewOut(BaseModel):
    size: int


@router.post("/segments/preview", response_model=PreviewOut)
async def preview_segment(body: PreviewIn, session: SessionDep, actor: ActorDep) -> PreviewOut:
    """Count an unsaved definition.

    The same compiler the real evaluation uses, so the number on the builder is
    the number the campaign will get — a preview computed by a second, looser
    path is worse than no preview, because it is trusted and wrong.
    """
    try:
        query = compile_segment(body.definition, body.required_consent, brand=body.brand)
    except SegmentDefinitionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return PreviewOut(size=len(list(await session.scalars(query))))


@router.delete("/segments/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_segment(key: str, session: SessionDep, actor: ActorDep) -> None:
    segment = await session.scalar(select(Segment).where(Segment.key == key))
    if segment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown segment")
    # A segment that has been sent to keeps its history: the delivery log is the
    # answer to "why did this customer get that message?", and it must outlive
    # the audience that caused it.
    used = await session.scalar(
        select(ActivationRun.id).where(ActivationRun.segment_id == segment.id).limit(1)
    )
    if used is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this audience has been sent to; deleting it would take the delivery record with it",
        )
    await session.execute(delete(SegmentMember).where(SegmentMember.segment_id == segment.id))
    await session.delete(segment)
    session.add(
        AuditLog(
            actor=actor,
            action="segment.deleted",
            entity="segment",
            entity_id=key,
            meta={"name": segment.name},
        )
    )
    await session.flush()


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
        skipped_identifier_risk=run.skipped_identifier_risk,
    )
