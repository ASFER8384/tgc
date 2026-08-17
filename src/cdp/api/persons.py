from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from cdp.api.deps import ActorDep, SessionDep
from cdp.consent.service import ConsentService
from cdp.identity.service import IdentityService
from cdp.models import AuditLog, Event, Identifier, Person, PersonBrandStat, ProfileTraits
from cdp.privacy.service import PrivacyService, UnknownPerson

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
    # Brand -> purpose -> granted. Nested rather than flat because a flat map
    # would have to answer "does she consent to WhatsApp" with one value, and
    # that question has three different answers.
    consent: dict[str, dict[str, bool]]
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
    consent_whatsapp_brands: list[str]
    last_order_at: datetime | None


class PersonPage(BaseModel):
    """A page of customers, and how many there are to page through.

    The total is on the response rather than fetched separately because the two
    have to agree: a count taken in its own request can be answered after a sale
    has landed, and the last page then holds people the count says are not
    there.
    """

    total: int
    offset: int
    limit: int
    people: list[PersonSummary]


@router.get("", response_model=PersonPage)
async def list_persons(
    session: SessionDep, actor: ActorDep,
    limit: int = 100, offset: int = 0, q: str | None = None,
) -> PersonPage:
    # Merge losers keep their row and point at the winner; the list shows humans,
    # so those are excluded rather than shown twice. Synthetic records go for the
    # same reason and are not the same thing: a shop counter is not a person who
    # was merged away, it is a place to put the sales nobody left a name on, and
    # it would otherwise sit at the top of this list as the best customer in the
    # business.
    query = select(Person).where(
        Person.merged_into_id.is_(None), Person.synthetic.is_(False)
    )

    if q:
        # Searched by whatever the person on the phone actually has to hand: a
        # name, or the number she is calling from. Matching identifiers rather
        # than only the display name is the point of resolution — a customer who
        # gave her email once and her phone another time is found by either.
        needle = f"%{q.strip().lower()}%"
        query = query.where(
            Person.id.in_(
                select(Identifier.person_id).where(func.lower(Identifier.value).like(needle))
            )
            | func.lower(func.coalesce(Person.display_name, "")).like(needle)
        )

    # Ranked in the database, not after the fetch: sorting a page that was itself
    # chosen arbitrarily would put the biggest customers off the end of the list
    # as soon as there are more of them than the limit.
    # Counted before the page is cut, and off the same filters: a total taken
    # from a different query is a total that disagrees with the list under it the
    # first time somebody searches.
    total = await session.scalar(
        select(func.count()).select_from(query.order_by(None).subquery())
    ) or 0

    query = (
        query.outerjoin(ProfileTraits, ProfileTraits.person_id == Person.id)
        # Ordered by a column that can tie, so a second, unique key goes with it.
        # Without it the database may return the same person on two pages and
        # another on none — a page boundary is exactly where that shows up.
        .order_by(func.coalesce(ProfileTraits.ltv, 0).desc(), Person.id)
        .offset(max(0, offset))
        .limit(limit)
    )
    people = list(await session.scalars(query))
    ids = [p.id for p in people]
    if not ids:
        return PersonPage(total=total, offset=offset, limit=limit, people=[])

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
                # The list column answers "can anyone reach her", which is the
                # useful question when scanning hundreds of rows. Which brand
                # may is on the profile, where a decision actually gets made.
                consent_whatsapp_brands=sorted(
                    b for b, purposes in current.items() if purposes["marketing_whatsapp"]
                ),
                last_order_at=trait.last_order_at if trait else None,
            )
        )
    # Already ordered by the query. Re-sorting here would only be a second, and
    # eventually disagreeing, opinion about what "top customers" means.
    return PersonPage(total=total, offset=offset, limit=limit, people=summaries)


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
    # No default. Recording which brand she agreed with is the whole point, and
    # a default would silently attribute every console entry to one of them.
    brand: str
    source: str = "console"
    evidence: str | None = None


@router.post("/{person_id}/consent", response_model=dict)
async def set_consent(
    person_id: str, body: ConsentIn, session: SessionDep, actor: ActorDep
) -> dict:
    """Record a grant or a withdrawal, and return the state brand by brand.

    The response is a table rather than a flat map because there is no single
    answer to "may we contact her" — only three, one per brand.
    """
    service = ConsentService(session, actor=actor)
    canonical_id = await IdentityService(session, actor=actor).canonical_id(person_id)
    try:
        await service.record(
            canonical_id,
            body.purpose,
            body.granted,
            brand=body.brand,
            source=body.source,
            evidence=body.evidence,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return await service.current(canonical_id)


@router.get("/{person_id}/export")
async def export_person(person_id: str, session: SessionDep, actor: ActorDep) -> dict:
    """Everything held about one person — the subject access request.

    Answered against the graph rather than against each source system, which is
    the main operational reason to hold identity centrally at all: the
    alternative is a manual sweep across five systems that neither scales nor
    survives being checked.
    """
    try:
        return await PrivacyService(session, actor=actor).export(person_id)
    except UnknownPerson as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown person") from exc


@router.delete("/{person_id}")
async def erase_person(
    person_id: str, session: SessionDep, actor: ActorDep, reason: str = "subject request"
) -> dict:
    """Erase a person and everything held about her.

    Deletes the whole identity cluster, not only the id supplied: a person who
    was merged away keeps a row, and removing just the one you were handed would
    leave her alive under an alias. The raw webhook bodies go too — they carry
    her name and address, and an erasure that left them would be one in name
    only.
    """
    try:
        report = await PrivacyService(session, actor=actor).erase(person_id, reason=reason)
    except UnknownPerson as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown person") from exc
    return report.as_dict()
