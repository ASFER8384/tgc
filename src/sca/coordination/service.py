"""What the coordination actually cost, measured from the orders themselves.

The claim this half of the platform makes is not that it parses email quickly.
It is that elapsed time is governed by the number of message round trips rather
than by processing speed, and that fewer round trips is therefore worth more
than faster parsing. That claim is either true of this supplier network or it is
not, and nothing here decides it in advance — every number below is counted from
rows the system wrote while doing its job.

Two things are measured apart on purpose.

Wall-clock hours and the supplier's own working hours are both reported, because
their difference *is* the argument. An order acknowledged in 26 hours of which
four were inside the supplier's working day did not take a day of anybody's
attention; it took four hours of work and twenty-two of nobody being there. A
system that only reports the 26 is measuring the timezone, not the coordination.

Results are stratified by supplier timezone rather than averaged, because the
benefit of removing a round trip scales with the wait until the next overlap. An
aggregate would be large for the mill with no overlap, small for the one six
hours away, and honest about neither.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sca.config import Settings, get_settings
from sca.inbound.service import ACT_THRESHOLD
from sca.models import InboundMessage, Issue, PurchaseOrder, Supplier
from sca.scheduling.windows import WorkingHours, overlap_hours, working_hours_between

# A reply that asks us something is a round trip the outbound message could have
# avoided by saying it in the first place. This is the quantity the completeness
# argument stands or falls on, so it is counted separately from every other kind
# of reply rather than folded into a total.
CLARIFYING_KINDS = ("question", "unknown")


def _median(values: list[float]) -> float | None:
    """Median rather than mean: one supplier who took three weeks would otherwise
    describe a network that usually answers the same day."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 1)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)


@dataclass
class RoundTrips:
    """How many exchanges a transaction took, and how many needed not to have."""

    orders: int = 0
    replies: int = 0
    per_order: float | None = None
    clarifications: int = 0
    # The share of replies that were the supplier asking rather than answering.
    # This is the headroom: every one of these is a full wait that a more
    # complete outbound message could have removed.
    avoidable_share: float | None = None


@dataclass
class Cycle:
    """Order out, acknowledgement back."""

    acknowledged: int = 0
    awaiting: int = 0
    median_hours: float | None = None
    # The same wait counted only while the supplier was at their desk. The gap
    # between this and the line above is the timezone, not the supplier.
    median_working_hours: float | None = None


@dataclass
class Zone:
    """One supplier timezone, and how much of the day it shares with Riyadh."""

    timezone: str
    suppliers: int = 0
    orders: int = 0
    # The number that explains the project. Zero means every question costs a
    # full day whatever anyone does about it.
    overlap_hours: float = 0.0
    median_hours: float | None = None
    median_working_hours: float | None = None


@dataclass
class Autonomy:
    """How much the system did without asking, per the confidence gate."""

    read: int = 0
    acted: int = 0
    filed_for_a_human: int = 0
    threshold: float = ACT_THRESHOLD
    acted_share: float | None = None


@dataclass
class Approval:
    """The commitment gate: the one tier that is never automatic.

    ``without_amendment`` is reported because it is the automation-bias
    indicator. A gate whose prepared orders are always approved unchanged is
    either preparing them well or being waved through, and the two look
    identical from inside. It is a number to watch, not a score to maximise.
    """

    needed: int = 0
    granted: int = 0
    without_amendment: int = 0
    revised: int = 0
    cancelled: int = 0


@dataclass
class Coordination:
    round_trips: RoundTrips = field(default_factory=RoundTrips)
    cycle: Cycle = field(default_factory=Cycle)
    zones: list[Zone] = field(default_factory=list)
    autonomy: Autonomy = field(default_factory=Autonomy)
    approval: Approval = field(default_factory=Approval)

    def as_dict(self) -> dict:
        return asdict(self)


