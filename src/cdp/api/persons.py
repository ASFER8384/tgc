from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from cdp.api.deps import ActorDep, SessionDep
from cdp.consent.service import ConsentService
from cdp.identity.service import IdentityService
from cdp.models import AuditLog, Event, Identifier, Person, PersonBrandStat, ProfileTraits

router = APIRouter(prefix="/persons", tags=["persons"])


class TimelineEntry(BaseModel):
    occurred_at: datetime
    source: str
    name: str
    channel: str | None
    value_amount: Decimal | None
    currency: str | None
    # What the source itself sent. Answering "where did this come from, and what
    # did they actually give us?" is the provenance question a console has to be
    # able to settle without anyone opening the database.
    payload: dict


class BrandStat(BaseModel):
    brand: str
    orders: int
    spend: Decimal


class PersonProfile(BaseModel):
    """What the console shows: one human, every channel, one timeline. This is the
    'oh' moment of the whole product — five systems collapsing into one customer."""

    person_id: str
    display_name: str | None
    preferred_language: str | None
    preferred_channel: str | None
    identifiers: dict[str, list[str]]
    traits: dict[str, object]
    brands: list[BrandStat]
    consent: dict[str, bool]
    timeline: list[TimelineEntry]


class PersonSummary(BaseModel):
    """One row of the console's customer list. Deliberately flat and small: the
    list renders hundreds of these, and the full profile is one click away."""

    person_id: str
    display_name: str | None
    phone: str | None
    email: str | None
    preferred_language: str | None
    order_count: int
    ltv: Decimal
    brands: list[str]
    consent_marketing_whatsapp: bool
    last_order_at: datetime | None


@router.get("", response_model=list[PersonSummary])
async def list_persons(
    session: SessionDep, actor: ActorDep, limit: int = 100
) -> list[PersonSummary]:
    # Merge losers keep their row and point at the winner; the list shows humans,
    # so those are excluded rather than shown twice.
    people = list(
        await session.scalars(
            select(Person).where(Person.merged_into_id.is_(None)).limit(limit)
        )
    )
    ids = [p.id for p in people]
    if not ids:
        return []

    contact: dict[str, dict[str, str]] = {}
    for row in await session.scalars(
        select(Identifier).where(
            Identifier.person_id.in_(ids), Identifier.kind.in_(("phone", "email"))
        )
    ):
        contact.setdefault(row.person_id, {}).setdefault(row.kind, row.value)

    trait_rows = await session.scalars(
        select(ProfileTraits).where(ProfileTraits.person_id.in_(ids))
    )
    traits = {t.person_id: t for t in trait_rows}
    brands: dict[str, list[str]] = {}
    for stat in await session.scalars(
        select(PersonBrandStat)
        .where(PersonBrandStat.person_id.in_(ids))
        .order_by(PersonBrandStat.spend.desc())
    ):
        brands.setdefault(stat.person_id, []).append(stat.brand)

    consent = ConsentService(session, actor=actor)
    summaries = []
    for person in people:
        trait = traits.get(person.id)
        current = await consent.current(person.id)
        summaries.append(
            PersonSummary(
                person_id=person.id,
                display_name=person.display_name,
                phone=contact.get(person.id, {}).get("phone"),
                email=contact.get(person.id, {}).get("email"),
                preferred_language=person.preferred_language,
                order_count=trait.order_count if trait else 0,
                ltv=trait.ltv if trait else Decimal("0"),
                brands=brands.get(person.id, []),
                consent_marketing_whatsapp=current.get("marketing_whatsapp", False),
                last_order_at=trait.last_order_at if trait else None,
            )
        )
    summaries.sort(key=lambda s: (s.ltv, s.order_count), reverse=True)
    return summaries


@router.get("/{person_id}", response_model=PersonProfile)
async def get_person(person_id: str, session: SessionDep, actor: ActorDep) -> PersonProfile:
    identity = IdentityService(session, actor=actor)
    # A previously issued id still resolves after a merge instead of 404ing —
    # downstream systems hold ids we no longer consider canonical.
    canonical_id = await identity.canonical_id(person_id)

    person = await session.get(Person, canonical_id)
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown person")

    identifiers: dict[str, list[str]] = {}
    rows = await session.scalars(
        select(Identifier).where(Identifier.person_id == canonical_id)
    )
    for row in rows:
        identifiers.setdefault(row.kind, []).append(row.value)

    traits = await session.get(ProfileTraits, canonical_id)
    brands = await session.scalars(
        select(PersonBrandStat)
        .where(PersonBrandStat.person_id == canonical_id)
        .order_by(PersonBrandStat.spend.desc())
    )
    events = await session.scalars(
        select(Event)
        .where(Event.person_id == canonical_id)
        .order_by(Event.occurred_at.desc())
        .limit(200)
    )

    # PDPL: reads of a customer's contact details are logged, not just writes.
    session.add(
        AuditLog(
            actor=actor,
            action="person.read",
            entity="person",
            entity_id=canonical_id,
            meta={"requested_id": person_id},
        )
    )

    return PersonProfile(
        person_id=canonical_id,
        display_name=person.display_name,
        preferred_language=person.preferred_language,
        preferred_channel=person.preferred_channel,
        identifiers=identifiers,
        traits=(
            {
                "order_count": traits.order_count,
                "ltv": traits.ltv,
                "aov": traits.aov,
                "recency_days": traits.recency_days,
                "rfm": traits.rfm,
                "brands_purchased": traits.brands_purchased,
                "event_count": traits.event_count,
                "first_order_at": traits.first_order_at,
                "last_order_at": traits.last_order_at,
            }
            if traits
            else {}
        ),
        brands=[BrandStat(brand=b.brand, orders=b.orders, spend=b.spend) for b in brands],
        consent=await ConsentService(session, actor=actor).current(canonical_id),
        timeline=[
            TimelineEntry(
                occurred_at=e.occurred_at,
                source=e.source,
                name=e.name,
                channel=e.channel,
                value_amount=e.value_amount,
                currency=e.currency,
                payload=e.payload or {},
            )
            for e in events
        ],
    )


class ConsentIn(BaseModel):
    purpose: str
    granted: bool
    source: str = "console"
    evidence: str | None = None


@router.post("/{person_id}/consent", response_model=dict[str, bool])
async def set_consent(
    person_id: str, body: ConsentIn, session: SessionDep, actor: ActorDep
) -> dict[str, bool]:
    service = ConsentService(session, actor=actor)
    canonical_id = await IdentityService(session, actor=actor).canonical_id(person_id)
    try:
        await service.record(
            canonical_id, body.purpose, body.granted, source=body.source, evidence=body.evidence
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return await service.current(canonical_id)
