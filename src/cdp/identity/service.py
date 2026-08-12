from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.identity.normalize import normalize
from cdp.models import (
    STRONG_KINDS,
    THIRD_PARTY_CONTEXTS,
    AuditLog,
    ConsentEvent,
    Event,
    Identifier,
    IdentityMerge,
    MergeReview,
    Person,
    PersonBrandStat,
    ProfileTraits,
    SegmentMember,
)


@dataclass(frozen=True)
class IdentifierIn:
    kind: str
    value: str
    capture_context: str = "unknown"

    @property
    def is_strong(self) -> bool:
        return self.kind in STRONG_KINDS


@dataclass
class Resolution:
    person_id: str
    created: bool = False
    merged_person_ids: list[str] = field(default_factory=list)
    review_ids: list[str] = field(default_factory=list)


# Tables carrying a person_id that must follow the winner on a merge. Traits and
# brand stats are absent on purpose: they are derived from events and get
# recomputed after the repoint, which is strictly safer than adding pre-aggregated
# totals together.
_REPOINTED = (Event, Identifier, ConsentEvent, SegmentMember)


class IdentityService:
    """Deterministic identity resolution.

    The identifier graph is stored, not held in memory: each identifier row is a
    node owned by exactly one person (enforced by a unique constraint), so a
    person *is* a connected component and resolution is a lookup plus, at most,
    a merge. No probabilistic matching — a false merge shows one customer another
    customer's order history, which is a breach, not a bug. When the evidence is
    weak, the case is queued for a human instead.
    """

    def __init__(self, session: AsyncSession, *, actor: str = "system", country_code: str = "966"):
        self.session = session
        self.actor = actor
        self.country_code = country_code

    def prepare(
        self,
        raw: dict[str, str | None] | list[tuple[str, str | None]],
        *,
        capture_context: str = "unknown",
    ) -> list[IdentifierIn]:
        """Normalise and de-duplicate incoming identifiers, dropping unusable ones.

        The capture context travels with the value. Recording it at capture is
        the only honest moment: inferring later that a number "looks like a mall
        signup" is guesswork, and this is a field whose whole purpose is to be
        trustworthy when a send is being decided.
        """
        pairs = list(raw.items()) if isinstance(raw, dict) else list(raw)
        out: dict[tuple[str, str], IdentifierIn] = {}
        for kind, value in pairs:
            folded = normalize(kind, value, self.country_code)
            if folded:
                out[(kind, folded)] = IdentifierIn(kind, folded, capture_context)
        return list(out.values())

    async def canonical_id(self, person_id: str) -> str:
        """Follow the merge chain to the surviving person. Bounded so a cycle
        introduced by a bad manual merge cannot hang a request."""
        seen: set[str] = set()
        current = person_id
        for _ in range(16):
            if current in seen:
                break
            seen.add(current)
            nxt = await self.session.scalar(
                select(Person.merged_into_id).where(Person.id == current)
            )
            if not nxt:
                return current
            current = nxt
        return current

    async def resolve(self, identifiers: list[IdentifierIn], *, seen_at: datetime) -> Resolution:
        """Return the person these identifiers belong to, creating or merging as
        the evidence allows."""
        if not identifiers:
            person = Person()
            self.session.add(person)
            await self.session.flush()
            return Resolution(person_id=person.id, created=True)

        keys = [(i.kind, i.value) for i in identifiers]
        rows = (
            await self.session.scalars(
                select(Identifier).where(tuple_(Identifier.kind, Identifier.value).in_(keys))
            )
        ).all()

        # Which persons does each *kind* of evidence point at?
        by_strength: dict[bool, dict[str, IdentifierIn]] = {True: {}, False: {}}
        for row in rows:
            canonical = await self.canonical_id(row.person_id)
            incoming = IdentifierIn(row.kind, row.value)
            by_strength[incoming.is_strong].setdefault(canonical, incoming)

        strong_matches = by_strength[True]
        weak_matches = {p: i for p, i in by_strength[False].items() if p not in strong_matches}

        resolution: Resolution
        to_attach = identifiers
        if strong_matches:
            resolution = await self._resolve_from_strong(strong_matches, weak_matches, seen_at)
        elif weak_matches:
            resolution, to_attach = await self._resolve_from_weak(weak_matches, identifiers)
        else:
            person = Person()
            self.session.add(person)
            await self.session.flush()
            resolution = Resolution(person_id=person.id, created=True)

        await self._attach(resolution.person_id, to_attach, seen_at)
        return resolution

    async def _resolve_from_weak(
        self, weak_matches: dict[str, IdentifierIn], identifiers: list[IdentifierIn]
    ) -> tuple[Resolution, list[IdentifierIn]]:
        """Only weak evidence points at an existing person.

        Two very different situations hide here. If the matched person is still
        anonymous, this is the same browser identifying itself and everything
        attaches. But if they already have strong identifiers and the incoming
        event carries a *new* strong identifier, the honest reading is a second
        human on a shared device — grafting her email onto the first person's
        profile would hand her their entire order history. So: new person, and the
        pair goes to the review queue with the shared device recorded.
        """
        incoming_strong = [i for i in identifiers if i.is_strong]
        identified = [p for p in weak_matches if await self._has_strong_identifier(p)]

        if incoming_strong and identified:
            person = Person()
            self.session.add(person)
            await self.session.flush()
            resolution = Resolution(person_id=person.id, created=True)

            for other in identified:
                link = weak_matches[other]
                review = MergeReview(
                    person_a_id=person.id,
                    person_b_id=other,
                    linked_by_kind=link.kind,
                    linked_by_value=link.value,
                    reason="weak_link_between_identified_people",
                )
                self.session.add(review)
                await self.session.flush()
                resolution.review_ids.append(review.id)

            # The shared identifier keeps its existing owner: stealing it would
            # just move the same wrong assumption in the other direction.
            owned_weak = {(i.kind, i.value) for i in weak_matches.values()}
            keep = [i for i in identifiers if (i.kind, i.value) not in owned_weak]
            return resolution, keep

        # Nobody involved has a strong identifier, so nothing can be exposed by
        # collapsing them: two anonymous browser identities.
        return await self._merge_group(weak_matches, reason="weak_only"), identifiers

    async def _resolve_from_strong(
        self,
        strong_matches: dict[str, IdentifierIn],
        weak_matches: dict[str, IdentifierIn],
        seen_at: datetime,
    ) -> Resolution:
        # Two strong identifiers arriving on the same event is first-party
        # evidence from the source system that this is one customer — a checkout
        # carrying both her email and her phone. Merge.
        resolution = await self._merge_group(strong_matches, reason="strong_link")

        for person_id, link in weak_matches.items():
            if await self._has_strong_identifier(person_id):
                # The family-iPad case. Ask.
                review = MergeReview(
                    person_a_id=resolution.person_id,
                    person_b_id=person_id,
                    linked_by_kind=link.kind,
                    linked_by_value=link.value,
                    reason="weak_link_between_identified_people",
                )
                self.session.add(review)
                await self.session.flush()
                resolution.review_ids.append(review.id)
            else:
                folded = await self._merge_pair(
                    resolution.person_id, person_id, link, reason="weak_link_to_anonymous"
                )
                resolution.merged_person_ids.extend(folded)
        return resolution

    async def _has_strong_identifier(self, person_id: str) -> bool:
        found = await self.session.scalar(
            select(Identifier.id)
            .where(Identifier.person_id == person_id, Identifier.kind.in_(STRONG_KINDS))
            .limit(1)
        )
        return found is not None

    async def _merge_group(self, matches: dict[str, IdentifierIn], *, reason: str) -> Resolution:
        """Collapse a set of persons into the oldest of them. Oldest wins so the
        id that has been handed to downstream systems the longest survives."""
        person_ids = list(matches)
        if len(person_ids) == 1:
            return Resolution(person_id=person_ids[0])

        ordered = (
            await self.session.scalars(
                select(Person)
                .where(Person.id.in_(person_ids))
                .order_by(Person.created_at, Person.id)
            )
        ).all()
        winner = ordered[0]
        resolution = Resolution(person_id=winner.id)
        for loser in ordered[1:]:
            link = matches[loser.id]
            resolution.merged_person_ids.extend(
                await self._merge_pair(winner.id, loser.id, link, reason=reason)
            )
        return resolution

    async def _merge_pair(
        self, winner_id: str, loser_id: str, link: IdentifierIn, *, reason: str
    ) -> list[str]:
        if winner_id == loser_id:
            return []

        for model in _REPOINTED:
            await self.session.execute(
                update(model).where(model.person_id == loser_id).values(person_id=winner_id)
            )
        # Derived rows are DROPPED, not repointed: the winner already has its own,
        # and both are recomputed from the now-combined events by the caller.
        # Repointing would collide on the primary key, and adding pre-aggregated
        # totals together is exactly the reconciliation this design avoids.
        for model in (ProfileTraits, PersonBrandStat):
            await self.session.execute(delete(model).where(model.person_id == loser_id))

        await self.session.execute(
            update(Person).where(Person.id == loser_id).values(merged_into_id=winner_id)
        )
        self.session.add(
            IdentityMerge(
                winner_person_id=winner_id,
                loser_person_id=loser_id,
                linked_by_kind=link.kind,
                linked_by_value=link.value,
                reason=reason,
            )
        )
        self.session.add(
            AuditLog(
                actor=self.actor,
                action="identity.merge",
                entity="person",
                entity_id=winner_id,
                meta={"loser": loser_id, "reason": reason, "linked_by": link.kind},
            )
        )
        await self.session.flush()
        return [loser_id]

    async def _attach(
        self, person_id: str, identifiers: list[IdentifierIn], seen_at: datetime
    ) -> None:
        keys = [(i.kind, i.value) for i in identifiers]
        existing = {
            (row.kind, row.value): row
            for row in (
                await self.session.scalars(
                    select(Identifier).where(tuple_(Identifier.kind, Identifier.value).in_(keys))
                )
            ).all()
        }
        for ident in identifiers:
            row = existing.get((ident.kind, ident.value))
            if row is None:
                self.session.add(
                    Identifier(
                        person_id=person_id,
                        kind=ident.kind,
                        value=ident.value,
                        first_seen_at=seen_at,
                        last_seen_at=seen_at,
                        capture_context=ident.capture_context,
                    )
                )
            else:
                if seen_at > row.last_seen_at:
                    row.last_seen_at = seen_at
                if seen_at < row.first_seen_at:
                    row.first_seen_at = seen_at
                # A number first written on a mall form and later typed into a
                # checkout by the customer herself has been confirmed by her.
                # Risk is cleared by better evidence, never added by weaker:
                # otherwise one gift order would permanently mark a number that
                # its owner has used a hundred times.
                confirms_it = (
                    row.capture_context in THIRD_PARTY_CONTEXTS
                    and ident.capture_context not in THIRD_PARTY_CONTEXTS
                    and ident.capture_context != "unknown"
                )
                if confirms_it or row.capture_context in (None, "unknown"):
                    row.capture_context = ident.capture_context
                # A row still pointing at a merged-away person is repointed here;
                # the unique constraint means it can only ever belong to one.
                row.person_id = person_id
        await self.session.flush()
