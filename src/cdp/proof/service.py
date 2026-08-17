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
    PersonBrandStat,
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
    # Counted apart, because they are the only ones the claim is about. A cart
    # token is a browser session, not a way to reach anybody, and there are
    # fourteen of them for every phone number — so a ratio taken over all
    # identifiers describes browsing volume while appearing to describe
    # resolution. Reported here at 2.9 rather than 17, which is the true and
    # far less flattering number, and the one that survives being asked about.
    strong: int = 0

    @property
    def identifiers_per_person(self) -> float:
        return round(self.strong / self.people, 2) if self.people else 0.0

    @property
    def weak(self) -> int:
        return max(0, self.identifiers - self.strong)


@dataclass(frozen=True)
class CrossBrand:
    """How many customers are shared between the brands.

    The one number three separate Shopify stores physically cannot produce.
    Each store knows its own buyers; only something holding all three knows that
    most of them are the same women.
    """

    buyers: int
    two_or_more: int
    all_three: int
    # One row per person per brand: what three stores counting independently
    # would report between them, since each counts her once. Measured rather
    # than derived — buyers plus multi-brand buyers is not the same number, and
    # is wrong the moment anybody shops all three.
    counted_separately: int = 0

    @property
    def share(self) -> float:
        return round(self.two_or_more / self.buyers, 3) if self.buyers else 0.0


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
class Point:
    """One week of trade. Weeks rather than days because the shop does not sell
    evenly across a week, and a daily line would show the weekend rather than
    the trend."""

    week: str
    amount: Decimal
    orders: int


@dataclass(frozen=True)
class Slice:
    label: str
    amount: Decimal
    count: int


@dataclass(frozen=True)
class Band:
    """One rung of the repeat-purchase ladder.

    Both numbers matter and neither means much alone: the people who have bought
    once are always the largest group and usually the smallest share of the
    money, and it is the gap between those two facts that says whether this is a
    business built on regulars or on a stream of strangers.
    """

    label: str
    people: int
    amount: Decimal


@dataclass(frozen=True)
class Proof:
    stitching: Stitching
    cross_brand: CrossBrand
    attribution: Attribution
    refusals: Refusals
    trend: list[Point]
    brands: list[Slice]
    channels: list[Slice]
    loyalty: list[Band]


def _live_person() -> Select:
    """Persons that still stand for themselves.

    A merged-away person keeps its row so old ids keep resolving, but counting it
    would understate exactly the work this measurement exists to show. A synthetic
    record — the shop counter that anonymous sales land on — is left out for the
    opposite reason: counting it would overstate the customer base by one shop.
    """
    return select(func.count(Person.id)).where(
        Person.merged_into_id.is_(None), Person.synthetic.is_(False)
    )