class CoordinationService:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    async def collect(self, *, now: datetime | None = None) -> Coordination:
        now = now or datetime.now(UTC)
        suppliers = {s.id: s for s in await self.session.scalars(select(Supplier))}
        sent = list(
            await self.session.scalars(
                select(PurchaseOrder).where(PurchaseOrder.sent_at.is_not(None))
            )
        )
        return Coordination(
            round_trips=await self._round_trips(sent),
            cycle=self._cycle(sent, suppliers),
            zones=self._zones(sent, suppliers, now),
            autonomy=await self._autonomy(),
            approval=await self._approval(),
        )

    async def _round_trips(self, sent: list[PurchaseOrder]) -> RoundTrips:
        """One reply is one completed exchange, so replies per order is the
        round-trip count for that order.

        Only replies matched to an order are counted. An unmatched message is a
        resolution failure, which is a different problem measured elsewhere, and
        including it here would inflate the count for orders that never saw it.
        """
        out = RoundTrips(orders=len(sent))
        if not sent:
            return out
        ids = [o.id for o in sent]
        rows = (
            await self.session.execute(
                select(InboundMessage.kind, func.count(InboundMessage.id))
                .where(InboundMessage.purchase_order_id.in_(ids))
                .group_by(InboundMessage.kind)
            )
        ).all()
        by_kind = {kind: count for kind, count in rows}
        out.replies = sum(by_kind.values())
        out.clarifications = sum(by_kind.get(k, 0) for k in CLARIFYING_KINDS)
        out.per_order = round(out.replies / len(sent), 2)
        if out.replies:
            out.avoidable_share = round(out.clarifications / out.replies, 3)
        return out

    def _cycle(self, sent: list[PurchaseOrder], suppliers: dict[str, Supplier]) -> Cycle:
        out = Cycle()
        wall, working = [], []
        for order in sent:
            if order.acknowledged_at is None:
                out.awaiting += 1
                continue
            supplier = suppliers.get(order.supplier_id)
            if supplier is None:
                continue
            out.acknowledged += 1
            wall.append((order.acknowledged_at - order.sent_at).total_seconds() / 3600)
            working.append(
                working_hours_between(
                    WorkingHours.from_supplier(supplier), order.sent_at, order.acknowledged_at
                )
            )
        out.median_hours = _median(wall)
        out.median_working_hours = _median(working)
        return out

    def _zones(
        self, sent: list[PurchaseOrder], suppliers: dict[str, Supplier], now: datetime
    ) -> list[Zone]:
        home = WorkingHours(timezone=self.settings.home_timezone)
        zones: dict[str, Zone] = {}
        waits: dict[str, list[float]] = {}
        work_waits: dict[str, list[float]] = {}

        for supplier in suppliers.values():
            zone = zones.setdefault(supplier.timezone, Zone(timezone=supplier.timezone))
            zone.suppliers += 1
            # Recomputed per supplier and kept as the smallest, because two
            # suppliers in one timezone can still keep different hours, and the
            # tighter overlap is the one that governs a reply.
            hours = WorkingHours.from_supplier(supplier)
            shared = overlap_hours(home, hours, now)
            zone.overlap_hours = (
                shared if zone.suppliers == 1 else min(zone.overlap_hours, shared)
            )

        for order in sent:
            supplier = suppliers.get(order.supplier_id)
            if supplier is None:
                continue
            zone = zones[supplier.timezone]
            zone.orders += 1
            if order.acknowledged_at is None:
                continue
            waits.setdefault(supplier.timezone, []).append(
                (order.acknowledged_at - order.sent_at).total_seconds() / 3600
            )
            work_waits.setdefault(supplier.timezone, []).append(
                working_hours_between(
                    WorkingHours.from_supplier(supplier), order.sent_at, order.acknowledged_at
                )
            )

        for name, zone in zones.items():
            zone.median_hours = _median(waits.get(name, []))
            zone.median_working_hours = _median(work_waits.get(name, []))
        # Least overlap first: that is where a removed round trip is worth most,
        # and therefore where anyone reading this should look first.
        return sorted(zones.values(), key=lambda z: (z.overlap_hours, z.timezone))

    async def _autonomy(self) -> Autonomy:
        """Read against acted, by the one confidence gate that exists.

        Named a tier deliberately: at present there is a single threshold
        covering filing a document and writing a confirmed date, which are not
        the same commitment. The number below is honest about what is measured —
        how often the system decided by itself — and it is the baseline against
        which per-tier thresholds would later be judged.
        """
        out = Autonomy()
        out.read = await self.session.scalar(
            select(func.count()).select_from(InboundMessage)
        ) or 0
        out.filed_for_a_human = await self.session.scalar(
            select(func.count()).select_from(Issue).where(Issue.kind == "unparsed_message")
        ) or 0
        out.acted = max(0, out.read - out.filed_for_a_human)
        if out.read:
            out.acted_share = round(out.acted / out.read, 3)
        return out

    async def _approval(self) -> Approval:
        gated = PurchaseOrder.requires_approval.is_(True)
        needed = await self.session.scalar(
            select(func.count()).select_from(PurchaseOrder).where(gated)
        ) or 0
        granted = await self.session.scalar(
            select(func.count())
            .select_from(PurchaseOrder)
            .where(gated, PurchaseOrder.approved_at.is_not(None))
        ) or 0
        # A revision is the amendment: it is what happens when the prepared order
        # was not the one the buyer was willing to place.
        revised = await self.session.scalar(
            select(func.count())
            .select_from(PurchaseOrder)
            .where(PurchaseOrder.requires_approval.is_(True), PurchaseOrder.revision > 0)
        ) or 0
        cancelled = await self.session.scalar(
            select(func.count())
            .select_from(PurchaseOrder)
            .where(
                PurchaseOrder.requires_approval.is_(True),
                PurchaseOrder.status == "cancelled",
            )
        ) or 0
        return Approval(
            needed=needed,
            granted=granted,
            without_amendment=max(0, granted - revised),
            revised=revised,
            cancelled=cancelled,
        )
