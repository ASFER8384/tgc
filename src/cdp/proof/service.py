"""Does any of this actually work, on the real data.

Everything the CDP promises is currently provable only by reading test files.
A customer cannot read test files. These are the same claims measured against
whatever is in the database right now, so a number that looks wrong is a real
problem rather than a demo that needs re-recording.

Three questions, in the order somebody sceptical would ask them:

1. Is it joining records at all, or just storing them? — identifiers per person.
2. Is that worth anything? — how much revenue belongs to a customer we can name.
3. Do the rules actually fire? — what got refused, and why.

Every figure is computed live. Nothing is cached, because a stale proof is worse
than no proof.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, case, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.models import (
    STRONG_KINDS,
    THIRD_PARTY_CONTEXTS,
    ActivationRun,
    Event,
    Identifier,
    IdentityMerge,
    MergeReview,
    Person,
)

PURCHASE_EVENT = "order_paid"


@dataclass(frozen=True)
class Stitching:
    """Evidence that resolution is doing something.

    A CDP where identifiers and people are the same number has resolved nothing —
    every record is its own customer, which is the state the retailer was already
    in. The ratio is the claim, and merges are how it was earned.
    """

    identifiers: int
    people: int
    merges: int
    open_reviews: int

    @property
    def identifiers_per_person(self) -> float:
        return round(self.identifiers / self.people, 2) if self.people else 0.0


@dataclass(frozen=True)
class Attribution:
    """How much of the business is attached to somebody we can name.

    This is the number that justifies the project. Unattributed revenue is not a
    failure — a walk-in who pays cash and gives no details is genuinely anonymous
    and should stay that way — but it bounds what any customer programme can
    reach, and nobody can plan against a bound they have not measured.
    """

    currency: str
    known_amount: Decimal
    total_amount: Decimal
    known_orders: int
    total_orders: int

    @property
    def share(self) -> float:
        if not self.total_amount:
            return 0.0
        return round(float(self.known_amount) / float(self.total_amount), 4)


@dataclass(frozen=True)
class Refusals:
    """What the consent and provenance rules stopped.

    Reported next to what was delivered on purpose. A refusal count with no
    delivery count beside it reads as a broken pipeline, and a delivery count
    with no refusals reads as a system with the safety switched off.
    """

    delivered: int
    skipped_no_consent: int
    skipped_identifier_risk: int
    risky_identifiers: int


@dataclass(frozen=True)
class Proof:
    stitching: Stitching
    attribution: Attribution
    refusals: Refusals


def _live_person() -> Select:
    """Persons that still stand for themselves.

    A merged-away person keeps its row so old ids keep resolving, but counting it
    would understate exactly the work this measurement exists to show.
    """
    return select(func.count(Person.id)).where(Person.merged_into_id.is_(None))


class ProofService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def collect(self) -> Proof:
        return Proof(
            stitching=await self.stitching(),
            attribution=await self.attribution(),
            refusals=await self.refusals(),
        )

    async def stitching(self) -> Stitching:
        identifiers = await self.session.scalar(select(func.count(Identifier.id))) or 0
        people = await self.session.scalar(_live_person()) or 0
        merges = (
            await self.session.scalar(
                select(func.count(IdentityMerge.id)).where(IdentityMerge.reverted_at.is_(None))
            )
            or 0
        )
        open_reviews = (
            await self.session.scalar(
                select(func.count(MergeReview.id)).where(MergeReview.status == "open")
            )
            or 0
        )
        return Stitching(
            identifiers=identifiers, people=people, merges=merges, open_reviews=open_reviews
        )

    async def attribution(self) -> Attribution:
        # "Known" means the person carries at least one identifier that names a
        # human by itself. A cart token is not a name, so an order carrying only
        # one is anonymous however much it is worth.
        known = exists().where(
            Identifier.person_id == Event.person_id,
            Identifier.kind.in_(STRONG_KINDS),
        )
        row = (
            await self.session.execute(
                select(
                    func.count(Event.id),
                    func.coalesce(func.sum(Event.value_amount), 0),
                    func.coalesce(func.sum(case((known, 1), else_=0)), 0),
                    func.coalesce(func.sum(case((known, Event.value_amount), else_=0)), 0),
                ).where(Event.name == PURCHASE_EVENT)
            )
        ).one()
        total_orders, total_amount, known_orders, known_amount = row

        currency = (
            await self.session.scalar(
                select(Event.currency)
                .where(Event.name == PURCHASE_EVENT, Event.currency.is_not(None))
                .group_by(Event.currency)
                .order_by(func.count(Event.id).desc())
                .limit(1)
            )
            or "SAR"
        )
        return Attribution(
            currency=currency,
            known_amount=Decimal(known_amount or 0),
            total_amount=Decimal(total_amount or 0),
            known_orders=int(known_orders or 0),
            total_orders=int(total_orders or 0),
        )

    async def refusals(self) -> Refusals:
        row = (
            await self.session.execute(
                select(
                    func.coalesce(func.sum(ActivationRun.delivered), 0),
                    func.coalesce(func.sum(ActivationRun.skipped_no_consent), 0),
                    func.coalesce(func.sum(ActivationRun.skipped_identifier_risk), 0),
                )
            )
        ).one()
        risky = (
            await self.session.scalar(
                select(func.count(Identifier.id)).where(
                    Identifier.capture_context.in_(THIRD_PARTY_CONTEXTS)
                )
            )
            or 0
        )
        return Refusals(
            delivered=int(row[0]),
            skipped_no_consent=int(row[1]),
            skipped_identifier_risk=int(row[2]),
            risky_identifiers=risky,
        )
