"""Take a supplier reply and, where the reading is confident enough, act on it."""

from datetime import UTC, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sca.inbound import parser
from sca.models import AuditLog, Document, InboundMessage, Issue, PurchaseOrder, Supplier
from sca.orders.service import OrderService

# Below this the message is filed and a human is asked. Set deliberately high:
# a wrong automatic acknowledgement hides a late order until it is too late to
# react, which costs far more than reading an email.
ACT_THRESHOLD = 0.7


class InboundService:
    def __init__(self, session: AsyncSession, *, actor: str = "email-agent"):
        self.session = session
        self.actor = actor
        self.orders = OrderService(session, actor=actor)

    async def ingest(
        self,
        *,
        external_id: str,
        from_address: str | None,
        subject: str | None,
        body: str,
        received_at: datetime | None = None,
    ) -> dict:
        received_at = received_at or datetime.now(UTC)

        existing = await self.session.scalar(
            select(InboundMessage).where(InboundMessage.external_id == external_id)
        )
        if existing is not None:
            # Mailboxes get reprocessed: a reconnect, a backfill, a retry. The
            # message id is what stops one confirmation being applied twice.
            return {"accepted": True, "duplicate": True, "message_id": existing.id}

        found = parser.parse(subject, body, received_at=received_at)
        supplier = await self._match_supplier(from_address)
        order = await self._match_order(found.po_number)

        message = InboundMessage(
            external_id=external_id,
            from_address=from_address,
            supplier_id=supplier.id if supplier else None,
            purchase_order_id=order.id if order else None,
            subject=subject,
            body=body or "",
            received_at=received_at,
            kind=found.kind,
            extracted=found.as_dict(),
            confidence=found.confidence,
        )
        self.session.add(message)
        await self.session.flush()

        actions: list[str] = []
        issue_ids: list[str] = []

        if order is None or found.confidence < ACT_THRESHOLD:
            issue = Issue(
                purchase_order_id=order.id if order else None,
                supplier_id=supplier.id if supplier else None,
                kind="unparsed_message",
                severity="low",
                detail=(
                    f"Reply from {from_address or 'unknown sender'} could not be applied "
                    f"automatically (read as {found.kind}, confidence {found.confidence:.1f})"
                ),
                suggested_action="Read the message and update the order by hand",
                context={"message_id": message.id, "extracted": found.as_dict()},
            )
            self.session.add(issue)
            await self.session.flush()
            issue_ids.append(issue.id)
            actions.append("filed for a human")
        else:
            actions, issue_ids = await self._apply(order, message, found, received_at)

        message.processed_at = datetime.now(UTC)
        self.session.add(
            AuditLog(
                actor=self.actor, action="inbound.ingest", entity="inbound_message",
                entity_id=message.id,
                meta={"kind": found.kind, "confidence": found.confidence, "actions": actions},
            )
        )
        await self.session.flush()
        return {
            "accepted": True,
            "duplicate": False,
            "message_id": message.id,
            "kind": found.kind,
            "confidence": round(found.confidence, 2),
            "purchase_order": order.number if order else None,
            "actions": actions,
            "issue_ids": issue_ids,
        }

    async def _apply(
        self, order: PurchaseOrder, message: InboundMessage, found: parser.Extraction,
        received_at: datetime,
    ) -> tuple[list[str], list[str]]:
        actions: list[str] = []
        issue_ids: list[str] = []

        if found.kind in ("acknowledgement", "delay"):
            confirmed = None
            if found.promised_date:
                confirmed = datetime.combine(found.promised_date, time(12, 0), tzinfo=UTC)
            issue = await self.orders.acknowledge(order, confirmed_date=confirmed,
                                                  now=received_at)
            actions.append(f"order {order.number} acknowledged")
            if confirmed:
                actions.append(f"delivery date set to {found.promised_date.isoformat()}")
            if issue is not None:
                issue_ids.append(issue.id)
                actions.append("date slip raised as an exception")

            restated = self._restated_value(order, found)
            if restated is not None:
                priced = Issue(
                    purchase_order_id=order.id,
                    supplier_id=order.supplier_id,
                    kind="price_mismatch",
                    severity="medium",
                    detail=(
                        f"{order.number} confirmed at {restated:,.2f} against an ordered "
                        f"value of {float(order.total_value):,.2f}"
                    ),
                    suggested_action=(
                        "Query the difference before the order ships, not at invoice"
                    ),
                    context={"confirmed": restated, "ordered": float(order.total_value)},
                )
                self.session.add(priced)
                await self.session.flush()
                issue_ids.append(priced.id)
                actions.append("confirmed value differs from the order")

            if found.kind == "delay" and issue is None:
                # A supplier saying "delayed" without a date is worse than one that
                # gives a late date, because nothing downstream can be replanned.
                nodate = Issue(
                    purchase_order_id=order.id,
                    supplier_id=order.supplier_id,
                    kind="supplier_delay",
                    severity="medium",
                    detail=f"{order.number}: supplier reports a delay",
                    suggested_action="Ask for a firm revised ship date before replanning cover",
                    context={"message_id": message.id},
                )
                self.session.add(nodate)
                await self.session.flush()
                issue_ids.append(nodate.id)

        elif found.kind in ("invoice", "packing_list"):
            document = Document(
                purchase_order_id=order.id,
                kind=found.kind,
                filename=f"{order.number}-{found.kind}.pdf",
                source_message_id=message.id,
                extracted=found.as_dict(),
            )
            self.session.add(document)
            await self.session.flush()
            actions.append(f"{found.kind.replace('_', ' ')} filed against {order.number}")

            if found.kind == "invoice" and found.amounts:
                invoiced = max(found.amounts)
                ordered = float(order.total_value)
                # Tolerance rather than exact match: freight and rounding move the
                # total legitimately, a real mispricing does not hide inside 2%.
                if ordered and abs(invoiced - ordered) / ordered > 0.02:
                    issue = Issue(
                        purchase_order_id=order.id,
                        supplier_id=order.supplier_id,
                        kind="price_mismatch",
                        severity="high",
                        detail=(
                            f"Invoice is {invoiced:,.2f} against an ordered value of "
                            f"{ordered:,.2f}"
                        ),
                        suggested_action="Hold payment and query the difference with the supplier",
                        context={"invoiced": invoiced, "ordered": ordered},
                    )
                    self.session.add(issue)
                    await self.session.flush()
                    issue_ids.append(issue.id)
                    actions.append("price difference raised as an exception")
        else:
            actions.append("read, no action needed")

        return actions, issue_ids

    # Tighter than the 2% allowed on an invoice, and deliberately so: an invoice
    # legitimately carries freight and rounding, while an acknowledgement is the
    # supplier reading our own order back to us. Drift there is a decision, not
    # arithmetic, and it is far cheaper to query before the goods move.
    CONFIRM_TOLERANCE = 0.005

    @staticmethod
    def _restated_value(order: PurchaseOrder, found: parser.Extraction) -> float | None:
        """The confirmed total when it disagrees with ours, otherwise None.

        A reply routinely quotes several figures — a deposit, freight, a unit
        price. Matching against all of them rather than the largest means a
        supplier who states the right total is silent even when they also mention
        a 30% advance, and only a genuine restatement raises anything.
        """
        ordered = float(order.total_value or 0)
        if not ordered or not found.amounts:
            return None
        if any(
            abs(amount - ordered) / ordered <= InboundService.CONFIRM_TOLERANCE
            for amount in found.amounts
        ):
            return None
        # No amount agreed. Report the nearest, which is the one a buyer will
        # recognise as the supplier's version of the total.
        return min(found.amounts, key=lambda amount: abs(amount - ordered))

    async def match_supplier(self, from_address: str | None) -> Supplier | None:
        """Public because the mailbox poller has to ask before it ingests.

        A message pasted into the console is there because a person decided it was
        a supplier reply. A message pulled out of a mailbox has had no such
        decision made about it, and that mailbox also holds the owner's own mail.
        """
        return await self._match_supplier(from_address)

    async def _match_supplier(self, from_address: str | None) -> Supplier | None:
        if not from_address:
            return None
        address = from_address.strip().lower()
        supplier = await self.session.scalar(
            select(Supplier).where(func_lower(Supplier.email) == address)
        )
        if supplier:
            return supplier
        # Fall back to the domain: suppliers reply from whichever colleague picked
        # up the thread, rarely from the address on the contract.
        domain = address.split("@")[-1]
        for candidate in await self.session.scalars(select(Supplier)):
            if candidate.email and candidate.email.lower().split("@")[-1] == domain:
                return candidate
        return None

    async def _match_order(self, number: str | None) -> PurchaseOrder | None:
        if not number:
            return None
        return await self.session.scalar(
            select(PurchaseOrder).where(PurchaseOrder.number == number)
        )


def func_lower(column):
    from sqlalchemy import func

    return func.lower(column)
