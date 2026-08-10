"""Purchase order lifecycle, approval gates and timezone aware sending."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sca.config import get_settings
from sca.models import (
    ALLOWED_TRANSITIONS,
    AuditLog,
    Issue,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
)
from sca.scheduling.windows import WorkingHours, is_open, next_open


class OrderError(ValueError):
    """Raised for an illegal transition or a missing precondition."""


class OrderService:
    def __init__(self, session: AsyncSession, *, actor: str = "system"):
        self.session = session
        self.actor = actor
        self.settings = get_settings()

    # ---------------------------------------------------------------- creation
    async def create(
        self,
        supplier_id: str,
        lines: list[dict],
        *,
        origin: str = "forecast",
        now: datetime | None = None,
    ) -> PurchaseOrder:
        now = now or datetime.now(UTC)
        supplier = await self.session.get(Supplier, supplier_id)
        if supplier is None:
            raise OrderError(f"unknown supplier {supplier_id}")
        if not lines:
            raise OrderError("a purchase order needs at least one line")

        total = Decimal("0.00")
        order = PurchaseOrder(
            number=await self._next_number(),
            supplier_id=supplier_id,
            currency=supplier.currency,
            origin=origin,
            expected_delivery_date=now + timedelta(days=supplier.lead_time_days),
        )
        self.session.add(order)
        await self.session.flush()

        for line in lines:
            quantity = int(line["quantity"])
            unit_price = Decimal(str(line["unit_price"]))
            line_total = (unit_price * quantity).quantize(Decimal("0.01"))
            total += line_total
            self.session.add(
                PurchaseOrderLine(
                    purchase_order_id=order.id,
                    sku=line["sku"],
                    description=line.get("description") or line["sku"],
                    quantity=quantity,
                    unit_price=unit_price,
                    line_total=line_total,
                )
            )

        order.total_value = total
        requires, reason = await self._approval_policy(supplier, total)
        order.requires_approval = requires
        order.approval_reason = reason
        order.status = "pending_approval" if requires else "approved"

        self._audit("order.create", order.id, {"total": str(total), "requires_approval": requires})
        await self.session.flush()
        return order

    async def _approval_policy(
        self, supplier: Supplier, total: Decimal
    ) -> tuple[bool, str | None]:
        """Two gates, both about money the business cannot get back.

        Value is the obvious one. The second is a supplier who has never completed
        an order with us: their first shipment is the one most likely to go wrong,
        and it is the one nobody should be able to trigger automatically.
        """
        if float(total) >= self.settings.approval_threshold_sar:
            return True, (
                f"value {total} {supplier.currency} is at or above the "
                f"{self.settings.approval_threshold_sar:.0f} approval threshold"
            )
        completed = await self.session.scalar(
            select(func.count())
            .select_from(PurchaseOrder)
            .where(PurchaseOrder.supplier_id == supplier.id, PurchaseOrder.status == "received")
        )
        if not completed:
            return True, "first completed order with this supplier"
        return False, None

    async def _next_number(self) -> str:
        count = await self.session.scalar(select(func.count()).select_from(PurchaseOrder)) or 0
        return f"PO-{5000 + count + 1}"

    # ------------------------------------------------------------- transitions
    def _transition(self, order: PurchaseOrder, to: str) -> None:
        allowed = ALLOWED_TRANSITIONS.get(order.status, ())
        if to not in allowed:
            raise OrderError(f"cannot move {order.number} from {order.status} to {to}")
        order.status = to

    async def approve(self, order: PurchaseOrder, *, approver: str,
                      now: datetime | None = None) -> PurchaseOrder:
        now = now or datetime.now(UTC)
        self._transition(order, "approved")
        order.approved_by = approver
        order.approved_at = now
        self._audit("order.approve", order.id, {"approver": approver})
        await self.session.flush()
        return order

    async def send(self, order: PurchaseOrder, *, now: datetime | None = None) -> dict:
        """Send now, or schedule for the supplier's next working hour.

        Both outcomes are a success. The caller does not have to know which, and a
        buyer approving at 6pm Riyadh does not have to think about Guangzhou.
        """
        now = now or datetime.now(UTC)
        supplier = await self.session.get(Supplier, order.supplier_id)
        hours = WorkingHours.from_supplier(supplier)

        if not is_open(hours, now):
            order.scheduled_send_at = next_open(hours, now)
            self._audit("order.schedule", order.id, {"at": order.scheduled_send_at.isoformat()})
            await self.session.flush()
            return {
                "sent": False,
                "scheduled_send_at": order.scheduled_send_at,
                "reason": f"{supplier.name} is closed, queued for their next working hour",
            }

        self._transition(order, "sent")
        order.sent_at = now
        order.scheduled_send_at = None
        self._audit("order.send", order.id, {"channel": supplier.channel})
        await self.session.flush()
        return {"sent": True, "scheduled_send_at": None, "reason": None}

    async def acknowledge(
        self, order: PurchaseOrder, *, confirmed_date: datetime | None, now: datetime | None = None
    ) -> Issue | None:
        """Record the supplier's confirmation, and compare their date with ours.

        Returns an issue when the promised date has slipped past tolerance, which
        is the moment a buyer wants to know, rather than at the delivery date.
        """
        now = now or datetime.now(UTC)
        if order.status == "sent":
            self._transition(order, "acknowledged")
        order.acknowledged_at = now
        if confirmed_date:
            order.confirmed_delivery_date = confirmed_date

        issue = None
        expected = order.expected_delivery_date
        if confirmed_date and expected:
            slip_days = (confirmed_date.date() - expected.date()).days
            if slip_days >= self.settings.eta_slip_days:
                issue = Issue(
                    purchase_order_id=order.id,
                    supplier_id=order.supplier_id,
                    kind="eta_slip",
                    severity="high" if slip_days >= 14 else "medium",
                    detail=(
                        f"{order.number} confirmed for {confirmed_date.date()}, "
                        f"{slip_days} days later than the {expected.date()} we asked for"
                    ),
                    suggested_action=(
                        "Check cover for these lines against the new date and decide whether to "
                        "split the order or source part of it elsewhere"
                    ),
                    context={"slip_days": slip_days},
                )
                self.session.add(issue)
        self._audit("order.acknowledge", order.id, {"confirmed": bool(confirmed_date)})
        await self.session.flush()
        return issue

    async def receive(
        self, order: PurchaseOrder, received: dict[str, int], *, now: datetime | None = None
    ) -> list[Issue]:
        """Book in what arrived and raise a short shipment where it does not match."""
        now = now or datetime.now(UTC)
        lines = list(
            await self.session.scalars(
                select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
            )
        )
        issues: list[Issue] = []
        for line in lines:
            got = int(received.get(line.sku, line.quantity))
            line.received_quantity = got
            if got < line.quantity:
                short = line.quantity - got
                issues.append(
                    Issue(
                        purchase_order_id=order.id,
                        supplier_id=order.supplier_id,
                        kind="short_shipment",
                        severity="high" if short > line.quantity * 0.2 else "medium",
                        detail=f"{line.sku}: {got} received of {line.quantity} ordered",
                        suggested_action=(
                            "Raise the shortfall with the supplier before the invoice is paid, "
                            "and hold payment for the missing quantity"
                        ),
                        context={"sku": line.sku, "ordered": line.quantity, "received": got},
                    )
                )
        for issue in issues:
            self.session.add(issue)

        if order.status in ("acknowledged", "in_transit"):
            self._transition(order, "received")
        self._audit("order.receive", order.id, {"lines": len(lines), "issues": len(issues)})
        await self.session.flush()
        return issues

    # --------------------------------------------------------------- chasing
    async def sweep_unacknowledged(self, *, now: datetime | None = None) -> list[Issue]:
        """Find orders the supplier has sat on, and raise them once each.

        The reminder is timezone aware in the only way that matters: the suggested
        action says when the supplier is next at their desk, so a buyer is never
        told to chase someone at 2am their time.
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(hours=self.settings.ack_reminder_hours)
        orders = list(
            await self.session.scalars(
                select(PurchaseOrder).where(
                    PurchaseOrder.status == "sent",
                    PurchaseOrder.sent_at.is_not(None),
                    PurchaseOrder.sent_at < cutoff,
                    PurchaseOrder.reminded_at.is_(None),
                )
            )
        )
        raised: list[Issue] = []
        for order in orders:
            supplier = await self.session.get(Supplier, order.supplier_id)
            hours = WorkingHours.from_supplier(supplier)
            open_now = is_open(hours, now)
            when = "now, they are at their desks" if open_now else (
                f"from {next_open(hours, now).astimezone(hours.zone):%H:%M %Z} their time"
            )
            waited = int((now - order.sent_at).total_seconds() // 3600)
            issue = Issue(
                purchase_order_id=order.id,
                supplier_id=order.supplier_id,
                kind="no_acknowledgement",
                severity="high" if waited >= 48 else "medium",
                detail=f"{order.number} sent {waited} hours ago with no acknowledgement",
                suggested_action=f"Chase {supplier.name} {when}",
                context={"hours_waited": waited, "supplier_open": open_now},
            )
            self.session.add(issue)
            order.reminded_at = now
            raised.append(issue)
        await self.session.flush()
        return raised

    async def due_to_send(self, *, now: datetime | None = None) -> list[PurchaseOrder]:
        """Orders queued for a working hour that has now arrived."""
        now = now or datetime.now(UTC)
        return list(
            await self.session.scalars(
                select(PurchaseOrder).where(
                    PurchaseOrder.status == "approved",
                    PurchaseOrder.scheduled_send_at.is_not(None),
                    PurchaseOrder.scheduled_send_at <= now,
                )
            )
        )

    def _audit(self, action: str, entity_id: str, meta: dict) -> None:
        self.session.add(
            AuditLog(
                actor=self.actor, action=action, entity="purchase_order",
                entity_id=entity_id, meta=meta,
            )
        )
