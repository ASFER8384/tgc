"""What one supplier has actually cost, counted from their own record.

Everything here is derived from rows this system wrote while doing its job:
orders it raised, replies it read, exceptions it filed, receipts somebody booked
in. Nothing is fetched from outside and nothing is estimated. Where there is not
enough history to answer, the answer is that there is not enough history — a
zero would read as "they have never been late", which is a claim, and the
opposite of what an empty table means.

Two deliberate refusals.

Nothing here is folded into a supplier score. A single number would rank a mill
that ships perfectly but only makes one thing above a mill that is occasionally
late and is the only source of three, and the buyer facing a stockout wants
those two facts separately. The console prints them separately.

Reliability is never called quality. Nothing in this system inspects goods, and
a column named quality would be claiming a measurement nobody takes.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sca.coordination.service import CLARIFYING_KINDS, _median
from sca.models import (
    AuditLog,
    InboundMessage,
    Issue,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplierItem,
)

# An order booked in within a day of being sent is a demo click, a backfill or a
# correction to something that had already arrived. Averaging those in reports a
# supplier who delivers instantly, which is worse than reporting nothing because
# it looks measured. Same floor as the arrival estimate uses, for the same
# reason.
MIN_CREDIBLE_SPAN_DAYS = 1.0


@dataclass
class Exclusivity:
    """What stops if they stop.

    The one figure here that needs no history at all, which makes it the only
    one worth anything on a supplier you have just added.
    """

    items: int = 0
    only_source: int = 0
    only_source_skus: list[str] = field(default_factory=list)


@dataclass
class Commitment:
    """Orders raised with them, and what became of those orders."""

    orders: int = 0
    live: int = 0
    received: int = 0
    cancelled: int = 0
    value: str = "0.00"
    currency: str = "SAR"
    # Their share of everything committed across all suppliers, so a large
    # number reads as large relative to the business rather than in isolation.
    share_of_spend: float | None = None


@dataclass
class Trouble:
    """Exceptions this system filed against them, by kind.

    Deliberately not a rate. Two issues against forty orders and two against two
    are different situations, so the order count travels beside it rather than
    being divided into it — a rate would hide which of the two this is.
    """

    total: int = 0
    open_now: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)


@dataclass
class Exchanges:
    """How many messages an order to them takes, and how many needed not to."""

    sent: int = 0
    replies: int = 0
    per_order: float | None = None
    # Replies that asked us something rather than answering. Each is a full wait
    # a more complete outbound message could have removed.
    clarifications: int = 0
    avoidable_share: float | None = None
    acknowledged: int = 0
    awaiting: int = 0
    median_hours: float | None = None


@dataclass
class Kept:
    """Did the goods land when they said they would.

    The most useful thing you can know about a supplier and the one that takes
    longest to earn: it needs an order that was promised a date and then
    actually booked in. Until then ``basis`` says why it is empty, because a
    blank here must not read as "never late".
    """

    checked: int = 0
    on_time: int = 0
    median_days_late: float | None = None
    worst_days_late: float | None = None
    basis: str = "no completed order with a promised date to check against"


@dataclass
class Short:
    """Lines that came in under what was ordered."""

    lines_received: int = 0
    lines_short: int = 0
    units_short: int = 0


@dataclass
class SupplierStats:
    supplier: str
    code: str
    exclusivity: Exclusivity = field(default_factory=Exclusivity)
    commitment: Commitment = field(default_factory=Commitment)
    trouble: Trouble = field(default_factory=Trouble)
    exchanges: Exchanges = field(default_factory=Exchanges)
    kept: Kept = field(default_factory=Kept)
    short: Short = field(default_factory=Short)

    def as_dict(self) -> dict:
        return asdict(self)


class SupplierAnalyticsService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def collect(
        self, supplier: Supplier, *, now: datetime | None = None
    ) -> SupplierStats:
        now = now or datetime.now(UTC)
        out = SupplierStats(supplier=supplier.name, code=supplier.code)
        orders = list(
            await self.session.scalars(
                select(PurchaseOrder).where(PurchaseOrder.supplier_id == supplier.id)
            )
        )
        out.exclusivity = await self._exclusivity(supplier)
        out.commitment = await self._commitment(supplier, orders)
        out.trouble = await self._trouble(supplier)
        out.exchanges = await self._exchanges(orders)
        out.kept = await self._kept(orders)
        out.short = await self._short(orders)
        return out

    async def _exclusivity(self, supplier: Supplier) -> Exclusivity:
        mine = [
            row.sku
            for row in await self.session.scalars(
                select(SupplierItem).where(
                    SupplierItem.supplier_id == supplier.id,
                    SupplierItem.active.is_(True),
                )
            )
        ]
        if not mine:
            return Exclusivity()
        counts = dict(
            (
                await self.session.execute(
                    select(SupplierItem.sku, func.count(SupplierItem.id))
                    .where(SupplierItem.sku.in_(mine), SupplierItem.active.is_(True))
                    .group_by(SupplierItem.sku)
                )
            ).all()
        )
        alone = sorted(sku for sku in mine if counts.get(sku, 0) <= 1)
        return Exclusivity(items=len(mine), only_source=len(alone), only_source_skus=alone)

    async def _commitment(
        self, supplier: Supplier, orders: list[PurchaseOrder]
    ) -> Commitment:
        out = Commitment(orders=len(orders), currency=supplier.currency)
        # Cancelled orders are excluded from the value and counted separately.
        # Money that was never going to be spent is not commitment, but an order
        # abandoned after being raised is a fact about either the supplier or the
        # planning that produced it, and worth its own line.
        standing = [o for o in orders if o.status != "cancelled"]
        out.cancelled = len(orders) - len(standing)
        out.received = sum(1 for o in standing if o.status == "received")
        out.live = len(standing) - out.received
        total = sum(float(o.total_value or 0) for o in standing)
        out.value = f"{total:.2f}"

        # Their share of everything committed. Currencies are not converted —
        # comparing across them needs a rate somebody owns — so the share is
        # taken within this supplier's own currency and is null where the
        # business buys in more than one.
        everyone = await self.session.scalar(
            select(func.coalesce(func.sum(PurchaseOrder.total_value), 0)).where(
                PurchaseOrder.status != "cancelled",
                PurchaseOrder.currency == supplier.currency,
            )
        )
        if everyone:
            out.share_of_spend = round(total / float(everyone), 3)
        return out

    async def _trouble(self, supplier: Supplier) -> Trouble:
        rows = (
            await self.session.execute(
                select(Issue.kind, Issue.status, func.count(Issue.id))
                .where(Issue.supplier_id == supplier.id)
                .group_by(Issue.kind, Issue.status)
            )
        ).all()
        out = Trouble()
        for kind, status, count in rows:
            out.total += count
            out.by_kind[kind] = out.by_kind.get(kind, 0) + count
            if status == "open":
                out.open_now += count
        return out

    async def _exchanges(self, orders: list[PurchaseOrder]) -> Exchanges:
        sent = [o for o in orders if o.sent_at is not None]
        out = Exchanges(sent=len(sent))
        if not sent:
            return out
        rows = (
            await self.session.execute(
                select(InboundMessage.kind, func.count(InboundMessage.id))
                .where(InboundMessage.purchase_order_id.in_([o.id for o in sent]))
                .group_by(InboundMessage.kind)
            )
        ).all()
        by_kind = {kind: count for kind, count in rows}
        out.replies = sum(by_kind.values())
        out.clarifications = sum(by_kind.get(k, 0) for k in CLARIFYING_KINDS)
        out.per_order = round(out.replies / len(sent), 2)
        if out.replies:
            out.avoidable_share = round(out.clarifications / out.replies, 3)

        waits = []
        for order in sent:
            if order.acknowledged_at is None:
                out.awaiting += 1
                continue
            out.acknowledged += 1
            waits.append((order.acknowledged_at - order.sent_at).total_seconds() / 3600)
        out.median_hours = _median(waits)
        return out

    async def _kept(self, orders: list[PurchaseOrder]) -> Kept:
        """Promised date against the day it was actually booked in.

        The receipt time comes from the audit log rather than a column, because
        the log already records exactly when somebody booked the goods in — and
        an order can be received without anything on the order itself moving.
        """
        candidates = {
            o.id: o
            for o in orders
            if o.status == "received"
            and o.confirmed_delivery_date is not None
            and o.sent_at is not None
        }
        out = Kept()
        if not candidates:
            return out
        receipts = list(
            await self.session.scalars(
                select(AuditLog).where(
                    AuditLog.action == "order.receive",
                    AuditLog.entity_id.in_(list(candidates)),
                )
            )
        )
        gaps: list[float] = []
        for row in receipts:
            order = candidates.get(row.entity_id)
            if order is None:
                continue
            if (row.created_at - order.sent_at).total_seconds() / 86400 < MIN_CREDIBLE_SPAN_DAYS:
                # Sent and received within the day: a test click or a backfill,
                # not a delivery anybody can be judged on.
                continue
            gaps.append(
                (row.created_at - order.confirmed_delivery_date).total_seconds() / 86400
            )
        if not gaps:
            out.basis = (
                "every completed order was booked in the same day it was sent, "
                "so none can be judged"
            )
            return out
        out.checked = len(gaps)
        # On time means on or before the promised day. A few hours past midnight
        # on the promised date is not a late delivery, it is a clock.
        out.on_time = sum(1 for g in gaps if g <= 1)
        out.median_days_late = _median(gaps)
        out.worst_days_late = round(max(gaps), 1)
        out.basis = f"{len(gaps)} completed order(s) that carried a promised date"
        return out

    async def _short(self, orders: list[PurchaseOrder]) -> Short:
        received = [o.id for o in orders if o.status == "received"]
        out = Short()
        if not received:
            return out
        lines = list(
            await self.session.scalars(
                select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id.in_(received))
            )
        )
        out.lines_received = len(lines)
        for line in lines:
            missing = line.quantity - line.received_quantity
            if missing > 0:
                out.lines_short += 1
                out.units_short += missing
        return out
