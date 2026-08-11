from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.config import get_settings
from cdp.ingest.schemas import PURCHASE_EVENTS
from cdp.models import Event, Person, PersonBrandStat, ProfileTraits


def _score(
    value: float | int | Decimal, thresholds: tuple[int, ...], *, invert: bool = False
) -> int:
    """Map a value onto 1..5 against fixed thresholds. `invert` for recency, where
    a smaller number of days is the better score."""
    band = 1
    for threshold in thresholds:
        if value >= threshold:
            band += 1
    if invert:
        # Recency: 0 days ago must land on 5, not 1.
        band = len(thresholds) + 2 - band
    return max(1, min(5, band))


class ProfileService:
    """Recompute derived traits for one person from their events.

    Full recompute rather than incremental deltas. It is O(events per person) —
    small for retail — and it means an identity merge, a refund, or a corrected
    trait definition all converge to the right answer by re-running, with no
    reconciliation path to get wrong.
    """

    def __init__(self, session: AsyncSession, *, now: datetime | None = None):
        self.session = session
        self._now = now
        self.settings = get_settings()

    @property
    def now(self) -> datetime:
        return self._now or datetime.now(UTC)

    async def recompute(self, person_id: str) -> ProfileTraits:
        events = (
            await self.session.scalars(
                select(Event).where(Event.person_id == person_id).order_by(Event.occurred_at)
            )
        ).all()

        orders = [e for e in events if e.name in PURCHASE_EVENTS]
        refunded = {
            str((e.payload or {}).get("refunded_order_id"))
            for e in events
            if e.name == "order_refunded"
        }
        orders = [o for o in orders if str((o.payload or {}).get("order_id")) not in refunded]

        order_count = len(orders)
        ltv = sum((o.value_amount or Decimal("0") for o in orders), Decimal("0"))
        aov = (ltv / order_count).quantize(Decimal("0.01")) if order_count else Decimal("0")
        first_order_at = orders[0].occurred_at if orders else None
        last_order_at = orders[-1].occurred_at if orders else None

        recency_days: int | None = None
        if last_order_at is not None:
            last = last_order_at if last_order_at.tzinfo else last_order_at.replace(tzinfo=UTC)
            recency_days = max(0, (self.now - last).days)

        rfm = None
        if order_count:
            rfm = "".join(
                str(x)
                for x in (
                    _score(recency_days or 0, self.settings.rfm_recency_days, invert=True),
                    _score(order_count, self.settings.rfm_frequency_orders),
                    _score(ltv, self.settings.rfm_monetary_sar),
                )
            )

        brands = await self._rebuild_brand_stats(person_id, orders)

        # Preferred channel is where the customer actually engages, which is not
        # always where they buy — the WhatsApp-first market makes this the field
        # campaign design leans on most.
        channels = Counter(e.channel for e in events if e.channel)
        if channels:
            person = await self.session.get(Person, person_id)
            if person is not None:
                person.preferred_channel = channels.most_common(1)[0][0]

        traits = await self.session.get(ProfileTraits, person_id)
        if traits is None:
            traits = ProfileTraits(person_id=person_id, computed_at=self.now)
            self.session.add(traits)

        traits.order_count = order_count
        traits.ltv = ltv
        traits.aov = aov
        traits.first_order_at = first_order_at
        traits.last_order_at = last_order_at
        traits.recency_days = recency_days
        traits.rfm = rfm
        traits.brands_purchased = len(brands)
        traits.event_count = len(events)
        traits.computed_at = self.now

        await self.session.flush()
        return traits

    async def _rebuild_brand_stats(self, person_id: str, orders: list[Event]) -> dict[str, Decimal]:
        await self.session.execute(
            delete(PersonBrandStat).where(PersonBrandStat.person_id == person_id)
        )
        spend: dict[str, Decimal] = {}
        counts: Counter[str] = Counter()
        last_seen: dict[str, datetime] = {}

        for order in orders:
            for brand, amount in ((order.payload or {}).get("brands") or {}).items():
                if brand == "unassigned":
                    # Kept out of affinity so an unmapped Shopify vendor cannot
                    # masquerade as a fourth brand; it stays visible on the event.
                    continue
                value = Decimal(str(amount))
                spend[brand] = spend.get(brand, Decimal("0")) + value
                counts[brand] += 1
                last_seen[brand] = order.occurred_at

        for brand, total in spend.items():
            self.session.add(
                PersonBrandStat(
                    person_id=person_id,
                    brand=brand,
                    orders=counts[brand],
                    spend=total.quantize(Decimal("0.01")),
                    last_order_at=last_seen.get(brand),
                )
            )
        await self.session.flush()
        return spend
