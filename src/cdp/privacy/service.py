"""Subject access and erasure, executed against the graph.

This is the operational argument for holding identity centrally at all. Answering
"send me everything you have on me" or "delete me" against five source systems is
a manual process that does not scale and will not withstand scrutiny; against one
graph it is a query.

Two properties matter more than completeness of the code:

**Erasure follows merges.** A person who has been merged away keeps her row and
points at the winner. Deleting only the person whose id you were handed leaves
her data alive under an alias — technically deleted, actually not. Everything in
the cluster goes.

**Erasure reaches the raw payloads.** Every webhook body is stored verbatim, and
those bodies contain her name, address and telephone number. An erasure that
tidied the normalised tables and left the raw events behind would be a deletion
in name only, and the raw store is precisely where a regulator would look.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.models import (
    ActivationDelivery,
    AuditLog,
    ConsentEvent,
    Event,
    Identifier,
    IdentityMerge,
    MergeReview,
    Person,
    PersonBrandStat,
    ProfileTraits,
    RawEvent,
    SegmentMember,
)


class UnknownPerson(LookupError):
    pass


@dataclass
class ErasureReport:
    """What was removed, counted per table.

    Returned to the caller and written to the audit log, because "we deleted
    her" is not a claim anyone should have to take on trust — and the counts are
    the only evidence left once the rows are gone.
    """

    person_ids: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"persons": self.person_ids, "deleted": self.counts}


class PrivacyService:
    def __init__(self, session: AsyncSession, *, actor: str = "system"):
        self.session = session
        self.actor = actor

    async def cluster(self, person_id: str) -> list[str]:
        """Every person row that is this human: the survivor and every alias.

        Walks outward rather than following ``merged_into_id`` once, because a
        merge chain can be several hops long and an alias of an alias is still
        her.
        """
        if await self.session.get(Person, person_id) is None:
            raise UnknownPerson(person_id)

        found = {person_id}
        frontier = {person_id}
        # Bounded: a cycle from a bad manual merge must not spin here, and no
        # legitimate cluster is anywhere near this deep.
        for _ in range(32):
            if not frontier:
                break
            rows = (
                await self.session.scalars(
                    select(Person).where(
                        or_(
                            Person.id.in_(frontier),
                            Person.merged_into_id.in_(frontier),
                        )
                    )
                )
            ).all()
            nxt = set()
            for row in rows:
                for candidate in (row.id, row.merged_into_id):
                    if candidate and candidate not in found:
                        found.add(candidate)
                        nxt.add(candidate)
            frontier = nxt
        return sorted(found)

    async def export(self, person_id: str) -> dict:
        """Everything held about one person, in the shape it is stored.

        Deliberately not prettied up for reading: a subject access request is
        answered with what the system actually holds, and summarising it is how
        the inconvenient parts go missing.
        """
        ids = await self.cluster(person_id)

        people = (await self.session.scalars(select(Person).where(Person.id.in_(ids)))).all()
        identifiers = (
            await self.session.scalars(select(Identifier).where(Identifier.person_id.in_(ids)))
        ).all()
        consent = (
            await self.session.scalars(
                select(ConsentEvent)
                .where(ConsentEvent.person_id.in_(ids))
                .order_by(ConsentEvent.occurred_at)
            )
        ).all()
        events = (
            await self.session.scalars(
                select(Event).where(Event.person_id.in_(ids)).order_by(Event.occurred_at)
            )
        ).all()
        traits = (
            await self.session.scalars(
                select(ProfileTraits).where(ProfileTraits.person_id.in_(ids))
            )
        ).all()
        brands = (
            await self.session.scalars(
                select(PersonBrandStat).where(PersonBrandStat.person_id.in_(ids))
            )
        ).all()
        deliveries = (
            await self.session.scalars(
                select(ActivationDelivery).where(ActivationDelivery.person_id.in_(ids))
            )
        ).all()
        merges = (
            await self.session.scalars(
                select(IdentityMerge).where(
                    or_(
                        IdentityMerge.winner_person_id.in_(ids),
                        IdentityMerge.loser_person_id.in_(ids),
                    )
                )
            )
        ).all()

        return {
            "person_ids": ids,
            "generated_at": datetime.now(UTC).isoformat(),
            "profile": [
                {
                    "id": p.id,
                    "display_name": p.display_name,
                    "preferred_language": p.preferred_language,
                    "preferred_channel": p.preferred_channel,
                    "merged_into_id": p.merged_into_id,
                }
                for p in people
            ],
            "identifiers": [
                {
                    "kind": i.kind,
                    "value": i.value,
                    "first_seen_at": i.first_seen_at.isoformat(),
                    "last_seen_at": i.last_seen_at.isoformat(),
                }
                for i in identifiers
            ],
            # The consent history, not just the current state: "prove she agreed
            # on 3 March" is the question, and the answer is the ledger.
            "consent": [
                {
                    "brand": c.brand,
                    "purpose": c.purpose,
                    "granted": c.granted,
                    "source": c.source,
                    "evidence": c.evidence,
                    "occurred_at": c.occurred_at.isoformat(),
                }
                for c in consent
            ],
            "events": [
                {
                    "name": e.name,
                    "source": e.source,
                    "channel": e.channel,
                    "occurred_at": e.occurred_at.isoformat(),
                    "value_amount": str(e.value_amount) if e.value_amount is not None else None,
                    "currency": e.currency,
                    "payload": e.payload,
                }
                for e in events
            ],
            "traits": [
                {
                    "order_count": t.order_count,
                    "ltv": str(t.ltv),
                    "aov": str(t.aov),
                    "recency_days": t.recency_days,
                    "rfm": t.rfm,
                    "computed_at": t.computed_at.isoformat(),
                }
                for t in traits
            ],
            "brands": [
                {"brand": b.brand, "orders": b.orders, "spend": str(b.spend)} for b in brands
            ],
            # What we sent her, and on what basis. The other half of the same
            # question: not only what we know, but what we did with it.
            "messages": [
                {
                    "destination": d.destination,
                    "status": d.status,
                    "consent_basis": d.consent_basis,
                    "sent_at": d.created_at.isoformat(),
                }
                for d in deliveries
            ],
            "merges": [
                {
                    "winner": m.winner_person_id,
                    "loser": m.loser_person_id,
                    "linked_by": m.linked_by_kind,
                    "reason": m.reason,
                }
                for m in merges
            ],
        }

    async def erase(self, person_id: str, *, reason: str = "subject request") -> ErasureReport:
        """Delete a person and everything held about her.

        The audit row that records the erasure survives it, holding counts and
        the person ids and nothing else. Keeping proof that a deletion happened
        is not in tension with the deletion: the evidence a regulator wants is
        that her data is gone, and a record with no personal data in it is how
        that is shown.
        """
        ids = await self.cluster(person_id)
        report = ErasureReport(person_ids=ids)

        # Raw payloads are reached through the events that were made from them.
        # Collected before the events are deleted, or the link is lost and the
        # bodies — which carry her name and address — survive the erasure.
        raw_ids = [
            row
            for row in await self.session.scalars(
                select(Event.raw_event_id).where(
                    Event.person_id.in_(ids), Event.raw_event_id.is_not(None)
                )
            )
        ]

        for label, statement in (
            ("segment_members", delete(SegmentMember).where(SegmentMember.person_id.in_(ids))),
            (
                "messages",
                delete(ActivationDelivery).where(ActivationDelivery.person_id.in_(ids)),
            ),
            ("consent_events", delete(ConsentEvent).where(ConsentEvent.person_id.in_(ids))),
            ("traits", delete(ProfileTraits).where(ProfileTraits.person_id.in_(ids))),
            ("brand_stats", delete(PersonBrandStat).where(PersonBrandStat.person_id.in_(ids))),
            ("identifiers", delete(Identifier).where(Identifier.person_id.in_(ids))),
            ("events", delete(Event).where(Event.person_id.in_(ids))),
            (
                "merge_reviews",
                delete(MergeReview).where(
                    or_(MergeReview.person_a_id.in_(ids), MergeReview.person_b_id.in_(ids))
                ),
            ),
            (
                "identity_merges",
                delete(IdentityMerge).where(
                    or_(
                        IdentityMerge.winner_person_id.in_(ids),
                        IdentityMerge.loser_person_id.in_(ids),
                    )
                ),
            ),
        ):
            result = await self.session.execute(statement)
            report.counts[label] = result.rowcount or 0

        if raw_ids:
            result = await self.session.execute(
                delete(RawEvent).where(RawEvent.id.in_(set(raw_ids)))
            )
            report.counts["raw_events"] = result.rowcount or 0

        # The self-reference has to go before the rows do, or deleting the
        # survivor trips the foreign key its own aliases still point at.
        await self.session.execute(
            update(Person).where(Person.id.in_(ids)).values(merged_into_id=None)
        )
        result = await self.session.execute(delete(Person).where(Person.id.in_(ids)))
        report.counts["persons"] = result.rowcount or 0

        self.session.add(
            AuditLog(
                actor=self.actor,
                action="person.erased",
                entity="person",
                entity_id=person_id,
                meta={"reason": reason, "person_ids": ids, "deleted": report.counts},
            )
        )
        await self.session.flush()
        return report
