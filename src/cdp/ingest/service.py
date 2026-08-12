from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.identity.service import IdentityService
from cdp.ingest.schemas import CanonicalEvent, IngestResult
from cdp.models import AuditLog, Event, Person, RawEvent
from cdp.profiles.service import ProfileService


def _capture_context(event: CanonicalEvent) -> str:
    """Which circumstance produced these identifiers.

    Derived from the source and the payload at the moment of capture, because
    working it out later is guesswork — and this field exists precisely to be
    trusted when deciding whether to send someone a message.
    """
    if event.source == "activation":
        return "activation"
    # Shopify marks a gift order with its own note fields; the number on one is
    # commonly the recipient's rather than the buyer's.
    payload = event.payload or {}
    if payload.get("gift") or (payload.get("note_attributes") and _is_gift(payload)):
        return "gift"
    if event.source in {"whatsapp", "messaging"}:
        return "messaging"
    if event.source.startswith("shopify"):
        return "checkout"
    if event.source == "console":
        return "console"
    return "unknown"


def _is_gift(payload: dict) -> bool:
    for attribute in payload.get("note_attributes") or []:
        name = str(attribute.get("name", "")).lower()
        if "gift" in name and str(attribute.get("value", "")).lower() not in {"", "no", "false"}:
            return True
    return False


class IngestService:
    """Land raw, normalise, resolve, project.

    Runs inside the request transaction for the base build: the raw event, the
    resolved person and the recomputed traits commit together or not at all.
    Moving the normalise/resolve half onto a queue is the next step (see README
    roadmap) — the raw event store is what makes that a change of *when*, not a
    change of *what*.
    """

    def __init__(
        self, session: AsyncSession, *, actor: str = "connector", country_code: str = "966"
    ):
        self.session = session
        self.actor = actor
        self.identity = IdentityService(session, actor=actor, country_code=country_code)
        self.profiles = ProfileService(session)

    async def ingest(self, event: CanonicalEvent, *, topic: str | None = None) -> IngestResult:
        received_at = datetime.now(UTC)

        # Idempotency first. Every one of these platforms retries, and Shopify in
        # particular will replay a topic if our 200 is slow.
        existing = await self.session.scalar(
            select(RawEvent).where(RawEvent.dedupe_key == event.dedupe_key)
        )
        if existing is not None:
            # A redelivery is not an unknown. The person this event resolved to
            # the first time is recorded, so return it rather than nothing —
            # otherwise a caller retrying a webhook loses the ability to act on
            # the customer, and cannot tell "already seen" from "unresolvable".
            # Read from the stored row rather than re-resolving: resolution
            # creates and merges, and a duplicate must not do either.
            person_id = await self.session.scalar(
                select(Event.person_id).where(Event.raw_event_id == existing.id).limit(1)
            )
            return IngestResult(accepted=True, duplicate=True, person_id=person_id)

        raw = RawEvent(
            source=event.source,
            topic=topic or event.name,
            dedupe_key=event.dedupe_key,
            payload=event.payload,
            received_at=received_at,
        )
        self.session.add(raw)
        await self.session.flush()

        identifiers = self.identity.prepare(
            event.identifiers, capture_context=_capture_context(event)
        )
        resolution = await self.identity.resolve(identifiers, seen_at=event.occurred_at)

        row = Event(
            raw_event_id=raw.id,
            person_id=resolution.person_id,
            source=event.source,
            name=event.name,
            occurred_at=event.occurred_at,
            value_amount=event.value_amount,
            currency=event.currency,
            channel=event.channel or event.source,
            payload={
                **event.payload,
                "brands": {k: str(v) for k, v in event.brands.items()},
            },
        )
        self.session.add(row)

        person = await self.session.get(Person, resolution.person_id)
        if person is not None:
            # First writer wins for the name, latest wins for language: a display
            # name changing usually means a bad merge worth noticing, whereas a
            # customer switching the storefront to Arabic is a real preference.
            if event.display_name and not person.display_name:
                person.display_name = event.display_name
            if event.language:
                person.preferred_language = event.language

        raw.processed_at = datetime.now(UTC)
        await self.session.flush()

        await self.profiles.recompute(resolution.person_id)

        self.session.add(
            AuditLog(
                actor=self.actor,
                action="ingest.event",
                entity="event",
                entity_id=row.id,
                meta={
                    "source": event.source,
                    "name": event.name,
                    "person_id": resolution.person_id,
                },
            )
        )
        await self.session.flush()

        return IngestResult(
            accepted=True,
            duplicate=False,
            person_id=resolution.person_id,
            event_id=row.id,
            merged_person_ids=resolution.merged_person_ids,
            review_ids=resolution.review_ids,
        )
