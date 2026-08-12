from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.activation.port import (
    DeliveryContext,
    Destination,
    NotConfigured,
    default_registry,
    validate_destination,
)
from cdp.consent.service import ConsentService
from cdp.models import ActivationDelivery, ActivationRun, AuditLog, Identifier, ProfileTraits
from cdp.segments.compiler import SegmentDefinitionError, compile_segment
from cdp.segments.service import SegmentService

# The kinds a message could actually be sent to. A shopify customer id is an
# identifier but not an address, so its provenance cannot cause a disclosure.
CONTACTABLE_KINDS = ("phone", "email", "whatsapp_id")


class ActivationService:
    """The return trip: segment out to a channel.

    A CDP that only stores data is a report. This is the half that changes what a
    customer actually experiences, and therefore the half that needs the strictest
    consent handling — two independent gates apply. The segment's own purpose is
    compiled into the member query, and the destination's declared purpose is
    checked per person before its API is ever called. A destination needing a
    purpose the segment did not require cannot leak, because it re-checks.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        registry: dict[str, Destination] | None = None,
        actor: str = "system",
    ):
        self.session = session
        self.actor = actor
        self.registry = registry if registry is not None else default_registry()
        self.segments = SegmentService(session, actor=actor)
        self.consent = ConsentService(session, actor=actor)

    async def run(self, segment_key: str, destination_name: str) -> ActivationRun:
        destination = self.registry.get(destination_name)
        if destination is None:
            raise SegmentDefinitionError(f"unknown destination: {destination_name}")
        validate_destination(destination)

        segment = await self.segments.get(segment_key)
        if segment is None:
            raise SegmentDefinitionError(f"unknown segment: {segment_key}")

        member_ids = list(
            await self.session.scalars(
                compile_segment(
                    segment.definition, segment.required_consent, brand=segment.brand
                )
            )
        )

        run = ActivationRun(
            segment_id=segment.id,
            destination=destination.name,
            requested=len(member_ids),
        )
        self.session.add(run)
        await self.session.flush()

        for person_id in member_ids:
            basis = destination.required_consent
            if basis is not None:
                # The destination's own requirement, checked against the brand
                # sending the campaign. A segment with no brand cannot clear a
                # brand-scoped grant, and is skipped rather than waved through.
                state = await self.consent.current(person_id, segment.brand)
                if not state.get(basis, False):
                    run.skipped_no_consent += 1
                    self.session.add(
                        ActivationDelivery(
                            run_id=run.id,
                            person_id=person_id,
                            destination=destination.name,
                            consent_basis=basis,
                            status="skipped",
                            detail=f"no consent for {basis}",
                        )
                    )
                    continue

            # Consent is necessary and not sufficient. She may have agreed, and
            # the number we hold may still not be hers — a mall form filled in
            # by a friend, a gift order carrying the recipient's phone. Sending
            # her purchase history to that number is a disclosure, so an
            # addressed destination refuses it regardless of the grant.
            if destination.addressed:
                risky = await self._third_party_identifier(person_id)
                if risky is not None:
                    run.skipped_identifier_risk += 1
                    self.session.add(
                        ActivationDelivery(
                            run_id=run.id,
                            person_id=person_id,
                            destination=destination.name,
                            consent_basis=basis,
                            status="skipped",
                            detail=f"{risky.kind} was captured at {risky.capture_context}",
                        )
                    )
                    continue

            ctx = DeliveryContext(
                person_id=person_id,
                segment_key=segment.key,
                identifiers=await self._identifiers(person_id),
                traits=await self._traits(person_id),
                consent_basis=basis,
            )
            try:
                result = await destination.deliver(ctx)
            except NotConfigured as exc:
                result_status, detail = "failed", str(exc)
            else:
                result_status, detail = result.status, result.detail

            if result_status == "delivered":
                run.delivered += 1
            elif result_status == "failed":
                run.failed += 1

            self.session.add(
                ActivationDelivery(
                    run_id=run.id,
                    person_id=person_id,
                    destination=destination.name,
                    consent_basis=basis,
                    status=result_status,
                    detail=detail,
                )
            )

        self.session.add(
            AuditLog(
                actor=self.actor,
                action="activation.run",
                entity="segment",
                entity_id=segment.key,
                meta={
                    "destination": destination.name,
                    "requested": run.requested,
                    "delivered": run.delivered,
                    "failed": run.failed,
                    "skipped_no_consent": run.skipped_no_consent,
                    "skipped_identifier_risk": run.skipped_identifier_risk,
                },
            )
        )
        await self.session.flush()
        return run

    async def _third_party_identifier(self, person_id: str) -> Identifier | None:
        """The contact identifier a message would go to, if it may not be hers.

        Only the identifiers a send would actually use are considered, and only
        the most recent of each kind — the same one ``_identifiers`` picks. A
        number captured at a stand two years ago that she has since typed into a
        checkout herself is not a reason to withhold today's message.
        """
        rows = (
            await self.session.scalars(
                select(Identifier).where(
                    Identifier.person_id == person_id,
                    Identifier.kind.in_(CONTACTABLE_KINDS),
                )
            )
        ).all()
        latest: dict[str, Identifier] = {}
        for row in sorted(rows, key=lambda r: r.last_seen_at):
            latest[row.kind] = row
        for row in latest.values():
            if row.third_party_plausible:
                return row
        return None

    async def _identifiers(self, person_id: str) -> dict[str, str]:
        rows = (
            await self.session.scalars(
                select(Identifier).where(Identifier.person_id == person_id)
            )
        ).all()
        # Latest of each kind: a customer who changed her number should be reached
        # on the new one.
        out: dict[str, str] = {}
        for row in sorted(rows, key=lambda r: r.last_seen_at):
            out[row.kind] = row.value
        return out

    async def _traits(self, person_id: str) -> dict[str, object]:
        traits = await self.session.get(ProfileTraits, person_id)
        if traits is None:
            return {}
        return {
            "order_count": traits.order_count,
            "ltv": str(traits.ltv),
            "aov": str(traits.aov),
            "recency_days": traits.recency_days,
            "rfm": traits.rfm,
        }