class ProofService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def collect(self) -> Proof:
        return Proof(
            stitching=await self.stitching(),
            cross_brand=await self.cross_brand(),
            attribution=await self.attribution(),
            refusals=await self.refusals(),
            trend=await self.trend(),
            brands=await self.brands(),
            channels=await self.channels(),
            loyalty=await self.loyalty(),
        )

    async def trend(self, weeks: int = 12) -> list[Point]:
        """Revenue by week, most recent last.

        Grouped in Python rather than SQL: the two databases in play disagree
        about how to truncate a date, and a chart is not worth a dialect split.
        The row count here is orders, not events, so it stays small.
        """
        rows = (
            await self.session.execute(
                select(Event.occurred_at, Event.value_amount).where(
                    Event.name == PURCHASE_EVENT, Event.value_amount.is_not(None)
                )
            )
        ).all()
        if not rows:
            return []

        latest = max(occurred_at for occurred_at, _ in rows)
        buckets: dict[int, tuple[Decimal, int]] = {}
        for occurred_at, amount in rows:
            # Whole weeks back from the most recent order, so the newest bucket
            # is always complete rather than a partial week that reads as a crash.
            index = (latest - occurred_at).days // 7
            if index >= weeks:
                continue
            total, count = buckets.get(index, (Decimal(0), 0))
            buckets[index] = (total + Decimal(amount), count + 1)

        out = []
        for index in sorted(buckets, reverse=True):
            total, count = buckets[index]
            label = "this week" if index == 0 else f"-{index}w"
            out.append(Point(week=label, amount=total, orders=count))
        return out

    # Where the ladder is cut. Once and twice stand alone because the first
    # repeat is the step that matters most and burying it in a 1-2 band hides
    # it; above that the rungs widen, since the difference between a sixth order
    # and a tenth is not a difference anybody acts on.
    LOYALTY_BANDS = (
        ("Bought once", 1, 1),
        ("Twice", 2, 2),
        ("3 to 5 times", 3, 5),
        ("6 or more", 6, None),
    )

    async def loyalty(self) -> list[Band]:
        """How many people have bought how often, and what each group is worth.

        Summed across brands rather than per brand, because a woman who bought
        once from each of the three is a returning customer of this business
        however she looks to any one of its shops — and telling the group she is
        three first-timers is the mistake the whole platform exists to stop.
        """
        rows = (
            await self.session.execute(
                select(
                    PersonBrandStat.person_id,
                    func.coalesce(func.sum(PersonBrandStat.orders), 0),
                    func.coalesce(func.sum(PersonBrandStat.spend), 0),
                )
                .join(Person, Person.id == PersonBrandStat.person_id)
                # Same exclusions as every other count here: a merged-away person
                # would be counted beside the person they were merged into, and
                # the shop counter is not a customer.
                .where(Person.merged_into_id.is_(None), Person.synthetic.is_(False))
                .group_by(PersonBrandStat.person_id)
            )
        ).all()

        tally = {label: [0, Decimal(0)] for label, _, _ in self.LOYALTY_BANDS}
        for _, orders, spend in rows:
            orders = int(orders or 0)
            if orders < 1:
                continue
            for label, low, high in self.LOYALTY_BANDS:
                if orders >= low and (high is None or orders <= high):
                    tally[label][0] += 1
                    tally[label][1] += Decimal(spend or 0)
                    break

        return [
            Band(label=label, people=tally[label][0], amount=tally[label][1])
            for label, _, _ in self.LOYALTY_BANDS
        ]

    async def brands(self) -> list[Slice]:
        rows = (
            await self.session.execute(
                select(
                    PersonBrandStat.brand,
                    func.coalesce(func.sum(PersonBrandStat.spend), 0),
                    func.coalesce(func.sum(PersonBrandStat.orders), 0),
                )
                .group_by(PersonBrandStat.brand)
                .order_by(func.sum(PersonBrandStat.spend).desc())
            )
        ).all()
        return [
            Slice(label=brand, amount=Decimal(spend or 0), count=int(orders or 0))
            for brand, spend, orders in rows
        ]

    async def channels(self) -> list[Slice]:
        """Where the relationship actually happens, counted in touchpoints rather
        than orders — the messaging and activation channels produce very few
        orders and a great deal of contact, which is the point of holding them."""
        rows = (
            await self.session.execute(
                select(
                    Event.source,
                    func.coalesce(func.sum(Event.value_amount), 0),
                    func.count(Event.id),
                )
                .group_by(Event.source)
                .order_by(func.count(Event.id).desc())
            )
        ).all()
        return [
            Slice(label=source, amount=Decimal(amount or 0), count=int(count or 0))
            for source, amount, count in rows
        ]

    async def cross_brand(self) -> CrossBrand:
        counted = (
            select(
                PersonBrandStat.person_id,
                func.count(func.distinct(PersonBrandStat.brand)).label("brands"),
            )
            .group_by(PersonBrandStat.person_id)
            .subquery()
        )
        buyers = await self.session.scalar(select(func.count()).select_from(counted)) or 0
        two = (
            await self.session.scalar(
                select(func.count()).select_from(counted).where(counted.c.brands >= 2)
            )
            or 0
        )
        three = (
            await self.session.scalar(
                select(func.count()).select_from(counted).where(counted.c.brands >= 3)
            )
            or 0
        )
        separately = (
            await self.session.scalar(select(func.count()).select_from(PersonBrandStat)) or 0
        )
        return CrossBrand(
            buyers=buyers, two_or_more=two, all_three=three, counted_separately=separately
        )

    async def stitching(self) -> Stitching:
        identifiers = await self.session.scalar(select(func.count(Identifier.id))) or 0
        strong = (
            await self.session.scalar(
                select(func.count(Identifier.id)).where(Identifier.kind.in_(STRONG_KINDS))
            )
            or 0
        )
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
            identifiers=identifiers, people=people, merges=merges,
            open_reviews=open_reviews, strong=strong,
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
